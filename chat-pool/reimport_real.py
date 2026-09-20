"""reimport_real.py — wipe log.jsonl and re-import ONLY real data.

No synthetic seed, no hand-picked rkeys. Every entry goes through
``pool.append()`` so ``app_rkey()`` derives its torus cell naturally
from (content, app) — the plane fills with whatever the real corpus
happens to hash to.

Sources (all genuine project data):
  A) cloud-workspace/pool.db      chunks / facts / sessions
  B) cloud_ws/mcp/local.db        workspace state rows
  C) pre_embedding_filter/facts.jsonl        session-derived facts
  D) pre_embedding_filter/dictionary.jsonl   term definitions

Usage:
  python reimport_real.py            # wipe + import
  python reimport_real.py --dry-run  # measure only, no writes
  python reimport_real.py --keep     # import without wiping

The last JSONL line is re-read by pool._prev() with a 4096-byte tail
window, so each text field is capped well below that (MAX_TEXT).
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

TOOLS = os.path.dirname(HERE)
LOG_PATH = os.path.join(HERE, "log.jsonl")

# Keep serialized lines comfortably under pool._prev()'s 4096-byte tail.
MAX_TEXT = 1200
GENESIS_TEXT = "log reimported — real corpus only, cells from app_rkey()"


# ── sources ──────────────────────────────────────────────────────────────
def _app_from_session(session_id, default="hermes"):
    s = (session_id or "").lower()
    for name in ("opencode", "codebuddy", "freebuff", "hermes"):
        if name in s:
            return name
    return default


def src_cloud_pool_db():
    """cloud-workspace/pool.db — chunks, facts, sessions."""
    path = os.path.join(TOOLS, "cloud-workspace", "pool.db")
    if not os.path.exists(path):
        return
    db = sqlite3.connect(path)
    for text, session_id, _topic in db.execute(
            "SELECT text, session_id, topic FROM chunks ORDER BY rowid"):
        if text and len(str(text).strip()) >= 10:
            yield _app_from_session(session_id), "CHAT", str(text).strip()[:MAX_TEXT]
    for key, value, _cat, source in db.execute(
            "SELECT key, value, category, source FROM facts ORDER BY rowid"):
        if value:
            yield (source or "hermes"), "DECISION", f"DECISION: {key}\n{str(value)[:MAX_TEXT]}"
    for sid, summary, label in db.execute(
            "SELECT id, summary, label FROM sessions ORDER BY rowid"):
        if summary:
            yield "freebuff", "CHAT", f"SESSION: {sid} ({label})\n{summary}"[:MAX_TEXT]
    db.close()


def src_cloud_ws_local_db():
    """cloud_ws/mcp/local.db — workspace state rows with real content."""
    path = os.path.join(TOOLS, "cloud_ws", "mcp", "local.db")
    if not os.path.exists(path):
        return
    db = sqlite3.connect(path)
    for ws_id, category, key, value in db.execute(
            "SELECT workspace_id, category, key, value FROM state ORDER BY rowid"):
        value = (value or "").strip()
        if len(value) < 12:
            continue
        yield "dashboard", "CHAT", f"WS {category}/{key}: {value}"[:MAX_TEXT]
    db.close()


def src_pre_embedding_facts():
    """pre_embedding_filter/facts.jsonl — real session-derived content."""
    path = os.path.join(TOOLS, "pre_embedding_filter", "facts.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (row.get("content") or "").strip()
            if len(content) < 20:
                continue
            hint = row.get("term_hint") or row.get("pattern") or "fact"
            yield "opencode", "CHAT", f"{hint}: {content}"[:MAX_TEXT]


def src_pre_embedding_dict():
    """pre_embedding_filter/dictionary.jsonl — real term definitions."""
    path = os.path.join(TOOLS, "pre_embedding_filter", "dictionary.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            term = (row.get("term") or "").strip()
            definition = (row.get("definition") or "").strip()
            if not term or len(definition) < 20:
                continue
            yield "hermes", "DECISION", f"DECISION: {term}\n{definition}"[:MAX_TEXT]


SOURCES = [
    ("cloud-workspace/pool.db", src_cloud_pool_db),
    ("cloud_ws/mcp/local.db", src_cloud_ws_local_db),
    ("pre_embedding_filter/facts.jsonl", src_pre_embedding_facts),
    ("pre_embedding_filter/dictionary.jsonl", src_pre_embedding_dict),
]


# ── engine ───────────────────────────────────────────────────────────────
def wipe():
    if os.path.exists(LOG_PATH):
        bak = LOG_PATH + ".bak." + time.strftime("%Y%m%d-%H%M%S")
        os.rename(LOG_PATH, bak)
        print(f"backed up old log → {os.path.basename(bak)}")


def write_genesis():
    body = {
        "v": 3, "t": time.time(), "app": "system", "kind": "CHAT",
        "text": GENESIS_TEXT, "to": "all", "tags": [], "prev": "",
        "rkey": pool.rkey(b"reimport-genesis"),
        "enc": pool.rkey(b"reimport-genesis") % 1440,
    }
    body["hash"] = hashlib.sha256(
        ("" + "system" + "CHAT" + "all" + GENESIS_TEXT).encode("utf-8")).hexdigest()
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(body, ensure_ascii=False) + "\n")


def measure():
    lines, cells, apps = [], {}, {}
    with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            lines.append(m)
            rk = m.get("rkey")
            if rk is None:
                continue
            cell = (rk % 144, rk // 144)
            cells[cell] = cells.get(cell, 0) + 1
            apps.setdefault(m.get("app", "?"), []).append(cell)
    n = len(cells)
    total = len(lines)
    print(f"\ntotal lines   : {total}")
    print(f"unique cells  : {n}/20736 ({100.0 * n / 20736:.1f}%)")
    if n:
        diag = sum(1 for x, y in cells if abs(x - y) < 3)
        edgey = sum(1 for x, y in cells if y < 5 or y > 138)
        center = sum(1 for x, y in cells if 60 <= x <= 89 and 70 <= y <= 89)
        print(f"diagonal x≈y  : {100.0 * diag / n:.1f}%  (uniform ≈ 6.9%)")
        print(f"edge-y        : {100.0 * edgey / n:.1f}%  (uniform ≈ 6.9%)")
        print(f"center 60-89  : {100.0 * center / n:.1f}%  (uniform ≈ 6.9%)")
    print("\nper-app scatter:")
    for app, pts in sorted(apps.items(), key=lambda kv: -len(kv[1])):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        print(f"  {app:<14} {len(pts):>4} lines  x:{min(xs):>3}-{max(xs):<3}  y:{min(ys):>3}-{max(ys):<3}")
    return n, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="count sources only")
    ap.add_argument("--keep", action="store_true", help="do not wipe the log")
    args = ap.parse_args()

    if args.dry_run:
        for name, fn in SOURCES:
            print(f"{name}: {sum(1 for _ in fn())} entries")
        return

    if not args.keep:
        wipe()
        write_genesis()

    grand = 0
    for name, fn in SOURCES:
        added = skipped = 0
        for app, kind, text in fn():
            try:
                pool.append(app, kind, text)
                added += 1
            except Exception as exc:  # noqa: BLE001 — report and continue
                skipped += 1
                if skipped <= 3:
                    print(f"  skip ({name}): {exc}")
        grand += added
        print(f"{name}: +{added} (skipped {skipped})")

    print(f"\nimported {grand} real entries")
    measure()


if __name__ == "__main__":
    main()
