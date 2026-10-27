#!/usr/bin/env python3
"""Dashboard core tests: parsing, receipts, error paths, deterministic render.

No numpy, no live ANN, no network except one loopback server on a temp port.
"""
import json
import os
import pathlib
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DASH = HERE.parent
ANN = DASH.parent
for path in (str(DASH), str(ANN)):
    if path not in sys.path:
        sys.path.insert(0, path)

import core  # noqa: E402
from totem_shadow_validate import ValidationContext  # noqa: E402

HEALTH_OK = {
    "status": "ok", "entries": 100, "routed": 1, "stamps_live": 100, "route_dead": 0,
    "maxk": 32, "k": 64, "labels": "derive", "order_stale": 0,
    "loader": {"mode": "bg", "index_age_s": 1.0},
    "tags": {"kinds": 12, "tagged": 100, "unlabeled": 0},
    "stamp": {"live": 100, "unplaced": 0, "side_rows": 100, "gate": "ok", "gate_code": 0},
    "totem": {"replayed": 1, "shut": 2, "free": 10, "pair": 4, "lit": 5, "open": 9},
    "roster": {"on": 1, "doors": 8, "rows": 20, "bytes": 80, "src": "mmap"},
}

CONTEXT = ValidationContext(
    generation=7, totems=frozenset({12}), frustum=4, edges=frozenset({"L"}), intent="unknown")


class SearchRequestTests(unittest.TestCase):
    def test_accepts_valid(self):
        got = core.parse_search_request({"vec": [1, 2.5], "topk": 3, "scope": "zone:0"})
        self.assertEqual(got, {"vec": [1.0, 2.5], "topk": 3, "scope": "zone:0"})

    def test_defaults(self):
        got = core.parse_search_request({"vec": [0.0]})
        self.assertEqual(got["topk"], 5)
        self.assertEqual(got["scope"], "")

    def test_rejects_non_object(self):
        for body in ([], "x", None, 3):
            with self.assertRaises(core.InputError):
                core.parse_search_request(body)

    def test_rejects_bad_vector(self):
        for vec in ([], "abc", [1, "2"], [True], [float("nan")], [float("inf")]):
            with self.assertRaises(core.InputError):
                core.parse_search_request({"vec": vec})

    def test_rejects_oversized_vector(self):
        with self.assertRaises(core.InputError):
            core.parse_search_request({"vec": [0.0] * (core.MAX_DIM + 1)})

    def test_topk_is_validated_not_coerced(self):
        for topk in ("5", 5.0, 0, 65, True, None):
            with self.assertRaises(core.InputError):
                core.parse_search_request({"vec": [0.0], "topk": topk})

    def test_rejects_bad_scope(self):
        for scope in (5, "x" * (core.MAX_SCOPE + 1)):
            with self.assertRaises(core.InputError):
                core.parse_search_request({"vec": [0.0], "scope": scope})


class HealthTests(unittest.TestCase):
    def test_clean(self):
        view = core.summarize_health(HEALTH_OK)
        self.assertEqual(view["state"], "live")
        self.assertEqual(view["warnings"], [])
        self.assertEqual(view["roster"]["doors"], 8)

    def test_missing_keys_are_named(self):
        payload = dict(HEALTH_OK)
        del payload["route_dead"]
        del payload["status"]
        view = core.summarize_health(payload)
        self.assertEqual(view["state"], "degraded")
        codes = [w["code"] for w in view["warnings"]]
        self.assertIn("missing_health_keys", codes)
        missing = next(w for w in view["warnings"] if w["code"] == "missing_health_keys")
        self.assertEqual(missing["keys"], ["status", "route_dead"])

    def test_dead_route_is_a_warning(self):
        payload = dict(HEALTH_OK, route_dead=3)
        view = core.summarize_health(payload)
        self.assertEqual(view["state"], "degraded")
        self.assertIn("route_dead_nonzero", [w["code"] for w in view["warnings"]])

    def test_gate_and_roster_warnings(self):
        payload = dict(HEALTH_OK)
        payload["stamp"] = dict(HEALTH_OK["stamp"], gate="reject", unplaced=2)
        payload["roster"] = dict(HEALTH_OK["roster"], on=0)
        payload["loader"] = {"mode": "sync"}
        codes = {w["code"] for w in core.summarize_health(payload)["warnings"]}
        self.assertTrue({"stamp_gate_not_ok", "stamp_unplaced_nonzero",
                         "roster_off", "loader_not_background"} <= codes)

    def test_malformed_section(self):
        payload = dict(HEALTH_OK, roster=["not", "a", "dict"])
        codes = [w["code"] for w in core.summarize_health(payload)["warnings"]]
        self.assertIn("malformed_section", codes)

    def test_unavailable_shape(self):
        view = core.unavailable_health("connection refused")
        self.assertFalse(view["ok"])
        self.assertEqual(view["state"], "unavailable")
        self.assertEqual(view["error"]["kind"], "upstream_unavailable")


def stamp_bytes(pairs):
    return b"".join(struct.pack("<ii", a, b) for a, b in pairs)


class SidecarTests(unittest.TestCase):
    def test_parse_stamps_rejects_partial(self):
        with self.assertRaises(core.InputError):
            core.parse_stamps(b"\x00" * 7)

    def test_parse_order(self):
        self.assertEqual(core.parse_order(b"\x00" * 12), 3)
        with self.assertRaises(core.InputError):
            core.parse_order(b"\x00" * 10)

    def test_occupancy_counts_own_and_pair(self):
        occ = core.bucket_occupancy(core.parse_stamps(stamp_bytes([(0, 1), (0, 1), (1, 1), (2, 0)])))
        self.assertEqual(occ["rows"], 4)
        self.assertEqual(occ["doors"], 3)
        self.assertEqual(occ["memberships"], 7)
        top = {row["door"]: row for row in occ["top"]}
        self.assertEqual(top[0]["own"], 2)
        self.assertEqual(top[0]["pair"], 1)
        self.assertEqual(top[0]["memberships"], 3)
        self.assertEqual(top[1]["pair"], 2)
        self.assertEqual(occ["top"][0]["door"], 0)  # sorted by memberships then door

    def test_negative_doors_are_counted_not_dropped_silently(self):
        occ = core.bucket_occupancy(core.parse_stamps(stamp_bytes([(-1, -1), (5, 5)])))
        self.assertEqual(occ["negative_rows"], 1)
        self.assertEqual(occ["doors"], 1)

    def test_zone_distribution(self):
        tags = {"entries": 10, "tagged": 10, "unlabeled": 0, "lut_zones": 2,
                "zones": [{"zone": 1, "tag": "b", "rows": 6, "retired": False},
                          {"zone": 0, "tag": "a", "rows": 4, "retired": True}]}
        view = core.zone_distribution(tags)
        self.assertEqual([z["zone"] for z in view["zones"]], [0, 1])
        self.assertTrue(view["coverage_ok"])
        self.assertTrue(view["zones"][0]["retired"])

    def test_zone_coverage_mismatch(self):
        tags = {"entries": 99, "zones": [{"zone": 0, "tag": "a", "rows": 4}]}
        self.assertFalse(core.zone_distribution(tags)["coverage_ok"])

    def test_zone_malformed(self):
        for bad in ({"zones": "x"},
                    {"zones": [1]},
                    {"zones": [{"zone": "0", "tag": "a", "rows": 1}]},
                    {"zones": [{"zone": 0, "tag": "a", "rows": -1}]}):
            with self.assertRaises(core.InputError):
                core.zone_distribution(bad)

    def test_epoch_binding(self):
        stat = {"size": 10, "mtime": 20}
        self.assertTrue(core.epoch_binding(stat, {"index_size": 10, "index_mtime": 20})["match"])
        stale = core.epoch_binding(stat, {"index_size": 10, "index_mtime": 21})
        self.assertFalse(stale["match"])
        self.assertEqual(stale["reason"], "stale_sidecar")
        self.assertEqual(core.epoch_binding(stat, None)["reason"], "anchors_state_missing")


class ReceiptTests(unittest.TestCase):
    def test_parse_receipts_surfaces_malformed(self):
        lines = ['{"sid":"a","totem_receipt":{"generation":7}}',
                 "not json",
                 '{"sid":5,"totem_receipt":{}}',
                 '{"sid":"b"}',
                 ""]
        receipts, malformed = core.parse_receipt_lines(lines)
        self.assertEqual(sorted(receipts), ["a"])
        self.assertEqual(len(malformed), 3)
        self.assertIn("invalid json", malformed[0]["error"])

    def test_missing_is_missing_never_inferred(self):
        hits = [{"sid": "a", "file": "f", "score": 0.9}]
        result = core.classify_candidates(hits, {}, CONTEXT)
        self.assertEqual(result["counts"]["missing"], 1)
        self.assertEqual(result["findings"][0]["status"], "missing")
        self.assertEqual(result["present_coverage"], 0.0)

    def test_valid_invalid_stale(self):
        receipts = {
            "v": {"generation": 7, "totem_id": 12, "frustum_id": 4, "wang_edge": "L"},
            "i": {"generation": 7, "totem_id": 12, "frustum_id": 9, "wang_edge": "L"},
            "s": {"generation": 6, "totem_id": 12, "frustum_id": 4, "wang_edge": "L"},
        }
        hits = [{"sid": "v"}, {"sid": "i"}, {"sid": "s"}, {"sid": "x"}]
        result = core.classify_candidates(hits, receipts, CONTEXT)
        statuses = {f["sid"]: f["status"] for f in result["findings"]}
        self.assertEqual(statuses, {"v": "valid", "i": "invalid", "s": "stale", "x": "missing"})
        self.assertEqual(result["counts"], {"hits": 4, "valid": 1, "invalid": 1, "stale": 1, "missing": 1})
        self.assertEqual(result["present_coverage"], 0.75)

    def test_unbound_sidecar_marks_present_receipts_stale(self):
        receipts = {"v": {"generation": 7, "totem_id": 12, "frustum_id": 4, "wang_edge": "L"}}
        result = core.classify_candidates([{"sid": "v"}], receipts, CONTEXT, bound=False)
        self.assertEqual(result["findings"][0]["status"], "stale")
        self.assertIn("index_binding_mismatch", result["findings"][0]["violations"])

    def test_relation_intent_requires_relation_valid(self):
        context = ValidationContext(generation=7, totems=frozenset({12}), frustum=4,
                                    edges=frozenset({"L"}), intent="relation")
        receipt = {"generation": 7, "totem_id": 12, "frustum_id": 4, "wang_edge": "L"}
        result = core.classify_candidates([{"sid": "a"}], {"a": receipt}, context)
        self.assertEqual(result["findings"][0]["status"], "invalid")
        self.assertIn("relation_edge_mismatch", result["findings"][0]["violations"])

    def test_malformed_hit(self):
        result = core.classify_candidates(["not a dict"], {}, CONTEXT)
        self.assertEqual(result["findings"][0]["status"], "invalid")
        self.assertEqual(result["counts"]["invalid"], 1)

    def test_empty_hits(self):
        result = core.classify_candidates([], {}, CONTEXT)
        self.assertEqual(result["counts"]["hits"], 0)
        self.assertEqual(result["present_coverage"], 0.0)


class ShadowTests(unittest.TestCase):
    def test_summary_and_totals(self):
        lines = [
            json.dumps({"event_time": "t1", "returned_hits": 5, "candidate_receipts": 0,
                        "candidate_receipts_missing": 5, "route_receipt": "present",
                        "route_violations": [], "walk_hash": 0, "items_scored": 10,
                        "items_total": 100}),
            json.dumps({"event_time": "t2", "returned_hits": 5, "candidate_receipts": 2,
                        "candidate_receipts_missing": 3, "route_receipt": "present",
                        "route_violations": ["totem_lit_bucket_count_mismatch"],
                        "walk_hash": 7, "items_scored": 11, "items_total": 100}),
        ]
        view = core.shadow_summary(lines)
        self.assertEqual(view["reports"], 2)
        self.assertEqual(view["returned_hits"], 10)
        self.assertEqual(view["candidate_receipts_missing"], 8)
        self.assertEqual(view["route_violation_count"], 1)
        self.assertEqual(view["coverage"], 0.2)
        self.assertEqual(view["first_event_time"], "t1")
        self.assertEqual(view["last_event_time"], "t2")
        self.assertEqual(len(view["timeline"]), 2)

    def test_malformed_rows_are_visible(self):
        lines = ["not json", json.dumps({"no": "event_time"}),
                 json.dumps({"event_time": "t", "route_violations": "x"})]
        view = core.shadow_summary(lines)
        self.assertEqual(view["reports"], 1)
        self.assertEqual(len(view["malformed"]), 3)

    def test_empty_report(self):
        view = core.shadow_summary([])
        self.assertEqual(view["reports"], 0)
        self.assertIsNone(view["last_event_time"])
        self.assertEqual(view["coverage"], 0.0)

    def test_timeline_is_bounded(self):
        lines = [json.dumps({"event_time": f"t{i}"}) for i in range(80)]
        view = core.shadow_summary(lines, max_timeline=10)
        self.assertEqual(len(view["timeline"]), 10)
        self.assertEqual(view["timeline"][-1]["event_time"], "t79")

    def test_deterministic_serialization(self):
        payload = core.shadow_summary([json.dumps({"event_time": "t", "route_violations": []})])
        self.assertEqual(core.deterministic_json(payload), core.deterministic_json(payload))


class ServerErrorPathTests(unittest.TestCase):
    """The server must report unavailable/degraded states, never a blank page."""

    @classmethod
    def setUpClass(cls):
        import server as dashboard_server
        from http.server import ThreadingHTTPServer

        cls.tmp = tempfile.TemporaryDirectory()
        data = pathlib.Path(cls.tmp.name)
        (data / "ann_index.jsonl").write_text('{"sid":"a","vec":[1,0,0]}\n', encoding="utf-8")
        (data / "ann_index.jsonl.stamp").write_bytes(stamp_bytes([(0, 1)]))
        (data / "ann_index.order").write_bytes(b"\x00" * 4)
        (data / "ann_anchors-state.json").write_text(
            json.dumps({"n_prev": 1, "index_size": 0, "index_mtime": 0}), encoding="utf-8")
        (data / "tag_lut_report.json").write_text("{}", encoding="utf-8")
        (data / "shadow.jsonl").write_text('{"event_time":"t1","route_violations":[]}\n',
                                           encoding="utf-8")
        cls.data = data
        # ANN points at a closed port -> every live call fails fast.
        dashboard_server.Handler.config = dashboard_server.Config(
            host="127.0.0.1", port=0, ann="http://127.0.0.1:9",
            index=data / "ann_index.jsonl", report=data / "shadow.jsonl", receipts="")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_health_unavailable_is_200_and_honest(self):
        payload = json.load(urllib.request.urlopen(self.url("/api/health"), timeout=10))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "unavailable")
        self.assertIn("fetched_at", payload)

    def test_index_without_ann_is_502(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url("/api/index"), timeout=10)
        self.assertEqual(caught.exception.code, 502)

    def test_search_without_ann_is_502(self):
        body = json.dumps({"vec": [1.0, 0.0, 0.0], "topk": 3}).encode()
        request = urllib.request.Request(self.url("/api/search"), body,
                                         {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 502)

    def test_search_rejects_bad_topk_with_400(self):
        body = json.dumps({"vec": [1.0], "topk": "5"}).encode()
        request = urllib.request.Request(self.url("/api/search"), body,
                                         {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_shadow_reads_local_report(self):
        payload = json.load(urllib.request.urlopen(self.url("/api/shadow"), timeout=10))
        self.assertEqual(payload["reports"], 1)
        self.assertTrue(payload["report_present"])

    def test_vector_from_local_index(self):
        payload = json.load(urllib.request.urlopen(self.url("/api/vector?row=0"), timeout=10))
        self.assertEqual(payload["dim"], 3)
        self.assertEqual(payload["sid"], "a")

    def test_vector_past_end_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url("/api/vector?row=99"), timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url("/nope"), timeout=10)
        self.assertEqual(caught.exception.code, 404)

    def test_no_write_endpoints(self):
        request = urllib.request.Request(self.url("/api/index"), b"{}", {"Content-Type": "application/json"})
        request.get_method = lambda: "PUT"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 501)


if __name__ == "__main__":
    unittest.main(verbosity=2)