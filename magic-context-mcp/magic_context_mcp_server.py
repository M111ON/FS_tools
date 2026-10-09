#!/usr/bin/env python3
"""
magic_context_mcp_server.py — MCP bridge to CortexKit Magic Context

Reads and writes the magic-context context.db (SQLite) shared with OpenCode/Pi.
Provides search, probe, discovery and write-back tools for Hermes.

DB location: env MAGIC_CONTEXT_DB, else ~/.local/share/cortexkit/magic-context/context.db
On this box the live DB is I:\\cortexkit\\magic-context\\context.db and must be
selected via MAGIC_CONTEXT_DB (set in the Hermes MCP server config).

Writes target the `memories` table, the only channel magic-context exposes across
sessions and harnesses. Row values must satisfy the plugin's isMemoryRow() guard
(see magic_remember) or OpenCode will silently drop them.
"""

import os
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── DB path ────────────────────────────────────────────────────────────
# Resolution order mirrors the plugin's own storage resolution, so the bridge
# finds the same DB OpenCode writes even when MAGIC_CONTEXT_DB is unset:
#   1. MAGIC_CONTEXT_DB        (explicit override; what Hermes config sets)
#   2. MAGIC_CONTEXT_STORAGE_DIR  (plugin's own env override)
#   3. $XDG_DATA_HOME/cortexkit/magic-context/context.db
#   4. ~/.local/share/cortexkit/magic-context/context.db  (platform default)
def _resolve_db_path() -> Path:
    explicit = os.environ.get("MAGIC_CONTEXT_DB", "").strip()
    if explicit:
        return Path(explicit)
    storage = os.environ.get("MAGIC_CONTEXT_STORAGE_DIR", "").strip()
    if storage:
        return Path(storage) / "context.db"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "cortexkit" / "magic-context" / "context.db"


DB_PATH = _resolve_db_path()

mcp = FastMCP("magic-context", dependencies=[])


def _connect():
    """Read-only connection to context.db."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"context.db not found at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _connect_write():
    """Read-write connection for writing memories back.

    context.db is shared with the live OpenCode plugin, so writes must wait for
    the lock rather than fail fast, and must not change journal mode (the plugin
    owns WAL setup). busy_timeout lets a short plugin transaction finish first.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"context.db not found at {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _ts_to_str(ts):
    """Convert epoch ms to ISO string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


# ── Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
def magic_stats() -> str:
    """Overview counts from magic-context: memories, compartments, notes, user profile."""
    conn = _connect()
    try:
        stats = {}
        # memories
        r = conn.execute("SELECT count(*) c, sum(case when status='active' then 1 else 0 end) active FROM memories").fetchone()
        stats["memories"] = {"total": r["c"], "active": r["active"]}

        # by category
        cats = conn.execute(
            "SELECT category, count(*) c FROM memories WHERE status='active' GROUP BY category ORDER BY c DESC"
        ).fetchall()
        stats["memories_by_category"] = {row["category"]: row["c"] for row in cats}

        # compartments
        r = conn.execute("SELECT count(*) c FROM compartments").fetchone()
        stats["compartments"] = r["c"]

        # notes
        notes = conn.execute("SELECT type, count(*) c FROM notes GROUP BY type").fetchall()
        stats["notes"] = {row["type"]: row["c"] for row in notes}

        # user_memories
        r = conn.execute("SELECT count(*) c FROM user_memories WHERE status='active'").fetchone()
        stats["user_memories"] = r["c"]

        # projects
        projs = conn.execute(
            "SELECT project_path, count(*) c FROM memories WHERE status='active' GROUP BY project_path ORDER BY c DESC LIMIT 10"
        ).fetchall()
        stats["projects"] = {row["project_path"]: row["c"] for row in projs}

        return json.dumps(stats, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_search(
    query: str,
    category: str = "",
    project: str = "",
    status: str = "active",
    limit: int = 20,
) -> str:
    """Search memories by keyword. Returns matching content with metadata.
    
    Args:
        query: search term (matched against content)
        category: filter by category (ARCHITECTURE, CONSTRAINTS, PROJECT_RULES, CONFIG_VALUES, NAMING)
        project: filter by project_path (partial match)
        status: filter by status (active, archived). Default: active
        limit: max results (default 20)
    """
    conn = _connect()
    try:
        conditions = ["status = ?"]
        params = [status]

        if query:
            conditions.append("content LIKE ?")
            params.append(f"%{query}%")
        if category:
            conditions.append("category = ?")
            params.append(category)
        if project:
            conditions.append("project_path LIKE ?")
            params.append(f"%{project}%")

        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT id, category, content, importance, project_path, created_at, updated_at "
            f"FROM memories WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "category": row["category"],
                "content": row["content"],
                "importance": row["importance"],
                "project": row["project_path"],
                "created": _ts_to_str(row["created_at"]),
                "updated": _ts_to_str(row["updated_at"]),
            })

        return json.dumps({"count": len(results), "results": results}, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_probe(memory_id: int) -> str:
    """Get full details of a specific memory by ID."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Memory {memory_id} not found"})

        result = dict(row)
        # Convert timestamps
        for key in ("created_at", "updated_at", "first_seen_at", "last_seen_at",
                     "last_retrieved_at", "verified_at", "classified_at", "expires_at"):
            if result.get(key):
                result[key] = _ts_to_str(result[key])
        # Parse metadata_json
        if result.get("metadata_json"):
            try:
                result["metadata_json"] = json.loads(result["metadata_json"])
            except json.JSONDecodeError:
                pass

        return json.dumps(result, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_compartments(
    session: str = "",
    limit: int = 10,
) -> str:
    """List session compartments (compressed conversation summaries).
    
    Args:
        session: filter by session_id (partial match)
        limit: max results (default 10)
    """
    conn = _connect()
    try:
        if session:
            rows = conn.execute(
                "SELECT id, session_id, sequence, title, substr(content,1,500) content_preview, "
                "importance, episode_type, harness, created_at "
                "FROM compartments WHERE session_id LIKE ? "
                "ORDER BY sequence ASC LIMIT ?",
                (f"%{session}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, session_id, sequence, title, substr(content,1,500) content_preview, "
                "importance, episode_type, harness, created_at "
                "FROM compartments ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "session": row["session_id"],
                "seq": row["sequence"],
                "title": row["title"],
                "preview": row["content_preview"],
                "importance": row["importance"],
                "type": row["episode_type"],
                "harness": row["harness"],
                "created": _ts_to_str(row["created_at"]),
            })

        return json.dumps({"count": len(results), "results": results}, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_compartment_detail(compartment_id: int) -> str:
    """Get full content of a specific compartment by ID."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM compartments WHERE id = ?", (compartment_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Compartment {compartment_id} not found"})

        result = dict(row)
        # Drop embedding blobs
        result.pop("p1_embedding", None)
        result.pop("p1_embedding_model_id", None)
        if result.get("created_at"):
            result["created_at"] = _ts_to_str(result["created_at"])

        return json.dumps(result, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_user_profile() -> str:
    """Get user profile memories (how the system understands the user)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, content, status, created_at, updated_at FROM user_memories "
            "WHERE status='active' ORDER BY id"
        ).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "content": row["content"],
                "status": row["status"],
                "created": _ts_to_str(row["created_at"]),
                "updated": _ts_to_str(row["updated_at"]),
            })

        return json.dumps({"count": len(results), "profile": results}, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_notes(note_type: str = "", limit: int = 20) -> str:
    """List notes (session notes and smart notes).
    
    Args:
        note_type: filter by type (session, smart). Empty = all.
        limit: max results (default 20)
    """
    conn = _connect()
    try:
        if note_type:
            rows = conn.execute(
                "SELECT id, type, status, substr(content,1,500) content_preview, "
                "session_id, project_path, created_at, updated_at "
                "FROM notes WHERE type = ? ORDER BY created_at DESC LIMIT ?",
                (note_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, type, status, substr(content,1,500) content_preview, "
                "session_id, project_path, created_at, updated_at "
                "FROM notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "type": row["type"],
                "status": row["status"],
                "preview": row["content_preview"],
                "session": row["session_id"],
                "project": row["project_path"],
                "created": _ts_to_str(row["created_at"]),
                "updated": _ts_to_str(row["updated_at"]),
            })

        return json.dumps({"count": len(results), "results": results}, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_recent(
    kind: str = "all",
    limit: int = 10,
) -> str:
    """Get most recently updated items across all tables.
    
    Args:
        kind: filter — 'memories', 'compartments', 'notes', or 'all' (default)
        limit: max results per kind (default 10)
    """
    conn = _connect()
    try:
        results = {}

        if kind in ("all", "memories"):
            rows = conn.execute(
                "SELECT id, category, substr(content,1,200) content_preview, importance, updated_at "
                "FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            results["memories"] = [
                {
                    "id": r["id"],
                    "category": r["category"],
                    "preview": r["content_preview"],
                    "importance": r["importance"],
                    "updated": _ts_to_str(r["updated_at"]),
                }
                for r in rows
            ]

        if kind in ("all", "compartments"):
            rows = conn.execute(
                "SELECT id, session_id, title, substr(content,1,200) preview, importance, created_at "
                "FROM compartments ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            results["compartments"] = [
                {
                    "id": r["id"],
                    "session": r["session_id"],
                    "title": r["title"],
                    "preview": r["preview"],
                    "importance": r["importance"],
                    "created": _ts_to_str(r["created_at"]),
                }
                for r in rows
            ]

        if kind in ("all", "notes"):
            rows = conn.execute(
                "SELECT id, type, status, substr(content,1,200) preview, created_at "
                "FROM notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            results["notes"] = [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "status": r["status"],
                    "preview": r["preview"],
                    "created": _ts_to_str(r["created_at"]),
                }
                for r in rows
            ]

        return json.dumps(results, indent=2, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def magic_remember(
    content: str,
    category: str = "CONSTRAINTS",
    importance: int = 50,
    project_path: str = "",
    scope: str = "project",
    shareable: int = 0,
) -> str:
    """Save a memory back to magic-context (bidirectional sync Hermes<->OpenCode).

    Writes conform to the plugin's own row contract, so OpenCode actually injects
    the memory. Verified against @cortexkit/opencode-magic-context v0.43.2:
      * normalized_hash = md5(lowercase, whitespace-collapsed, trimmed)
      * source_type MUST be one of historian|agent|dreamer|user — any other value
        is silently dropped by the plugin's isMemoryRow() guard.
      * category must be a known category, status active|permanent|archived,
        scope project|ecosystem|universe.

    Args:
        content: the memory content to store
        category: PROJECT_RULES, ARCHITECTURE, CONSTRAINTS, CONFIG_VALUES, NAMING,
            USER_DIRECTIVES, USER_PREFERENCES, CONFIG_DEFAULTS,
            ARCHITECTURE_DECISIONS, ENVIRONMENT, WORKFLOW_RULES, KNOWN_ISSUES
        importance: 0-100 (higher = more important, default 50)
        project_path: project identity ('git:<rootcommit>' or 'dir:<md5-12>'). If
            empty, resolved from the current working directory.
        scope: project | ecosystem | universe (default project)
        shareable: 1 to let workspace peers see it, else 0
    """
    import hashlib
    import re as _re

    CATEGORIES = {
        "PROJECT_RULES", "ARCHITECTURE", "CONSTRAINTS", "CONFIG_VALUES", "NAMING",
        "USER_DIRECTIVES", "USER_PREFERENCES", "CONFIG_DEFAULTS",
        "ARCHITECTURE_DECISIONS", "ENVIRONMENT", "WORKFLOW_RULES", "KNOWN_ISSUES",
    }
    SOURCE_TYPES = {"historian", "agent", "dreamer", "user"}
    SCOPES = {"project", "ecosystem", "universe"}
    # The plugin's own values (getSourceType -> "agent"), so the row is visible.
    source_type = "agent"

    category = (category or "").strip().upper()
    if category not in CATEGORIES:
        return json.dumps({
            "status": "error",
            "message": f"invalid category {category!r}; must be one of {sorted(CATEGORIES)}",
        }, ensure_ascii=False)
    if scope not in SCOPES:
        return json.dumps({
            "status": "error", "message": f"invalid scope {scope!r}; one of {sorted(SCOPES)}",
        }, ensure_ascii=False)
    if source_type not in SOURCE_TYPES:   # defensive; constant above is valid
        source_type = "agent"

    content = (content or "").strip()
    if not content:
        return json.dumps({"status": "error", "message": "content is empty"}, ensure_ascii=False)

    # Plugin's exact normalization: lowercase, collapse whitespace, trim.
    normalized = _re.sub(r"\s+", " ", content.lower()).strip()
    norm_hash = hashlib.md5(normalized.encode("utf-8")).hexdigest()

    project = (project_path or "").strip() or _resolve_identity(os.getcwd())
    if not project:
        return json.dumps({
            "status": "error",
            "message": "project_path could not be resolved; pass it explicitly "
                       "(e.g. 'git:<root-commit>' or 'dir:<md5-12>')",
        }, ensure_ascii=False)

    importance = max(0, min(100, int(importance)))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    conn = _connect_write()
    try:
        # UNIQUE(project_path, category, normalized_hash) is the dedupe key —
        # scope the lookup by project, not globally.
        existing = conn.execute(
            "SELECT id FROM memories WHERE project_path = ? AND category = ? "
            "AND normalized_hash = ? AND status != 'archived'",
            (project, category, norm_hash),
        ).fetchone()
        if existing:
            return json.dumps({
                "status": "duplicate",
                "existing_id": existing["id"],
                "message": "Memory already exists (same project, category and content)",
            }, ensure_ascii=False)

        cursor = conn.execute(
            "INSERT INTO memories "
            "(project_path, category, content, normalized_hash, importance, scope, "
            "shareable, source_type, seen_count, retrieval_count, "
            "first_seen_at, created_at, updated_at, last_seen_at, "
            "status, verification_status, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, 'active', 'unverified', ?)",
            (project, category, content, norm_hash, importance, scope, int(bool(shareable)),
             source_type, now_ms, now_ms, now_ms, now_ms,
             json.dumps({"origin": "hermes-mcp"}, ensure_ascii=False)),
        )
        conn.commit()
        return json.dumps({
            "status": "saved",
            "id": cursor.lastrowid,
            "category": category,
            "importance": importance,
            "project": project,
            "scope": scope,
            "shareable": int(bool(shareable)),
            "source_type": source_type,
        }, indent=2, ensure_ascii=False)
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return json.dumps({"status": "duplicate", "message": str(e)}, ensure_ascii=False)
    finally:
        conn.close()


def _resolve_identity(path):
    """Mirror the plugin's resolveProjectIdentity: git root commit, else dir hash."""
    import hashlib as _hl
    try:
        cur = os.path.realpath(path)
    except Exception:
        return ""
    probe = cur
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            try:
                import subprocess as _sp
                r = _sp.run(["git", "rev-list", "--max-parents=0", "HEAD"],
                            cwd=probe, capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    cands = sorted(
                        ln.strip()[:64] for ln in r.stdout.splitlines()
                        if len(ln.strip()) >= 7
                    )
                    if cands:
                        return "git:" + cands[0]
            except Exception:
                pass
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return "dir:" + _hl.md5(cur.encode("utf-8")).hexdigest()[:12]


@mcp.tool()
def magic_sync_hermes(
    days: float = 30,
    mode: str = "digest",
    apply: bool = True,
    limit: int = 0,
) -> str:
    """Push Hermes session history into magic-context so OpenCode can recall it.

    Memories are the only cross-session, cross-harness channel magic-context
    exposes, so this is what makes Hermes work visible to OpenCode. Runs the
    sync engine in-process (imported from hermes_mc_sync.py next to this file).

    Args:
        days: look back N days of Hermes sessions (default 30)
        mode: digest (one activity record per session), facts (durable rules), both
        apply: True writes; False previews and writes nothing
        limit: cap sessions processed (0 = all in window)
    """
    import importlib.util

    if mode not in ("digest", "facts", "both"):
        return json.dumps({"status": "error",
                           "message": "mode must be digest|facts|both"},
                          ensure_ascii=False)

    script = Path(__file__).resolve().parent / "hermes_mc_sync.py"
    if not script.exists():
        return json.dumps({"status": "error",
                           "message": f"sync engine not found at {script}"},
                          ensure_ascii=False)

    # Point the engine at this server's DB *before* importing it: the module
    # resolves CTX_DB/HERMES_DB at import time.
    os.environ["MAGIC_CONTEXT_DB"] = str(DB_PATH)

    # Import in-process rather than spawning a child: a subprocess that inherits
    # the MCP server's stdio pipe deadlocks the whole session.
    try:
        spec = importlib.util.spec_from_file_location("hermes_mc_sync", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"no import spec for {script}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return json.dumps({"status": "error",
                           "message": f"could not load sync engine: {e}"},
                          ensure_ascii=False)

    try:
        summary = mod.run_sync(days=float(days), mode=mode, apply=bool(apply),
                               limit=int(limit), quiet=True)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)

    lines = [
        "APPLIED" if summary["applied"] else "DRY RUN — nothing written",
        f"sessions processed : {summary['sessions_processed']}",
        f"memories inserted  : {summary['inserted']}",
        f"memories updated   : {summary['updated']}",
        f"unchanged (skipped): {summary['skipped']}",
    ]
    for p, c in sorted(summary["by_project"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {p}  ({c} sessions)")
    return json.dumps({"status": "ok", **summary, "summary": "\n".join(lines)},
                      indent=2, ensure_ascii=False)


# ── Main ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
