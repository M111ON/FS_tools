#!/usr/bin/env python
"""first_run.py — first-run health contract for the ANN memory layer.

Runs every check a new user needs before their first real query, prints ONE
verdict (READY / NOT READY) plus the exact fix for each failing check.

Usage:
    python first_run.py            # human-readable
    python first_run.py --json     # machine-readable
"""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.request
import urllib.error

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

EMBED_HOST = os.environ.get("LZ_EMBED_HOST", "127.0.0.1")
EMBED_PORT = int(os.environ.get("LZ_EMBED_PORT", "18095"))
ANN_HOST = os.environ.get("ANN_HOST", "127.0.0.1")
ANN_PORT = int(os.environ.get("ANN_PORT", "8096"))

SAMPLE_QUERY = "DWGLS field contract"


def _get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return None, {"error": str(e)}


def check_embedder():
    status, data = _get("http://%s:%d/health" % (EMBED_HOST, EMBED_PORT))
    if status != 200 or not isinstance(data, dict) or data.get("status") != "ok":
        return {"ok": False, "name": "embedder", "detail": str(data.get("error", "unreachable")),
                "fix": "start llama-server: I:/llama/llama-b10830-win-vulkan-x64/llama-server.exe -m I:/model/multilingual-e5-small-q8_0-fixed.gguf --embedding --port %d -c 512" % EMBED_PORT}
    # warm probe (steady-state, persistent conn)
    try:
        conn = http.client.HTTPConnection(EMBED_HOST, EMBED_PORT, timeout=10)
        conn.request("POST", "/embedding", body=json.dumps({"content": "warmup"}).encode(),
                    headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
        conn = http.client.HTTPConnection(EMBED_HOST, EMBED_PORT, timeout=10)
        t0 = time.perf_counter()
        conn.request("POST", "/embedding", body=json.dumps({"content": "probe"}).encode(),
                    headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        probe_ms = (time.perf_counter() - t0) * 1000.0
        conn.close()
        slow = probe_ms > 50
        return {"ok": True, "name": "embedder", "detail": "healthy, probe %.1fms" % probe_ms,
                "fix": ("embed latency %.1fms > 50ms target" % probe_ms) if slow else None,
                "probe_ms": round(probe_ms, 1)}
    except Exception as e:
        return {"ok": False, "name": "embedder", "detail": str(e),
                "fix": "embedder health OK but embedding call failed; restart sidecar"}


def check_ann_server():
    status, data = _get("http://%s:%d/health" % (ANN_HOST, ANN_PORT))
    if status != 200 or not isinstance(data, dict) or data.get("status") != "ok":
        return {"ok": False, "name": "ann_server", "detail": str(data.get("error", "unreachable")),
                "fix": "start ANN server: cd I:/tools/ann && python ann_keepalive.py --once"}
    entries = data.get("entries", 0)
    if entries <= 0:
        return {"ok": False, "name": "ann_server", "detail": "0 entries indexed",
                "fix": "regrow index"}
    return {"ok": True, "name": "ann_server", "detail": "%d entries, %s" % (entries, data.get("loader", {}).get("mode", "?")),
            "fix": None}


def check_index_file():
    idx = os.path.join(DIR, "data", "memcore", "ann_index.jsonl")
    if not os.path.exists(idx):
        return {"ok": False, "name": "index_file", "detail": "missing", "fix": "run ann_sync to build index"}
    size = os.path.getsize(idx)
    age = time.time() - os.path.getmtime(idx)
    if age > 7 * 86400:
        return {"ok": True, "name": "index_file", "detail": "%.1f MB, %.1f days old (stale)" % (size / 1e6, age / 86400),
                "fix": "index >7 days old; consider regrow"}
    return {"ok": True, "name": "index_file", "detail": "%.1f MB, %.1f hours old" % (size / 1e6, age / 3600),
            "fix": None}


def check_sample_query():
    status, _ = _get("http://%s:%d/health" % (ANN_HOST, ANN_PORT))
    if status != 200:
        return {"ok": False, "name": "sample_query", "detail": "ann server down", "fix": "see ann_server check"}
    try:
        import mcp_ann_search as m
        s = m._get_gpu_search()
        s._ensure_loaded()
        t0 = time.perf_counter()
        r = s.search(SAMPLE_QUERY, topk=3)
        lat = (time.perf_counter() - t0) * 1000.0
        hits = r.get("hits", []) if isinstance(r, dict) else []
        if not hits:
            return {"ok": False, "name": "sample_query", "detail": "0 hits", "fix": "index empty or embedder mismatch"}
        return {"ok": True, "name": "sample_query",
                "detail": "%d hits, %.1fms (top %.3f)" % (len(hits), lat, hits[0].get("score", 0)),
                "fix": None,
                "latency_ms": round(lat, 1)}
    except Exception as e:
        return {"ok": False, "name": "sample_query", "detail": str(e), "fix": "check python import path"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    checks = [check_embedder(), check_ann_server(), check_index_file(), check_sample_query()]
    ready = all(c.get("ok") for c in checks)
    result = {"ready": ready, "checks": checks}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ready else 1
    print("=" * 60)
    print("ANN MEMORY LAYER - FIRST-RUN CHECK")
    print("=" * 60)
    for c in checks:
        flag = "OK  " if c.get("ok") else "FAIL"
        print("[%s] %-14s %s" % (flag, c["name"], c["detail"]))
        if c.get("fix"):
            print("        fix: %s" % c["fix"])
    print("-" * 60)
    print("VERDICT: %s" % ("READY - run your first real query." if ready else "NOT READY - apply fixes above, re-run."))
    print("=" * 60)
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
PY