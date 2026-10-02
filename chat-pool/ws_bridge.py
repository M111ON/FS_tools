"""ws_bridge.py — chat-pool <-> cloud-memory workspace pool bridge.

Two directions, no worker changes (reuse the existing inbox routes):

A) chat -> workspace: live POST /append lines whose text mentions
   @<ws-id-prefix> (>=6 hex chars of an ACTIVE workspace id) are forwarded
   into that workspace's inbox with title "[chat-pool] ..." .
B) workspace -> chat: a poller (started by pool_server.main) walks every
   ACTIVE workspace inbox and appends NEW messages to the feed as
   app="ws-pool" kind="bridge".

Echo guard: direction-A forwards carry the "[chat-pool]" title prefix, and
direction B skips them — otherwise our own forward comes straight back into
the feed as a duplicate.

State: .wsbridge_state.json (cursor per workspace + processed-id map).
First sight of a workspace seeds at its newest message — no history replay.
Local-first: forward/poll failures print and continue, the feed never blocks.
The chat->memory direction (pool lines -> ctx-memory) is pool.memory_bridge,
a different bridge — this file only joins chat-pool with workspace inboxes.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pool_cloud as pc

DIR = Path(__file__).resolve().parent
STATE = Path(os.environ.get("WSBRIDGE_STATE", str(DIR / ".wsbridge_state.json")))
WORKER = os.environ.get("CLOUD_MEMORY_URL",
                        "https://cloud-memory-worker.aexid03.workers.dev")
POLL_S = int(os.environ.get("WSBRIDGE_POLL_S", "120"))
MIN_PREFIX = 6
ECHO = "[chat-pool]"
DONE_TTL_S = 7 * 86400
MENTION = re.compile(r"@([0-9a-fA-F]{%d,})" % MIN_PREFIX)

_lock = threading.Lock()
_cache = {"t": 0.0, "ws": None}


def _http(method, path, payload=None, timeout=20):
    key = pc._key()
    headers = {"Content-Type": "application/json", "X-API-Key": key,
               "User-Agent": "chat-pool/1.0"}  # CF 1010-blocks the default python UA
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(WORKER + path, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _active():
    """[(id, name)] of ACTIVE workspaces, cached 60s."""
    now = time.time()
    with _lock:
        if _cache["ws"] is not None and now - _cache["t"] < 60:
            return _cache["ws"]
    rows = _http("GET", "/workspace/list?status=active").get("workspaces") or []
    ws = [(w["id"], w.get("name") or w["id"][:8]) for w in rows]
    with _lock:
        _cache["t"], _cache["ws"] = now, ws
    return ws


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"ws": {}, "done": {}}


def _save(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def forward(app, kind, text, to="all"):
    """Direction A — called by pool_server right after a LIVE /append lands."""
    if kind == "bridge" or not text:
        return 0
    hits = {m.lower() for m in MENTION.findall(text)}
    if not hits:
        return 0
    n = 0
    act = _active()
    for ws_id, ws_name in act:
        for h in list(hits):
            if not ws_id.startswith(h):
                continue
            print(f"ws-bridge: forward {app} -> {ws_id[:8]} (hits={hits}, active={len(act)})")
            _http("POST", f"/workspace/{ws_id}/inbox", {
                "from_agent": str(app)[:100],
                "type": "message",
                "title": f"{ECHO} mention @{app}"[:200],
                "body": f"{ECHO} {app} (chat-pool): {text}"[:4000],
            })
            hits.discard(h)
            n += 1
    if n == 0:
        print(f"ws-bridge: forward {app} -> NO MATCH (hits={hits})")
    return n


def pull_once():
    """Direction B — poll every active workspace inbox, emit new to feed."""
    import pool
    st = _load()
    now_ms = time.time() * 1000
    done = {k: v for k, v in st.get("done", {}).items()
            if v > now_ms - DONE_TTL_S * 1000}
    emitted = 0
    for ws_id, ws_name in _active():
        try:
            msgs = _http("GET", f"/workspace/{ws_id}/inbox").get("messages") or []
        except Exception as e:
            print(f"ws-bridge: inbox {ws_id[:8]} FAIL {e}")
            continue
        if not msgs:
            continue
        msgs.sort(key=lambda m: m.get("created_at") or 0)
        cur = st["ws"].get(ws_id)
        if cur is None:
            cur = msgs[-1].get("created_at") or int(now_ms)
        for m in msgs:
            ca = m.get("created_at") or 0
            mid = str(m.get("id"))
            if ca <= cur or mid in done:
                continue
            done[mid] = ca
            title = (m.get("title") or "").strip()
            if title.startswith(ECHO):
                continue          # our own direction-A forward — don't echo
            line = f"[ws:{ws_name}] {m.get('from_agent') or '?'}"
            if m.get("to_agent"):
                line += f" -> {m['to_agent']}"
            if title:
                line += f" | {title}"
            body = (m.get("body") or "").strip()
            if body:
                line += f": {body}"
            pool.append("ws-pool", "bridge", line[:1800])
            emitted += 1
        st["ws"][ws_id] = msgs[-1].get("created_at") or cur
    st["done"] = done
    _save(st)
    return emitted


def loop():
    """Poller thread — 1 poll / ws / POLL_S (120s default: ~2M D1 rows/day)."""
    time.sleep(5)
    while True:
        try:
            n = pull_once()
            if n:
                print(f"ws-bridge: {n} workspace inbox message(s) -> feed")
        except Exception as e:
            print(f"ws-bridge: pull FAIL {e}")
        time.sleep(POLL_S)
