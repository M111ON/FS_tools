"""
store.py — persistent state so the pipeline can be re-run on a growing folder
of exported chat logs without redoing work:

  - files table: path, file_hash, mtime, status (processed/skipped/error)
  - turns table: turn_hash -> (first file, first index) — dedupe fork content
  - facts table: extracted raw facts ready for the LLM synthesis pass

All keyed by content hash, not filename, so renamed/re-exported files are
still recognized as duplicates.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    pattern TEXT,
    turn_count INTEGER,
    processed_at REAL,
    status TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    turn_hash TEXT PRIMARY KEY,
    first_file TEXT,
    first_index INTEGER
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT,
    pattern TEXT,
    term_hint TEXT,
    content TEXT,
    kind TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_file);
"""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="replace")).hexdigest()


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- file-level ----
    def file_already_processed(self, path: str) -> bool:
        h = file_hash(path)
        row = self.conn.execute(
            "SELECT file_hash FROM files WHERE path=?", (path,)
        ).fetchone()
        return row is not None and row[0] == h

    def mark_file(self, path: str, pattern: str, turn_count: int, status: str = "processed"):
        h = file_hash(path)
        self.conn.execute(
            "INSERT INTO files (path, file_hash, pattern, turn_count, processed_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET file_hash=excluded.file_hash, "
            "pattern=excluded.pattern, turn_count=excluded.turn_count, "
            "processed_at=excluded.processed_at, status=excluded.status",
            (path, h, pattern, turn_count, time.time(), status),
        )
        self.conn.commit()

    # ---- turn-level dedup (handles fork overlap across files) ----
    def turn_seen(self, text: str) -> bool:
        h = content_hash(text)
        row = self.conn.execute(
            "SELECT 1 FROM turns WHERE turn_hash=?", (h,)
        ).fetchone()
        return row is not None

    def record_turn(self, text: str, file: str, index: int):
        h = content_hash(text)
        self.conn.execute(
            "INSERT OR IGNORE INTO turns (turn_hash, first_file, first_index) VALUES (?, ?, ?)",
            (h, file, index),
        )

    def commit(self):
        self.conn.commit()

    # ---- facts ----
    def add_fact(self, source_file: str, pattern: str, content: str,
                 kind: str, term_hint: Optional[str] = None):
        self.conn.execute(
            "INSERT INTO facts (source_file, pattern, term_hint, content, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_file, pattern, term_hint, content, kind, time.time()),
        )

    def export_facts_jsonl(self, out_path: str):
        rows = self.conn.execute(
            "SELECT source_file, pattern, term_hint, content, kind FROM facts ORDER BY id"
        ).fetchall()
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "source_file": r[0],
                    "pattern": r[1],
                    "term_hint": r[2],
                    "content": r[3],
                    "kind": r[4],
                }, ensure_ascii=False) + "\n")
        return len(rows)

    def stats(self):
        f = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        t = self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        fa = self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return {"files": f, "unique_turns": t, "facts": fa}
