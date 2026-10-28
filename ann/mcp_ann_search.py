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
import urllib.error
import urllib.request

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


@mcp.tool()
def ann_health() -> str:
    """Health of the ANN index server: entry count, index age, embedder status.
    Check this when ann_search fails — the embed sidecar (:18095) can be down
    while the index itself still loads."""
    return json.dumps(_call("GET", "/health"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run("stdio")
