#!/usr/bin/env python3
"""hermes_mc_sync.py — push Hermes session history into CortexKit magic-context.

Why memories (and not raw transcripts): magic-context's *only* cross-session,
cross-harness recall channel is the `memories` table. OpenCode injects
`memories WHERE project_path = <identity> AND status IN ('active','permanent')`
into every turn, and `ctx_search` reads it project-wide. Compartments and the
message FTS index are session-scoped, so a Hermes session id is never reachable
from an OpenCode session. Memories are therefore where Hermes history has to go.

Contract this tool honours (verified against @cortexkit/opencode-magic-context
v0.43.2, dist/v2/server.js):
  * normalized_hash = md5( content.toLowerCase().replace(/\\s+/g," ").trim() )
    and UNIQUE(project_path, category, normalized_hash) is the dedupe key.
  * isMemoryRow() rejects a row unless:
      category   IN {PROJECT_RULES, ARCHITECTURE, CONSTRAINTS, CONFIG_VALUES,
                     NAMING, USER_DIRECTIVES, USER_PREFERENCES, CONFIG_DEFAULTS,
                     ARCHITECTURE_DECISIONS, ENVIRONMENT, WORKFLOW_RULES, KNOWN_ISSUES}
      status     IN {active, permanent, archived}
      scope      IN {project, ecosystem, universe}
      source_type IN {historian, agent, dreamer, user}
    A row failing any of these is silently dropped from injection. (The old MCP
    bridge wrote source_type='hermes', which is why its writes were invisible.)
  * project identity = `git:<root commit>` (git rev-list --max-parents=0 HEAD,
    sorted, first) or `dir:<md5(realpath)[:12]>` when the dir is not a repo.

Idempotent: each Hermes session maps to one row per (project, category), tracked
in metadata_json.hermes_session_id. Re-runs update in place (content change
rewrites normalized_hash; the memories_au trigger keeps memories_fts in sync).

Usage:
    python hermes_mc_sync.py --dry-run --days 30      # preview, writes nothing
    python hermes_mc_sync.py --apply --days 30        # write
    python hermes_mc_sync.py --apply --mode facts     # durable rules/decisions
    python hermes_mc_sync.py --apply --mirror-transcripts   # + raw FTS archive
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Hermes split its live session DB into per-profile databases under profiles/<name>/
# at the Oct-2026 update; the root state.db froze on 2026-10-09. We therefore read
# every Hermes DB we can find, not just the root one. HERMES_STATE_DB still pins a
# single DB when set (used by the MCP tool / tests).
HERMES_HOME = Path(os.environ.get("HERMES_HOME_ROOT", r"I:\Hermes"))
HERMES_DB_ENV = os.environ.get("HERMES_STATE_DB", "").strip()
CTX_DB = Path(os.environ.get("MAGIC_CONTEXT_DB", r"I:\cortexkit\magic-context\context.db"))
STATE = HERE / "hermes_mc_sync_state.json"


def discover_hermes_dbs() -> list[tuple[str, Path]]:
    """Return [(label, path)] for every Hermes session DB, live profiles included.

    Order: root state.db first (legacy), then profiles/*/state.db sorted by name.
    A missing file is skipped so an empty profile dir is harmless. When
    HERMES_STATE_DB is set the caller has pinned one DB and we return just it.
    """
    if HERMES_DB_ENV:
        return [("root", Path(HERMES_DB_ENV))]
    out: list[tuple[str, Path]] = []
    root = HERMES_HOME / "state.db"
    if root.exists():
        out.append(("root", root))
    prof_dir = HERMES_HOME / "profiles"
    if prof_dir.is_dir():
        for d in sorted(prof_dir.iterdir()):
            db = d / "state.db"
            if db.exists():
                out.append((d.name, db))
    return out

VALID_CATEGORIES = {
    "PROJECT_RULES", "ARCHITECTURE", "CONSTRAINTS", "CONFIG_VALUES", "NAMING",
    "USER_DIRECTIVES", "USER_PREFERENCES", "CONFIG_DEFAULTS",
    "ARCHITECTURE_DECISIONS", "ENVIRONMENT", "WORKFLOW_RULES", "KNOWN_ISSUES",
}
SOURCE_TYPE = "agent"          # valid per MEMORY_SOURCE_TYPE_LOOKUP
DEFAULT_DIGEST_CATEGORY = "ENVIRONMENT"

# ── identity ───────────────────────────────────────────────────────────────

def _dir_identity(path: str) -> str:
    canonical = os.path.realpath(path)
    return "dir:" + hashlib.md5(canonical.encode("utf-8")).hexdigest()[:12]


def _git_root_commit(start: str):
    cur = os.path.realpath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            try:
                r = subprocess.run(
                    ["git", "rev-list", "--max-parents=0", "HEAD"],
                    cwd=cur, capture_output=True, text=True, timeout=5,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                )
                if r.returncode == 0:
                    cands = sorted(
                        ln.strip()[:64] for ln in r.stdout.splitlines()
                        if re.fullmatch(r"[0-9a-f]{7,64}", ln.strip()[:64])
                    )
                    if cands:
                        return cands[0]
            except Exception:
                pass
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def resolve_identity(path: str | None) -> str | None:
    """Mirror the plugin's resolveProjectIdentity (git root commit, else dir hash)."""
    if not path:
        return None
    if not os.path.isdir(path):
        return None
    root = _git_root_commit(path)
    if root:
        return "git:" + root
    return _dir_identity(path)


# ── memory contract ────────────────────────────────────────────────────────

def normalize_content(content: str) -> str:
    return re.sub(r"\s+", " ", content.lower()).strip()


def normalized_hash(content: str) -> str:
    return hashlib.md5(normalize_content(content).encode("utf-8")).hexdigest()


def connect_ctx() -> sqlite3.Connection:
    if not CTX_DB.exists():
        raise SystemExit(f"context.db not found at {CTX_DB}")
    conn = sqlite3.connect(str(CTX_DB), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def find_existing(conn, project: str, category: str, session_id: str):
    row = conn.execute(
        "SELECT id, content, normalized_hash FROM memories "
        "WHERE project_path = ? AND category = ? "
        "AND metadata_json LIKE ? AND status != 'archived' LIMIT 1",
        (project, category, f'%"hermes_session_id": "{session_id}"%'),
    ).fetchone()
    return row


def upsert_memory(conn, *, project, category, content, session_id, importance,
                  scope, shareable, kind, dry_run):
    """Insert or update one Hermes-derived memory. Returns 'insert'|'update'|'skip'."""
    content = content.strip()
    if not content:
        return "skip"
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid category {category!r}")
    h = normalized_hash(content)
    meta = json.dumps({
        "hermes_session_id": session_id,
        "hermes_sync": True,
        "kind": kind,
    }, ensure_ascii=False)
    now = int(time.time() * 1000)

    existing = find_existing(conn, project, category, session_id)
    if existing is not None:
        if existing["normalized_hash"] == h:
            return "skip"
        if dry_run:
            return "update"
        try:
            conn.execute(
                "UPDATE memories SET content = ?, normalized_hash = ?, "
                "importance = ?, scope = ?, shareable = ?, metadata_json = ?, "
                "updated_at = ?, last_seen_at = ? WHERE id = ?",
                (content, h, importance, scope, shareable, meta, now, now, existing["id"]),
            )
            conn.commit()
            return "update"
        except sqlite3.IntegrityError:
            conn.rollback()
            return "skip"   # a plugin memory already owns this hash

    if dry_run:
        return "insert"
    try:
        conn.execute(
            "INSERT INTO memories (project_path, category, content, normalized_hash, "
            "importance, scope, shareable, source_type, seen_count, retrieval_count, "
            "first_seen_at, created_at, updated_at, last_seen_at, status, "
            "verification_status, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, 'active', 'unverified', ?)",
            (project, category, content, h, importance, scope, shareable,
             SOURCE_TYPE, now, now, now, now, meta),
        )
        conn.commit()
        return "insert"
    except sqlite3.IntegrityError:
        conn.rollback()
        return "skip"


def reap_memory(conn, *, project, category, session_id, dry_run) -> bool:
    """Delete a previously-synced row whose session no longer yields a digest.

    Needed because build_digest() now drops all-noise sessions: without this the
    old row would linger and keep injecting noise forever. Only rows this tool
    created are touched (matched on the hermes_session_id marker).
    """
    row = find_existing(conn, project, category, session_id)
    if row is None:
        return False
    if dry_run:
        return True
    try:
        conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        return False


# ── Hermes reader ──────────────────────────────────────────────────────────

def hermes_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else (discover_hermes_dbs() or [("root", Path(r"I:\Hermes\state.db"))])[0][1]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def load_sessions(conn, since_s: float, min_messages: int, sources_out):
    """started_at in Hermes state.db is Unix SECONDS (not ms).

    Selection keys on last_activity_at (falling back to started_at) rather than
    started_at alone: a session that began weeks ago but is still being used has
    new turns that must be re-digested, and started_at alone would exclude it
    forever. Re-runs are idempotent (upsert by (project, category, session_id)),
    so re-reading an active session is safe and only rewrites on content change.
    """
    q = ("SELECT id, title, source, cwd, git_repo_root, message_count, started_at "
         "FROM sessions WHERE message_count >= ? "
         "AND COALESCE(last_activity_at, started_at) >= ? "
         f"AND source NOT IN ({','.join('?' * len(sources_out))}) "
         "ORDER BY COALESCE(last_activity_at, started_at) ASC")
    return conn.execute(q, (min_messages, since_s, *sources_out)).fetchall()


def session_messages(conn, sid: str):
    return conn.execute(
        "SELECT role, content FROM messages "
        "WHERE session_id = ? AND role IN ('user','assistant') "
        "AND content IS NOT NULL AND length(content) > 0 ORDER BY id",
        (sid,),
    ).fetchall()


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# Harness-injected wrappers that ride along in user turns but are not the user's
# own words: out-of-band deliveries, background-process notices, XML system
# blocks. Including them would spend every future turn's injection budget on
# plumbing, so they are stripped before the digest is built.
INJECTED_RX = re.compile(
    r"^\s*(\[OUT-OF-BAND|\[IMPORTANT|\[System|\[Background|\[SYSTEM|<system-reminder|"
    r"<available-skills|<compartment_examples|<ctx-search-hint|<session-history|"
    r"\[Reminder|\[NOTE|\[Tool)",
    re.I,
)


def _clean_user_text(text: str) -> str:
    """Drop harness-injected blocks and inline markers; keep the user's words."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or INJECTED_RX.match(line):
            continue
        lines.append(line)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    # Strip trailing/leading bracketed harness markers that ride inline
    # (e.g. "... ใช้ tui ดีไหม [/OUT-OF-BAND USER MESSAGE]").
    out = re.sub(r"\s*\[/?[A-Z][A-Z0-9 _-]{4,60}\]\s*$", "", out).strip()
    out = re.sub(r"^\s*\[/?[A-Z][A-Z0-9 _-]{4,60}\]\s*", "", out).strip()
    return out


# Prompts that are UI gestures, not requests: session-open placeholders, ellipses,
# "continue"/"go"/"ok", bare menu picks. They carry no recallable intent but would
# otherwise be injected into every later turn, so they are dropped.
GESTURE_RX = re.compile(
    r"^\s*(\.+|…+|newsession|new\s*session|start|continue|cont|go|next|ok|okay|"
    r"done|yes|no|y|n|thanks?|ขอบคุณ|ต่อ|ไปต่อ|โอเค|ได้เลย|"
    r"\d+[.)]\s*[a-z_]{0,20})\s*$",
    re.I,
)

# Assistant endings that mean "nothing happened here" — interrupted, empty, or a
# bare acknowledgement. Not a result worth remembering.
EMPTY_OUTCOME_RX = re.compile(
    r"^(do|done|ok|okay|operation interrupted|interrupted|cancelled|canceled|"
    r"no response|n/?a|\.+|-+|understood|sure)\.?$",
    re.I,
)


def _is_gesture(text: str) -> bool:
    """True when a user prompt carries no recallable intent."""
    t = (text or "").strip()
    if len(t) < 4:
        return True
    return bool(GESTURE_RX.match(t))


def build_digest(sess, msgs) -> str:
    """One compact, human-readable activity record per Hermes session.

    Kept deliberately small: this text is injected into every OpenCode turn for
    the project, so it competes for a finite memory budget (4000 tokens by
    default, 12500 for DWGLS). Noise here is a recurring tax on all later work,
    so UI gestures, interrupted runs and empty outcomes are dropped outright.
    """
    users, assistants = [], []
    for m in msgs:
        if m["role"] == "user":
            t = _clean_user_text(m["content"])
            if t and not _is_gesture(t):
                users.append(t)
        elif m["role"] == "assistant":
            t = _clean_user_text(m["content"])
            if t:
                assistants.append(t)
    if not users and not assistants:
        return ""

    when = datetime.fromtimestamp(sess["started_at"], tz=timezone.utc).strftime("%Y-%m-%d")
    where = sess["git_repo_root"] or sess["cwd"] or "(no cwd)"
    title = (sess["title"] or "").strip()
    if _is_gesture(title) or re.fullmatch(r"[\s.…#\d-]*", title):
        title = ""

    asks, seen = [], set()
    for u in users:
        key = _clip(u, 90).lower()
        if key in seen:
            continue
        seen.add(key)
        asks.append(_clip(u, 130))
        if len(asks) >= 5:
            break

    # Last assistant message that carries a real result, skipping harness failure
    # notices and empty acknowledgements.
    outcome = ""
    for a in reversed(assistants):
        if re.search(r"request was not processed|no response|^error\b", a, re.I):
            continue
        if EMPTY_OUTCOME_RX.match(a.strip()):
            continue
        outcome = _clip(a, 320)
        break

    # A digest with neither a real ask nor a real outcome is not worth injecting.
    if not asks and not outcome:
        return ""

    lines = [f"Hermes work log — {when} — {where}"]
    if title:
        lines.append(f"Session: {_clip(title, 120)}")
    lines.append(f"({len(msgs)} turns, source={sess['source']}, id={sess['id']})")
    if asks:
        lines.append("User asked about:")
        lines.extend(f"- {a}" for a in asks)
    if outcome:
        lines.append(f"Outcome: {outcome}")
    return "\n".join(lines)


FACT_RULES = [
    ("PROJECT_RULES", re.compile(r"\b(rule|always|never|must|do not|don't|ห้าม|ต้อง|อย่า)\b", re.I)),
    ("CONSTRAINTS", re.compile(r"\b(constraint|limit|limitation|cannot|can't|ข้อจำกัด)\b", re.I)),
    ("CONFIG_VALUES", re.compile(r"\b(config|configuration|port|version|setting|env|path|ค่า(?:ตั้ง|กำหนด))\b", re.I)),
    ("NAMING", re.compile(r"\b(naming|name it|rename|called|ชื่อ)\b", re.I)),
    ("ARCHITECTURE", re.compile(r"\b(architecture|design|schema|pipeline|structure|สถาปัตยกรรม)\b", re.I)),
]

# Narration the assistant emits while working — not durable knowledge. Without
# this filter facts mode fills the memory block with "Let me check X" lines.
NOISE_RX = re.compile(
    r"^(let me|i'll|i will|i'm going to|now let me|next,? let|let's|first,? let|"
    r"i should|i need to|let me try|i'll start|i have|here'?s|"
    r"ตรวจสอบ|ลอง|ผมจะ|ฉันจะ|เริ่ม|ต่อไป|ให้ผม|ขอ)",
    re.I,
)


def classify_fact(text: str) -> str:
    for cat, rx in FACT_RULES:
        if rx.search(text):
            return cat
    return "ENVIRONMENT"


def extract_facts(sess, msgs, limit: int = 6):
    """Heuristic: pull durable-looking sentences out of assistant turns.

    Deliberately conservative — a noisy memory block costs every future turn,
    so narration, questions and hedges are dropped rather than risked.
    """
    out, seen = [], set()
    for m in msgs:
        if m["role"] != "assistant":
            continue
        for raw in re.split(r"(?<=[.!?])\s+|\n+", m["content"]):
            s = _clip(raw, 300)
            if len(s) < 40 or len(s) > 300:
                continue
            if NOISE_RX.search(s):
                continue
            if s.rstrip().endswith("?"):
                continue
            if not any(rx.search(s) for _, rx in FACT_RULES):
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((classify_fact(s), f"[Hermes {sess['id']}] {s}"))
            if len(out) >= limit:
                return out
    return out


# ── optional: mirror raw transcript into the message FTS archive ────────────

def mirror_transcript(conn, sess, msgs, project, dry_run) -> int:
    """Archive the raw transcript under a synthetic session id.

    Conversation search in magic-context is session-scoped, so these rows are not
    reachable from an OpenCode session via ctx_search today; they preserve the
    full history for ctx_expand / future project-wide search, and they are what
    the earlier `vault-export` import did.
    """
    sid = f"hermes_{sess['id']}"
    rows = []
    ordn = 0
    for m in msgs:
        ordn += 1
        content = re.sub(r"\s+", " ", m["content"]).strip()
        if not content:
            continue
        rows.append((sid, ordn, f"hermesmsg_{sess['id']}_{ordn}", m["role"], content))
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    now = int(time.time() * 1000)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM message_history_fts WHERE rowid IN "
                     "(SELECT fts_rowid FROM message_fts_rowid_map WHERE session_id = ?)", (sid,))
        conn.execute("DELETE FROM message_fts_rowid_map WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM message_history_source WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM message_history_index WHERE session_id = ?", (sid,))
        for sid_, ord_, mid_, role_, content_ in rows:
            h = hashlib.sha256(content_.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO message_history_source (session_id, message_id, message_ordinal, "
                "source_version, normalized_content_hash, role, harness, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'hermes', ?)",
                (sid_, mid_, ord_, f"string:{mid_}", h, role_, now),
            )
            cur = conn.execute(
                "INSERT INTO message_history_fts (session_id, message_ordinal, message_id, role, content) "
                "VALUES (?, ?, ?, ?, ?)", (sid_, ord_, mid_, role_, content_),
            )
            conn.execute(
                "INSERT OR REPLACE INTO message_fts_rowid_map "
                "(session_id, message_ordinal, fts_rowid, message_time_ms) VALUES (?, ?, ?, ?)",
                (sid_, ord_, cur.lastrowid, now),
            )
        conn.execute(
            "INSERT INTO message_history_index (session_id, last_indexed_ordinal, "
            "dirty_floor_ordinal, updated_at, harness) VALUES (?, ?, 0, ?, 'hermes') "
            "ON CONFLICT(session_id) DO UPDATE SET last_indexed_ordinal = excluded.last_indexed_ordinal, "
            "updated_at = excluded.updated_at",
            (sid, rows[-1][1], now),
        )
        conn.execute(
            "INSERT INTO session_projects (session_id, harness, project_path, updated_at) "
            "VALUES (?, 'hermes', ?, ?) "
            "ON CONFLICT(session_id, harness) DO UPDATE SET project_path = excluded.project_path, "
            "updated_at = excluded.updated_at",
            (sid, project, now),
        )
        conn.commit()
        return len(rows)
    except sqlite3.Error as e:
        conn.rollback()
        print(f"  mirror failed for {sid}: {e}")
        return 0


# ── state ──────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=0, ensure_ascii=False), encoding="utf-8")


# ── main ───────────────────────────────────────────────────────────────────

def run_sync(*, days=30.0, since=None, min_messages=6, limit=0, project="",
             mode="digest", digest_category=DEFAULT_DIGEST_CATEGORY,
             digest_importance=60, fact_importance=65, scope="project",
             shareable=0, mirror_transcripts=False, include_subagents=False,
             apply=False, quiet=True, log=print) -> dict:
    """Core sync routine. Returns a summary dict; prints progress unless quiet.

    Callable in-process (the MCP tool uses this) so no child process is spawned —
    a subprocess inheriting the MCP server's stdio pipe deadlocks.
    """
    if mode not in ("digest", "facts", "both"):
        raise ValueError("mode must be digest|facts|both")
    if digest_category not in VALID_CATEGORIES:
        raise ValueError(f"digest_category must be one of {sorted(VALID_CATEGORIES)}")

    dry_run = not apply
    since_s = since if since else time.time() - days * 86400
    excluded = ["cron"] if include_subagents else ["cron", "subagent"]

    dbs = discover_hermes_dbs()
    if not dbs:
        raise SystemExit("no Hermes state.db found under %s" % HERMES_HOME)

    conn = connect_ctx()
    state = load_state()
    tally = {"insert": 0, "update": 0, "skip": 0}
    by_project: dict[str, int] = {}
    by_db: dict[str, int] = {}
    processed = 0
    mirrored = 0
    reaped = 0

    try:
        for label, db_path in dbs:
            hc = hermes_conn(db_path)
            try:
                sessions = load_sessions(hc, since_s, min_messages, excluded)
                if limit:
                    sessions = sessions[-limit:]
                if not sessions:
                    continue
                if not quiet:
                    log(f"── {label} ({db_path}) : {len(sessions)} sessions")
                for sess in sessions:
                    proj = resolve_identity(sess["git_repo_root"] or sess["cwd"])
                    if proj is None:
                        continue
                    if project and project not in proj:
                        continue
                    msgs = session_messages(hc, sess["id"])
                    if not msgs:
                        continue

                    acts = []
                    if mode in ("digest", "both"):
                        digest = build_digest(sess, msgs)
                        if digest:
                            acts.append((digest_category, digest, digest_importance, "digest"))
                        else:
                            # Session became all-noise (or lost its content): remove
                            # any digest we previously wrote so it stops injecting.
                            if reap_memory(conn, project=proj, category=digest_category,
                                           session_id=sess["id"], dry_run=dry_run):
                                reaped += 1
                                if not quiet:
                                    log(f"[{proj}] {sess['id']}  reaped stale digest "
                                        f"({digest_category})")
                    if mode in ("facts", "both"):
                        for cat, text in extract_facts(sess, msgs):
                            acts.append((cat, text, fact_importance, "fact"))
                    if not acts and not mirror_transcripts:
                        continue

                    # The storage contract is one row per (project, category,
                    # session): find_existing() matches on those three and uses
                    # LIMIT 1. When a session yields several facts in one
                    # category, writing them all just rewrites the same row N
                    # times per run (measured: 97 UPDATE+commit ops for a single
                    # real content change) — pure WAL churn on a 340MB DB now
                    # that this runs hourly. Collapse to the last per category,
                    # which is exactly the row that survives today anyway.
                    _dedup: dict[str, tuple] = {}
                    for act in acts:
                        _dedup[act[0]] = act
                    acts = list(_dedup.values())

                    processed += 1
                    by_project[proj] = by_project.get(proj, 0) + 1
                    by_db[label] = by_db.get(label, 0) + 1
                    if not quiet:
                        log(f"[{label}] {sess['id']}  {len(msgs)} turns -> {len(acts)} memories")

                    for cat, content, imp, kind in acts:
                        res = upsert_memory(conn, project=proj, category=cat, content=content,
                                            session_id=sess["id"], importance=imp, scope=scope,
                                            shareable=shareable, kind=kind, dry_run=dry_run)
                        tally[res] += 1
                        if not quiet and res != "skip":
                            log(f"    {res:6} {cat:22} {_clip(content, 90)}")

                    if mirror_transcripts:
                        n = mirror_transcript(conn, sess, msgs, proj, dry_run)
                        mirrored += n
                        if not quiet and n:
                            log(f"    mirror {n:5} transcript rows -> hermes_{sess['id']}")

                    state[sess["id"]] = {"project": proj, "db": label,
                                         "synced_at": int(time.time() * 1000),
                                         "messages": len(msgs)}
            finally:
                hc.close()

        if not dry_run:
            save_state(state)
    finally:
        conn.close()

    return {
        "applied": not dry_run,
        "sessions_processed": processed,
        "inserted": tally["insert"],
        "updated": tally["update"],
        "skipped": tally["skip"],
        "reaped": reaped,
        "mirrored_rows": mirrored,
        "by_project": by_project,
        "by_db": by_db,
        "dbs": [f"{label}:{p}" for label, p in dbs],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (default)")
    ap.add_argument("--days", type=float, default=30, help="look back N days (default 30)")
    ap.add_argument("--since", type=float, default=None, help="epoch seconds lower bound")
    ap.add_argument("--min-messages", type=int, default=6, help="skip tiny sessions")
    ap.add_argument("--limit", type=int, default=0, help="max sessions to process (0=all)")
    ap.add_argument("--project", default="", help="only sessions whose identity matches")
    ap.add_argument("--mode", choices=["digest", "facts", "both"], default="digest")
    ap.add_argument("--digest-category", default=DEFAULT_DIGEST_CATEGORY)
    ap.add_argument("--digest-importance", type=int, default=60)
    ap.add_argument("--fact-importance", type=int, default=65)
    ap.add_argument("--scope", default="project", choices=["project", "ecosystem", "universe"])
    ap.add_argument("--shareable", type=int, default=0, choices=[0, 1])
    ap.add_argument("--mirror-transcripts", action="store_true")
    ap.add_argument("--include-subagents", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        summary = run_sync(
            days=args.days, since=args.since, min_messages=args.min_messages,
            limit=args.limit, project=args.project, mode=args.mode,
            digest_category=args.digest_category, digest_importance=args.digest_importance,
            fact_importance=args.fact_importance, scope=args.scope,
            shareable=args.shareable, mirror_transcripts=args.mirror_transcripts,
            include_subagents=args.include_subagents, apply=args.apply, quiet=args.quiet,
        )
    except ValueError as e:
        raise SystemExit(str(e))

    print()
    print("APPLIED" if summary["applied"] else "DRY RUN — nothing written")
    print(f"  sessions processed : {summary['sessions_processed']}")
    print(f"  memories inserted  : {summary['inserted']}")
    print(f"  memories updated   : {summary['updated']}")
    print(f"  unchanged (skipped): {summary['skipped']}")
    if summary.get("reaped"):
        print(f"  stale digests reaped: {summary['reaped']}")
    if summary["mirrored_rows"]:
        print(f"  transcript rows    : {summary['mirrored_rows']}")
    for p, c in sorted(summary["by_project"].items(), key=lambda kv: -kv[1]):
        print(f"    {p}  ({c} sessions)")
    if summary.get("by_db"):
        for lbl, c in sorted(summary["by_db"].items()):
            print(f"    db {lbl}: {c} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
