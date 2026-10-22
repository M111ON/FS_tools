#!/usr/bin/env python3
"""Hermes Agent Live Monitor — reads state.db WAL, prints last N messages."""
import sqlite3, time, sys, os, json

DB = os.path.expandvars(r"I:\hermes\state.db")
LAST_ID = 0
ROWS = 20
INTERVAL = 2

def get_session_id(conn):
    r = conn.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    return r[0] if r else None

def fetch_messages(conn, session_id, since_id):
    rows = conn.execute(
        "SELECT m.id, m.role, m.tool_name, m.content, m.tool_calls "
        "FROM messages m WHERE m.session_id=? AND m.id > ? "
        "ORDER BY m.timestamp ASC",
        (session_id, since_id),
    ).fetchall()
    return rows

def format_msg(row):
    mid, role, tool_name, content, tool_calls = row
    ts = ""
    # extract time from id if possible, else just show role
    if role == "user":
        c = (content or "")[:100].replace("\n", " ")
        return f"  YOU: {c}"
    elif role == "tool":
        c = (content or "")[:120].replace("\n", " ")
        tn = tool_name or "?"
        return f"  [{tn}] {c}"
    elif role == "assistant":
        # assistant messages: content might be empty, tool_calls has the info
        if tool_calls:
            try:
                tc = json.loads(tool_calls)
                if isinstance(tc, list) and len(tc) > 0:
                    names = [t.get("function", {}).get("name", "?") for t in tc]
                    return f"  ME -> {', '.join(names)}"
            except:
                pass
        if content:
            c = content[:150].replace("\n", " ")
            return f"  ME: {c}"
        return None  # skip empty assistant
    return None

def main():
    global LAST_ID
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    session_id = get_session_id(conn)
    if not session_id:
        print("No active session")
        return

    # initial load: last N messages
    rows = conn.execute(
        "SELECT m.id, m.role, m.tool_name, m.content, m.tool_calls "
        "FROM messages m WHERE m.session_id=? "
        "ORDER BY m.timestamp DESC LIMIT ?", (session_id, ROWS)
    ).fetchall()
    rows.reverse()
    if rows:
        LAST_ID = rows[-1][0]
        print(f"Session: {session_id}  (showing last {len(rows)} messages)")
        print("-" * 60)
        for r in rows:
            line = format_msg(r)
            if line:
                print(line)
        print("-" * 60)
        print(f"Live (polling every {INTERVAL}s, Ctrl+C to quit)...")

    # live tail
    while True:
        time.sleep(INTERVAL)
        new_rows = fetch_messages(conn, session_id, LAST_ID)
        if new_rows:
            for r in new_rows:
                LAST_ID = max(LAST_ID, r[0])
                line = format_msg(r)
                if line:
                    print(line)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
