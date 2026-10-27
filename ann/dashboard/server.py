"""Read-only ANN dashboard server.

    I:\\python3.14.4\\python.exe dashboard/server.py --port 8391

Read-only by construction:
  * outbound: GET /health, GET /v1/state/tags, POST /v1/state/search (a query,
    not a mutation) against the ANN data plane;
  * inbound: GET only, plus POST /api/search which forwards the same query;
  * local: the index sidecars named below are opened read-only ("rb"/"r").

There is no write endpoint, no cache file, and no ANN file is ever opened for
writing. Nothing here can change ANN output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import core  # noqa: E402


class Config:
    def __init__(self, host, port, ann, index, report, receipts):
        self.host = host
        self.port = port
        self.ann = ann.rstrip("/")
        self.index = pathlib.Path(index)
        self.data_dir = self.index.parent
        self.report = pathlib.Path(report) if report else None
        self.receipts = pathlib.Path(receipts) if receipts else None
        self._lock = threading.Lock()
        self._receipts_cache = None
        self._vector_cache = OrderedDict()

    # -- ANN data plane ---------------------------------------------------
    def ann_get(self, path, timeout=15.0):
        started = time.perf_counter()
        request = urllib.request.Request(self.ann + path)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return payload, round((time.perf_counter() - started) * 1000.0, 2)

    def ann_post(self, path, body, timeout=30.0):
        started = time.perf_counter()
        request = urllib.request.Request(
            self.ann + path,
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return payload, round((time.perf_counter() - started) * 1000.0, 2)

    # -- read-only local sidecars ----------------------------------------
    def read_bytes(self, name):
        return (self.data_dir / name).read_bytes()

    def read_json(self, name):
        return json.loads((self.data_dir / name).read_text(encoding="utf-8"))

    def read_lines(self, path):
        if path is None or not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def index_stat(self):
        info = self.index.stat()
        return {"size": info.st_size, "mtime": int(info.st_mtime)}

    def index_sha256(self):
        digest = hashlib.sha256()
        with self.index.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def read_index_row(self, row):
        """Read one row vector out of the jsonl index, read-only.

        Bounded: the stream stops at the requested row, and the last few
        vectors are cached so paging in the UI does not re-scan.
        """
        key = int(row)
        with self._lock:
            if key in self._vector_cache:
                self._vector_cache.move_to_end(key)
                return self._vector_cache[key]
        if key < 0:
            raise core.InputError("row must be >= 0")
        found = None
        with self.index.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream):
                if number == key:
                    if not line.strip():
                        raise core.InputError(f"row {key} is empty")
                    found = json.loads(line)
                    break
        if found is None:
            raise core.InputError(f"row {key} is past the end of the index")
        vec = found.get("vec")
        if not isinstance(vec, list) or not vec:
            raise core.InputError(f"row {key} has no vector")
        payload = {"row": key, "sid": found.get("sid"), "dim": len(vec), "vec": vec}
        with self._lock:
            self._vector_cache[key] = payload
            while len(self._vector_cache) > 32:
                self._vector_cache.popitem(last=False)
        return payload

    # -- receipts sidecar -------------------------------------------------
    def receipts_state(self):
        with self._lock:
            if self._receipts_cache is not None:
                return self._receipts_cache
        state = {
            "configured": False,
            "path": None,
            "label": "none",
            "rows": 0,
            "malformed": [],
            "bound": True,
            "binding": {"reason": "no_sidecar_configured"},
            "context": None,
            "receipts": {},
        }
        if self.receipts is not None:
            label = "structural_test_data" if "sift" in self.receipts.name.lower() else "producer"
            receipts, malformed = core.parse_receipt_lines(self.read_lines(self.receipts))
            manifest_path = self.receipts.with_name(self.receipts.name + ".manifest.json")
            binding = {"reason": "manifest_missing"}
            context = None
            bound = False
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                binding = {
                    "reason": "ok",
                    "rows": manifest.get("rows"),
                    "producer_epoch": manifest.get("producer_epoch"),
                    "sidecar_sha256": manifest.get("sha256"),
                    "index_sha256": manifest.get("index_sha256"),
                }
                bound = True
                totals = manifest.get("totems")
                edges = manifest.get("edges")
                context = core.ValidationContext(
                    generation=int(manifest.get("generation", -1)),
                    totems=frozenset(int(item) for item in (totals or [])),
                    frustum=int(manifest.get("frustum", -1)),
                    edges=frozenset(str(item) for item in (edges or [])),
                    intent=str(manifest.get("intent", "unknown")),
                )
            state = {
                "configured": True,
                "path": str(self.receipts),
                "label": label,
                "rows": len(receipts),
                "malformed": malformed,
                "bound": bound,
                "binding": binding,
                "context": None if context is None else {
                    "generation": context.generation,
                    "totems": sorted(context.totems),
                    "frustum": context.frustum,
                    "edges": sorted(context.edges),
                    "intent": context.intent,
                },
                "_context": context,
                "receipts": receipts,
            }
        with self._lock:
            self._receipts_cache = state
        return state

    def receipts_public(self):
        state = self.receipts_state()
        return {key: value for key, value in state.items() if not key.startswith("_") and key != "receipts"}


def build_index_view(config):
    """Index view: epoch binding, zone split, per-door occupancy, coverage."""
    stamp_data = config.read_bytes("ann_index.jsonl.stamp")
    order_rows = core.parse_order(config.read_bytes("ann_index.order"))
    stamps = core.parse_stamps(stamp_data)
    anchors_state = config.read_json("ann_anchors-state.json")
    tag_lut_report = config.read_json("tag_lut_report.json")

    tags, _tags_ms = config.ann_get("/v1/state/tags")
    health, _health_ms = config.ann_get("/health")
    health_view = core.summarize_health(health)

    occupancy = core.bucket_occupancy(stamps)
    zones = core.zone_distribution(tags)
    binding = core.epoch_binding(config.index_stat(), anchors_state)

    pairs = len(stamps)
    return {
        "ok": True,
        "index": {
            "path": str(config.index),
            "rows": pairs,
            "stat": config.index_stat(),
            "order_rows": order_rows,
            "stamp_rows": pairs,
            "order_delta": order_rows - pairs,
            "order_covers_rows": order_rows >= pairs,
        },
        "binding": binding,
        "epoch": {
            "n_prev": anchors_state.get("n_prev"),
            "last_sid": anchors_state.get("last_sid"),
            "k": anchors_state.get("K"),
            "anchors_cksum": anchors_state.get("anchors_cksum"),
            "anchors_size_bytes": (config.data_dir / "ann_anchors.bin").stat().st_size
            if (config.data_dir / "ann_anchors.bin").exists() else None,
            "roster_sidecar_bytes": (config.data_dir / "ann_anchors-roster.bin").stat().st_size
            if (config.data_dir / "ann_anchors-roster.bin").exists() else None,
        },
        "bucket_occupancy": occupancy,
        "zones": zones,
        "tag_bake": {
            "bake_id": tag_lut_report.get("bake_id"),
            "baked_at": tag_lut_report.get("baked_at"),
            "index_rows": tag_lut_report.get("index_rows"),
            "tagged_rows": tag_lut_report.get("tagged_rows"),
            "orphan_rows": tag_lut_report.get("orphan_rows"),
            "untagged_rows": tag_lut_report.get("untagged_rows"),
            "backward_changed_total": tag_lut_report.get("backward_changed_total"),
        },
        "health": health_view,
        "health_entries": health.get("entries"),
        "buckets_live": health.get("k"),
        "receipts": config.receipts_public(),
    }


def build_shadow_view(config):
    lines = config.read_lines(config.report)
    summary = core.shadow_summary(lines)
    summary["report_path"] = str(config.report) if config.report else None
    summary["report_present"] = config.report is not None and config.report.exists()
    summary["receipts"] = config.receipts_public()
    return summary


class Handler(BaseHTTPRequestHandler):
    server_version = "ann-dashboard/1.0"
    config: Config = None  # set in main()

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, sort_keys=True)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, kind, message):
        self._send(status, {"ok": False, "error": {"kind": kind, "message": str(message)}})

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, self._asset("index.html"), "text/html; charset=utf-8")
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path == "/api/health":
                return self._health()
            if path == "/api/index":
                return self._index()
            if path == "/api/vector":
                return self._vector(query)
            if path == "/api/shadow":
                return self._send(200, build_shadow_view(self.config))
            if path == "/api/receipts":
                return self._send(200, self.config.receipts_public())
            return self._error(404, "not_found", f"no route {path}")
        except core.InputError as error:
            return self._error(400, "bad_request", error)
        except urllib.error.URLError as error:
            return self._error(502, "upstream_unavailable", error)
        except Exception as error:  # keep the dashboard honest, never a blank page
            return self._error(500, "internal_error", error)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/search":
            return self._error(404, "not_found", f"no route {parsed.path}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4 * 1024 * 1024:
                raise core.InputError("request body missing or too large")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except ValueError as error:
                raise core.InputError(f"invalid json: {error}")
            request = core.parse_search_request(body)
            response, ann_ms = self.config.ann_post("/v1/state/search", request)
            receipt_state = self.config.receipts_state()
            context = receipt_state.get("_context")
            classification = core.classify_candidates(
                response.get("hits", []),
                receipt_state.get("receipts", {}),
                context if context is not None else core.ValidationContext(
                    generation=-1, totems=frozenset(), frustum=-1, edges=frozenset()),
                bound=bool(receipt_state.get("bound")),
                label=receipt_state.get("label", "live"),
            )
            self._send(200, {
                "ok": True,
                "request": {"topk": request["topk"], "scope": request["scope"],
                            "dim": len(request["vec"])},
                "response": response,
                "candidate_receipts": classification,
                "receipts": self.config.receipts_public(),
                "timing": {"ann_ms": ann_ms},
            })
        except core.InputError as error:
            return self._error(400, "bad_request", error)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            return self._error(502, "upstream_rejected", f"{error.code}: {detail}")
        except urllib.error.URLError as error:
            return self._error(502, "upstream_unavailable", error)
        except Exception as error:
            return self._error(500, "internal_error", error)

    # -- handlers ---------------------------------------------------------
    def _asset(self, name):
        path = HERE / "static" / name
        return path.read_text(encoding="utf-8")

    def _health(self):
        started = time.perf_counter()
        try:
            payload, _ms = self.config.ann_get("/health")
        except Exception as error:
            body = core.unavailable_health(error)
            body["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            body["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            return self._send(200, body)
        view = core.summarize_health(payload)
        view["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        view["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return self._send(200, view)

    def _index(self):
        try:
            view = build_index_view(self.config)
        except urllib.error.URLError as error:
            return self._error(502, "upstream_unavailable", error)
        except core.InputError as error:
            return self._error(500, "sidecar_malformed", error)
        return self._send(200, view)

    def _vector(self, query):
        fixture = (query.get("fixture") or [""])[0]
        if fixture == "zeros":
            return self._send(200, {"fixture": "zeros", "dim": 384, "vec": [0.0] * 384})
        if fixture == "unit":
            vec = [0.0] * 384
            vec[0] = 1.0
            return self._send(200, {"fixture": "unit", "dim": 384, "vec": vec})
        row = (query.get("row") or ["0"])[0]
        try:
            row_number = int(row)
        except ValueError:
            raise core.InputError("row must be an integer")
        return self._send(200, self.config.read_index_row(row_number))


def main(argv=None):
    parser = argparse.ArgumentParser(description="read-only ANN dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8391)
    parser.add_argument("--ann", default="http://127.0.0.1:8096")
    parser.add_argument("--index", default=r"I:\tools\ann\data\memcore\ann_index.jsonl")
    parser.add_argument("--report", default=r"I:\tools\ann\data\memcore\totem_shadow.jsonl")
    parser.add_argument("--receipts", default="",
                        help="optional sid -> totem_receipt jsonl sidecar")
    args = parser.parse_args(argv)

    Handler.config = Config(
        host=args.host,
        port=args.port,
        ann=args.ann,
        index=args.index,
        report=args.report,
        receipts=args.receipts,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ANN dashboard (read-only) on http://{args.host}:{args.port}")
    print(f"  ann     : {args.ann}")
    print(f"  index   : {args.index}")
    print(f"  report  : {args.report}")
    print(f"  receipts: {args.receipts or '(none — candidate receipts report missing)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()