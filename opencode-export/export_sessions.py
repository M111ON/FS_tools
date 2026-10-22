#!/usr/bin/env python3
"""export_sessions.py — dump opencode sessions from opencode.db to {info, messages} JSON.

Same shape as `opencode export <sessionID>` (what export_session_md.ps1 expects).
Tolerates a partially-corrupt DB: skips unreadable rows, exports the rest.

Usage:
    python export_sessions.py [--db PATH] [--out DIR] [--since MS] [--full]

Incremental by default: only sessions with time_updated >= last-run marker
(<out>/.last_export). Pass --full to re-export everything.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

DEF_DB = "I:/OpenCode-data/opencode.db"
DEF_OUT = "I:/.vault/exported_session"
STATE = ".last_export"
CHUNK = 500


def connect(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.execute("pragma query_only = on")
    return c


def read_sessions(c):
    """Return list of session-row dicts; skips pages that fail to read."""
    rows = []
    try:
        cur = c.execute("select * from session order by time_updated")
        cols = [d[0] for d in cur.description]
        while True:
            try:
                batch = cur.fetchmany(CHUNK)
            except sqlite3.DatabaseError as e:
                print(f"WARN: skipping unreadable session pages: {e}", flush=True)
                break
            if not batch:
                break
            rows.extend(dict(zip(cols, r)) for r in batch)
        return rows
    except sqlite3.DatabaseError as e:
        print(f"WARN: ordered scan failed ({e}), salvaging via workspace index", flush=True)
    try:
        ids = [r[0] for r in
               c.execute("select id from session indexed by session_workspace_idx")]
    except sqlite3.DatabaseError as e:
        print(f"WARN: index salvage failed: {e}", flush=True)
        return rows
    cols = [d[0] for d in c.execute("select * from session limit 0").description]
    for sid in ids:
        try:
            r = c.execute("select * from session where id = ?", (sid,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if r:
            rows.append(dict(zip(cols, r)))
    rows.sort(key=lambda r: r.get("time_updated") or 0)
    return rows


def dump_session(c, s):
    sid = s["id"]
    msgs = []
    try:
        mrows = c.execute(
            "select id, data, time_created from message where session_id = ? order by time_created",
            (sid,)).fetchall()
    except sqlite3.DatabaseError as e:
        print(f"WARN: {sid}: cannot read messages: {e}", flush=True)
        return None
    for mid, mdata, _ in mrows:
        try:
            minfo = json.loads(mdata)
        except (TypeError, ValueError):
            continue
        parts = []
        try:
            prows = c.execute(
                "select data from part where message_id = ? order by time_created",
                (mid,)).fetchall()
        except sqlite3.DatabaseError:
            prows = []
        for (pdata,) in prows:
            try:
                parts.append(json.loads(pdata))
            except (TypeError, ValueError):
                continue
        msgs.append({"info": minfo, "parts": parts})
    return {
        "info": {"id": sid, "title": s.get("title"),
                 "time": {"created": s.get("time_created"),
                          "updated": s.get("time_updated")}},
        "messages": msgs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--since", type=int, default=None)
    ap.add_argument("--until", type=int, default=None)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    state = out / STATE
    since = a.since
    if since is None and not a.full and state.exists():
        try:
            since = int(state.read_text().strip())
        except ValueError:
            since = None
    since = since or 0

    c = connect(a.db)
    sessions = read_sessions(c)
    print(f"sessions readable: {len(sessions)}, since={since}", flush=True)

    done, skipped, partial = 0, 0, 0
    newest = since
    until = a.until if a.until else 10 ** 18
    for s in sessions:
        upd = s.get("time_updated") or 0
        if upd < since or upd > until:
            continue
        newest = max(newest, upd)
        try:
            data = dump_session(c, s)
        except sqlite3.DatabaseError as e:
            print(f"WARN: {s.get('id')}: {e}", flush=True)
            skipped += 1
            continue
        if data is None:
            skipped += 1
            continue
        if not data["messages"]:
            partial += 1
        (out / f"{s['id']}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        done += 1

    state.write_text(str(max(newest, int(time.time() * 1000))))
    print(f"exported={done} empty/partial={partial} skipped={skipped}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
