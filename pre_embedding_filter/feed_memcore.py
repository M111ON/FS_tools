#!/usr/bin/env python3
"""Feed magic-context context.db memories into facts.jsonl for the memcore pipeline.

Backfills memories that map-memories will never file-map (no file provenance),
so they stay searchable via memcore_query instead of dying in the map backlog.
Idempotent: state in fed_memcore.json tracks fed memory ids.
"""
import json
import os
import sqlite3
import sys

CTX_DB = r"file:I:/cortexkit/magic-context/context.db?mode=ro"
FACTS = r"I:\tools\pre_embedding_filter\facts.jsonl"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fed_memcore.json")
CATS = ("ARCHITECTURE", "PROJECT_RULES", "CONSTRAINTS", "CONFIG_VALUES", "NAMING")


def load_state():
    try:
        return set(json.load(open(STATE, encoding="utf-8")))
    except Exception:
        return set()


def save_state(ids):
    json.dump(sorted(ids), open(STATE, "w"), indent=0)


def main():
    dry = "--dry" in sys.argv
    fed = load_state()
    db = sqlite3.connect(CTX_DB, uri=True)
    import time
    rows = db.execute(
        f"""SELECT id, project_path, category, created_at, content FROM memories
            WHERE status IN ('active','permanent')
              AND category IN ({",".join("?" * len(CATS))})
              AND (expires_at IS NULL OR expires_at > ?)""",
        (*CATS, time.time() * 1000),
    ).fetchall()
    db.close()

    fresh = [r for r in rows if r[0] not in fed]
    if not fresh:
        print(f"nothing new ({len(rows)} total live, {len(fed)} fed)")
        return 0
    print(f"{len(fresh)} new / {len(rows)} live")
    if dry:
        for r in fresh[:10]:
            print(" ", r[0], r[2], str(r[4])[:80].replace("\n", " "))
        return 0

    with open(FACTS, "a", encoding="utf-8") as f:
        for mid, proj, cat, created, content in fresh:
            first = content.strip().splitlines()[0][:120] if content.strip() else ""
            f.write(json.dumps({
                "source_file": f"context.db://{proj}",
                "pattern": cat.lower(),
                "term_hint": first,
                "content": content,
                "kind": "embedded_document",
            }, ensure_ascii=False) + "\n")
            fed.add(mid)
    save_state(fed)
    print(f"appended {len(fresh)} -> facts.jsonl (fed now {len(fed)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
