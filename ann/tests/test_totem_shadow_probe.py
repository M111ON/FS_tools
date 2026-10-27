import unittest

from totem_shadow_probe import probe


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self):
        import json
        return json.dumps(self.payload).encode()


class ShadowProbeTest(unittest.TestCase):
    def test_live_shape_counts_missing_candidate_receipts(self):
        import totem_shadow_probe
        old = totem_shadow_probe.urllib.request.urlopen
        totem_shadow_probe.urllib.request.urlopen = lambda *a, **k: FakeResponse({
            "hits": [{"sid": "a"}, {"sid": "b"}],
            "route": {"totem": {"lit": 1}, "walk_hash": 9,
                      "items_scored": 2, "items_total": 10}})
        try:
            got = probe("http://test", [0.0], "", 2)
        finally:
            totem_shadow_probe.urllib.request.urlopen = old
        self.assertEqual(got["returned_hits"], 2)
        self.assertEqual(got["candidate_receipts_missing"], 2)
        self.assertTrue(got["hits_unchanged"])

    def test_report_is_append_only_jsonl(self):
        import tempfile
        from pathlib import Path
        from totem_shadow_probe import append_report
        path = Path(tempfile.mktemp())
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        append_report(path, {"mode": "shadow", "missing": 2})
        append_report(path, {"mode": "shadow", "missing": 1})
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_sid_receipts_are_joined_without_filtering_hits(self):
        import totem_shadow_probe
        old = totem_shadow_probe.urllib.request.urlopen
        totem_shadow_probe.urllib.request.urlopen = lambda *a, **k: FakeResponse({
            "hits": [{"sid": "a"}, {"sid": "b"}], "route": {"totem": {"lit": 0}, "buckets": []}})
        try:
            got = totem_shadow_probe.probe("http://test", [0.0], receipts={"a": {}})
        finally:
            totem_shadow_probe.urllib.request.urlopen = old
        self.assertEqual(got["candidate_receipts"], 1)
        self.assertEqual(got["candidate_receipts_missing"], 1)
        self.assertEqual(got["returned_hits"], 2)


if __name__ == "__main__":
    unittest.main()
