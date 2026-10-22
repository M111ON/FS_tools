"""import_chat_drop.py — import I:/tools/chat_drop/conversations.json into pool.

Only REQUEST (user) + RESPONSE (assistant) fragments. THINK/SEARCH/FILE/TOOL_* skipped.
Append-only via pool.append() — never wipes the live log.

Usage:
  python import_chat_drop.py --dry-run   # count only, no writes
  python import_chat_drop.py             # append for real
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

SRC = r"I:\tools\chat_drop\conversations.json"
APP = "chatdrop"
CHUNK = 1000  # chars; serialized line stays under pool._prev()'s 4096-byte tail


def collect():
    d = json.load(open(SRC, encoding="utf-8"))
    msgs = []
    for c in d:
        title = (c.get("title") or "untitled")[:60]
        cid = (c.get("id") or "")[:8]
        for v in (c.get("mapping") or {}).values():
            msg = v.get("message") or {}
            ts = msg.get("inserted_at") or ""
            for f in msg.get("fragments") or []:
                t = f.get("type")
                if t == "REQUEST":
                    role = "user"
                elif t == "RESPONSE":
                    role = "ai"
                else:
                    continue
                text = (f.get("content") or "").strip()
                if len(text) < 2:
                    continue
                msgs.append((ts, title, cid, role, text))
    msgs.sort(key=lambda m: m[0])
    return msgs


def chunks(text):
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [text]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    msgs = collect()
    total_chunks = sum(len(chunks(m[4])) for m in msgs)
    print(f"conversations: {len(set(m[2] for m in msgs))}")
    print(f"messages: {len(msgs)} -> {total_chunks} chunks")
    if args.dry_run:
        return

    n = skip = 0
    for ts, title, cid, role, text in msgs:
        parts = chunks(text)
        for i, part in enumerate(parts):
            head = f"[{title}#{cid}] ({role}" + (f" {i + 1}/{len(parts)}" if len(parts) > 1 else "") + f") {ts}\n"
            try:
                pool.append(APP, "CHAT", head + part)
                n += 1
            except Exception as exc:  # noqa: BLE001
                skip += 1
                if skip <= 3:
                    print(f"  skip: {exc}")
    print(f"appended {n} (skipped {skip})")


if __name__ == "__main__":
    main()
