#!/usr/bin/env python
"""mcp_ann_search.py — MCP facade over the ANN semantic index (:8096).

The ANN server (I:/tools/ann, ann_search.c) is a plain HTTP service, not MCP.
This is a THIN PROXY: one tool forwards to `POST /v1/state/search` and returns
the JSON as-is — no ranking or filtering logic here (same rule as the taskboard
proxy: all logic stays in the service itself).

The index is DERIVED from the memcore store (cloud_ws/memcore/ann_sync.py keeps
I:/tools/ann/data/memcore/ann_index.jsonl in step with fact_store.v2.sqlite3),
so this is the semantic half of memcore: memcore_query ranks via this server
too (v2.py `_ann_vec_rank`), and this tool lets an agent query the same index
directly by text.

Requires the ANN server running:
    LZ_EMBED_HOST=127.0.0.1 LZ_EMBED_PORT=18095 ./build/ann_search.exe --serve 8096
Check health first: GET /health (see ann/README.md).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("ANN_URL", "http://127.0.0.1:8096")
TIMEOUT = float(os.environ.get("ANN_TIMEOUT", "15"))

mcp = FastMCP("ann-search")


def _call(method, path, body=None):
    """Forward one HTTP call to the ANN server; return parsed JSON or an
    error dict (never raise into MCP — the caller wants a readable answer)."""
    url = BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return {"error": json.loads(raw)}
        except json.JSONDecodeError:
            return {"error": f"HTTP {e.code}", "body": raw}
    except OSError as e:
        return {
            "error": f"ANN server unreachable at {BASE}: {e}",
            "hint": "start it: LZ_EMBED_HOST=127.0.0.1 LZ_EMBED_PORT=18095 "
                    "./build/ann_search.exe --serve 8096 (see ann/README.md)",
        }


@mcp.tool()
def ann_search(query: str, topk: int = 5, sid: str = "", coord: str = "") -> str:
    """Semantic search over the memcore-derived ANN index (~62k entries).

    Args:
        query: natural language query (Thai or English)
        topk: number of results (default 5)
        sid: optional session id — FILTERS the search to that session's rows
        coord: optional provenance stamp "sid#ordinal" of the message this query
            is about. It does not change the search; it lands in
            data/query_log/ann_query-*.jsonl so `chatmap.py when` can later show
            which searches consumed that message. Omit it and the log records
            coord:null, which is honest but unattributable.

    Returns the ANN server's JSON: hits with scores plus route/anchor info."""
    body = {"query": query, "topk": max(1, min(int(topk), 64))}
    if sid:
        body["sid"] = sid
    if coord:
        body["coord"] = coord
    return json.dumps(_call("POST", "/v1/state/search", body),
                      ensure_ascii=False, indent=1)


# Lazy singleton for the GPU/CPU-resident search (loaded on first use).
_gpu_search = {"inst": None}


def _get_gpu_search():
    if _gpu_search["inst"] is None:
        from ann_search_gpu import AnnSearchGPU
        _gpu_search["inst"] = AnnSearchGPU()
    return _gpu_search["inst"]


@mcp.tool()
def ann_search_fast(query: str, topk: int = 5, kinds: str = "", budget: float = 5.0) -> str:
    """Fast semantic search using a resident index (GPU/CPU torch).

    Same corpus as ann_search (~54k entries) but scans via a resident,
    normalized matrix (torch matmul) instead of the C server's path.
    Measured: ~90 ms vs ~1250 ms for ann_search (14x) on this box.
    kinds: comma-separated kinds to include, e.g. "embedded_document,dictionary_entry"
           (omit for all). Useful to filter out chatlog/exchange noise.
    Use this when latency matters; falls back to CPU torch if no GPU.

    IMPORTANT: the result contains a 'footer' field. Print it verbatim as the
    LAST line of your reply whenever you call this tool, e.g.
    [ann] 1 query, 2 hits | 142.0ms (embed 130.58ms / scan 11.43ms) | device=cpu index=54255
    This is the per-call recall/speed receipt; do not paraphrase or omit it."""

    try:
        s = _get_gpu_search()
        ks = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        result = s.search(query, topk=max(1, min(int(topk), 64)), kinds=ks)
        hits = result.get("hits", []) if isinstance(result, dict) else result
        last = getattr(s, "_last", None) or {}
        footer = ("[ann] {q} query, {h} hits | {lat}ms "
                  "(embed {emb}ms / scan {scn}ms) | device={dev} index={idx}").format(
            q=1, h=last.get("hits", len(hits)),
            lat=last.get("latency_ms", 0),
            emb=last.get("embed_ms", 0), scn=last.get("scan_ms", 0),
            dev=last.get("device", s._dev), idx=s.n)
        return json.dumps({"hits": hits, "n": s.n,
                          "device": s._dev, "footer": footer,
                          "budget_pct": result.get("budget_pct") if isinstance(result, dict) else None,
                          "scan_actual_pct": result.get("scan_actual_pct") if isinstance(result, dict) else None},
                         ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False, indent=1)


_stats_window = {"since": time.time()}


@mcp.tool()
def ann_search_stats(consume: bool = True, all: bool = False,
                     flush: bool = False) -> str:
    """Per-turn log for the fast search: query count, hits, latency
    (p50/p95/p99, embed vs scan), kinds usage, device. The closing line is a
    graft-style tally: who used the system this turn, and recall/speed.

    IMPORTANT: print the 'closing_log' field verbatim as the LAST line of your
    reply whenever you call this tool.

    consume=true (default) reports only queries since the previous call, then
    advances the window — so one call at the end of each turn is the turn's
    footer. consume=false reports the cumulative log. all=true ignores the
    window. flush=true also appends the log to JSONL and clears it."""
    try:
        s = _get_gpu_search()
        if all or not consume:
            since = None
        else:
            since = _stats_window["since"]
        out = {"closing_log": s.closing_log(since=since),
               "stats": s.stats(since=since)}
        if consume and not all:
            _stats_window["since"] = time.time()
        if flush:
            out["flushed"] = s.flush_log()
        return json.dumps(out, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False, indent=1)


@mcp.tool()
def ann_health() -> str:
    """Health of the ANN index server: entry count, index age, embedder status.
    Check this when ann_search fails — the embed sidecar (:18095) can be down
    while the index itself still loads."""
    return json.dumps(_call("GET", "/health"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser()
    _parser.add_argument("--http", action="store_true",
                        help="Run as a persistent HTTP MCP service (streamable-http).")
    _parser.add_argument("--host", default="127.0.0.1")
    _parser.add_argument("--port", type=int, default=8205)
    _args = _parser.parse_args()
    if _args.http:
        # Persistent service: load index synchronously at startup (once).
        try:
            _get_gpu_search()._ensure_loaded()
            sys.stderr.write("[ann-mcp] index loaded (http mode)\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[ann-mcp] load failed: {e}\n")
            sys.stderr.flush()
        mcp.settings.host = _args.host
        mcp.settings.port = _args.port
        mcp.run("streamable-http")
    else:
        # stdio mode (per-session): pre-warm in background.
        try:
            import threading as _th
            def _warm():
                try:
                    _get_gpu_search()._ensure_loaded()
                    sys.stderr.write("[ann-mcp] index pre-warmed\n")
                    sys.stderr.flush()
                except Exception as e:
                    sys.stderr.write(f"[ann-mcp] pre-warm failed: {e}\n")
                    sys.stderr.flush()
            _th.Thread(target=_warm, daemon=True).start()
        except Exception:
            pass
        mcp.run("stdio")
