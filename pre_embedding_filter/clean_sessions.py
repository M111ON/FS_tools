#!/usr/bin/env python3
"""Batch-clean exported session .md files — v2 approach.
Parse with converter-style role detection, strip tool I/O from assistant content."""
import re, sys, os
from pathlib import Path
from typing import List, Dict, Optional

INPUT_DIR = Path(r"I:\.vault\exported_session")
OUTPUT_DIR = INPUT_DIR / "cleaned"
OUTPUT_DIR.mkdir(exist_ok=True)

# ---- role detection (## User / ## Assistant (model · time)) ----
ROLE_RE = re.compile(r"^##\s+(User|Assistant)\b(.*)$", re.MULTILINE)
METADATA_RE = re.compile(r"^\*\*(Session ID|Created|Updated):\*\*\s*.+$", re.MULTILINE)
SEPARATOR_RE = re.compile(r"^---\s*$", re.MULTILINE)

# ---- tool I/O noise to strip from assistant turns ----
# "Called the X tool..." inline echo with optional <path>/<type>/<content> tags
TOOL_CALL_ECHO_RE = re.compile(
    r"Called the \w+ tool with the following input:[^\r\n]*"
    r"(?:<path>.*?</path>)?"
    r"(?:\r?\n<type>[^<\r\n]*</type>)?"
    r"(?:\r?\n<content>.*?</content>)?",
    re.DOTALL,
)
# <content>...</content> nested blocks
CONTENT_TAG_RE = re.compile(r"<content>.*?</content>", re.DOTALL)
PATH_TAG_RE = re.compile(r"<path>[^<]*</path>")
TYPE_TAG_RE = re.compile(r"<type>[^<]*</type>")

# **Tool: name** block through to next ## header or **Tool:**
TOOL_IO_BLOCK_RE = re.compile(
    r"\*\*Tool:.*?\n.*?(?=\n## |\n\*\*Tool:|\Z)",
    re.DOTALL,
)
# _Thinking:_ block
THINKING_RE = re.compile(r"_Thinking:_(?:\n(?!## ).*)*", re.MULTILINE)
# "Full output saved to: ..." 
FULL_OUTPUT_RE = re.compile(r"Full output saved to:.*$", re.MULTILINE)
# ANSI escape codes
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def strip_tool_noise(text: str) -> str:
    """Strip tool I/O machinery from assistant content."""
    # Strip ANSI first so other patterns can match clean text
    text = ANSI_RE.sub("", text)
    # Strip line-number prefixes from tool output (e.g. "135: ## Assistant")
    text = re.sub(r"^\d+:\s?", "", text, flags=re.MULTILINE)
    text = TOOL_CALL_ECHO_RE.sub("", text)
    while CONTENT_TAG_RE.search(text):
        text = CONTENT_TAG_RE.sub("", text)
    text = PATH_TAG_RE.sub("", text)
    text = TYPE_TAG_RE.sub("", text)
    text = TOOL_IO_BLOCK_RE.sub("", text)
    text = THINKING_RE.sub("", text)
    text = FULL_OUTPUT_RE.sub("", text)
    return text


def parse_session(content: str) -> Dict:
    """Parse session .md into turns by ## User / ## Assistant headers."""
    # Strip metadata header
    content = METADATA_RE.sub("", content)
    content = SEPARATOR_RE.sub("", content)
    
    # Find all role markers
    markers = list(ROLE_RE.finditer(content))
    if not markers:
        return {"turns": []}
    
    turns = []
    for i, m in enumerate(markers):
        speaker = m.group(1)  # User or Assistant
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        body = content[start:end].strip()
        if body:
            turns.append({"role": speaker, "content": body})
    
    return {"turns": turns}


def clean_session(content: str) -> str:
    """Full clean pipeline."""
    # Remove tool noise from entire text first (some tags span turns)
    content = strip_tool_noise(content)
    
    session = parse_session(content)
    lines = []
    for t in session["turns"]:
        role = t["role"]
        body = t["content"].strip()
        if not body:
            continue
        lines.append(f"## {role}\n")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


files = sorted(INPUT_DIR.glob("session-*.md"))
FORCE = "--force" in sys.argv

# ---- low-value session filter ----
# Sessions with fewer than MSG_MIN messages AND fewer than TOK_MIN tokens are
# skipped: they are empty boots, connection-tests, or magic-context-compartment
# stubs that add noise without useful content.
MSG_MIN = 6
TOK_MIN = 10_000

# Build a fast lookup {short_id -> (msg_count, token_count)} from opencode.db.
# Keyed by first 11 chars of session id ("ses_XXXXXXX") to match filenames.
_session_stats: dict = {}
try:
    import sqlite3 as _sqlite3
    _db = _sqlite3.connect("file:I:/Opencode-data/opencode.db?mode=ro", uri=True)
    for _r in _db.execute("""
        SELECT s.id, count(m.id),
               s.tokens_input + s.tokens_output + s.tokens_reasoning
        FROM session s LEFT JOIN message m ON m.session_id=s.id
        GROUP BY s.id
    """):
        _session_stats[_r[0][:11]] = (_r[1], _r[2] or 0)
    _db.close()
except Exception as _e:
    print(f"[warn] could not load session stats from opencode.db: {_e}")

def _is_low_value(stem: str) -> bool:
    """Return True if session should be skipped (too few messages + tokens)."""
    if not _session_stats:
        return False
    # stem e.g. "session-ses_XXXXXXX" — strip prefix to get short id
    inner = stem[len("session-"):]
    short = inner[:11]
    stats = _session_stats.get(short)
    if stats is None:
        return False
    msgs, tok = stats
    return msgs < MSG_MIN and tok < TOK_MIN

print(f"Found {len(files)} files" + ("" if FORCE else " (incremental: unchanged md skipped)"))
print(f"Low-value filter: msgs<{MSG_MIN} AND tok<{TOK_MIN} (known sessions: {len(_session_stats)})")

total_turns = 0
skipped = 0
skipped_low = 0
for i, f in enumerate(files, 1):
    if "_clean" in f.stem or "_debug" in f.stem:
        continue
    if _is_low_value(f.stem):
        skipped_low += 1
        continue
    out = OUTPUT_DIR / f"{f.stem}_clean.md"
    if not FORCE and out.exists() and out.stat().st_mtime >= f.stat().st_mtime:
        skipped += 1
        continue
    try:
        raw = f.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_session(raw)
        out.write_text(cleaned, encoding="utf-8")
        
        # Count turns
        turn_count = cleaned.count("## User") + cleaned.count("## Assistant")
        size = len(cleaned.encode("utf-8"))
        total_turns += turn_count
        print(f"[{i}/{len(files)}] {f.stem} -> {size//1024}KB, {turn_count} turns")
    except Exception as e:
        print(f"[{i}/{len(files)}] {f.stem} ERROR: {e}")

print(f"\nTotal: {total_turns} turns across {len(files)} files (skipped {skipped} unchanged, {skipped_low} low-value)")
