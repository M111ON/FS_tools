# Hermes → magic-context bridge (write path)

**Goal:** work done in Hermes becomes recallable in OpenCode (and Pi/OMP), so a
later OpenCode session knows what Hermes already did.

Status: **working**, verified against `@cortexkit/opencode-magic-context` v0.43.2.

---

## The one fact that determines everything

magic-context exposes exactly **one** cross-session / cross-harness channel:
the `memories` table.

| Store | Scope | Reachable from another OpenCode session? |
|---|---|---|
| `memories` | project identity | **yes** — injected every turn + `ctx_search` |
| `compartments` | one `session_id` | no |
| `message_history_fts` | one `session_id` | no |
| `notes` (`type='session'`) | one `session_id` | no |

OpenCode injects memories with:

```sql
SELECT ... FROM memories
 WHERE project_path = <identity>
   AND status IN ('active','permanent')
   AND (expires_at IS NULL OR expires_at > now)
```

`ctx_search` adds project-wide FTS over the same table. Compartments and the
message FTS index are keyed by `session_id`, and a Hermes session id
(`20261009_121602_4cd74f`) never equals an OpenCode one (`ses_…`), so writing
transcripts there is invisible to OpenCode. **Memories are the only route.**

## Why the earlier bridge was read-only in effect

`magic_remember` existed but its rows were silently dropped by the plugin's
`isMemoryRow()` guard. Three bugs:

1. `source_type='hermes'` — the guard accepts only
   `historian | agent | dreamer | user`. Anything else is discarded before
   injection, with no error.
2. `project_path` was passed as NULL/"global" when the caller omitted it, but
   injection is always scoped to a project identity.
3. The duplicate check searched `normalized_hash` globally instead of
   `(project_path, category, normalized_hash)`.

All three are fixed in `magic_context_mcp_server.py`.

## Row contract (must match or OpenCode ignores the row)

```
normalized_hash = md5( content.toLowerCase().replace(/\s+/g," ").trim() )
UNIQUE(project_path, category, normalized_hash)     -- dedupe key

category    IN PROJECT_RULES ARCHITECTURE CONSTRAINTS CONFIG_VALUES NAMING
                 USER_DIRECTIVES USER_PREFERENCES CONFIG_DEFAULTS
                 ARCHITECTURE_DECISIONS ENVIRONMENT WORKFLOW_RULES KNOWN_ISSUES
status      IN active | permanent | archived
scope       IN project | ecosystem | universe
source_type IN historian | agent | dreamer | user
```

Project identity (mirrors the plugin's `resolveProjectIdentity`):

* `git:<root-commit>` — `git rev-list --max-parents=0 HEAD`, sorted, first.
  (Note: the **root** commit, not HEAD — so it is stable across branches.)
* `dir:<md5(realpath)[:12]>` — when the directory is not a git repo.

Both this tool and the plugin use `md5(lowercase, whitespace-collapsed, trimmed)`;
99% of pre-existing rows match it (verified against 300 live rows).

## Source databases (multi-DB since the Oct-2026 profile split)

Hermes moved its live session DB into `profiles/<name>/state.db`; the root
`I:\Hermes\state.db` froze on **2026-10-09** and no longer receives turns. The
engine therefore **auto-discovers every Hermes DB** — root `state.db` plus each
`profiles/*/state.db` — and syncs them all. `HERMES_STATE_DB` still pins a single
DB when set (tests / one-off runs); leave it unset for the normal multi-DB sweep.

Selection keys on `last_activity_at` (falling back to `started_at`), not
`started_at` alone, so a long-running session keeps refreshing as it gains turns.

Run it on a schedule so OpenCode/Pi stay current:

```bat
REM install once; then runs hourly
I:\tools\magic-context-mcp\install_mc_sync_task.cmd 60
```

Task name: **`hermes-mc-sync`** (`schtasks /run /tn hermes-mc-sync` to fire now).

## Files

| File | Role |
|---|---|
| `hermes_mc_sync.py` | Hermes `state.db` (all DBs) → `context.db` memories. Idempotent. |
| `hermes_mc_sync.cmd` | wrapper for Task Scheduler / manual runs |
| `install_mc_sync_task.cmd` | creates the `hermes-mc-sync` scheduled task |
| `magic_context_mcp_server.py` | MCP bridge; fixed `magic_remember` + `magic_sync_hermes` |
| `backups/` | pre-change `context.db` snapshots |

## Usage

```bash
PY=I:/tools/cloud-workspace/.venv/Scripts/python.exe

# preview (writes nothing)
$PY hermes_mc_sync.py --dry-run --days 30

# write one activity record per session
$PY hermes_mc_sync.py --apply --days 30

# also mine durable rules/decisions (noisier — review first)
$PY hermes_mc_sync.py --apply --days 30 --mode both

# also archive raw transcripts into message FTS (see caveat below)
$PY hermes_mc_sync.py --apply --days 30 --mirror-transcripts
```

Useful flags: `--project <substr>`, `--min-messages N`, `--limit N`,
`--scope ecosystem`, `--shareable 1`, `--include-subagents`.

From Hermes chat the same thing is available as the MCP tool
`magic_sync_hermes(days=30, mode="digest", apply=True)`.

> **Why the MCP tool calls `run_sync()` in-process instead of spawning
> `hermes_mc_sync.py`.** A subprocess launched from inside an MCP stdio server
> inherits the server's stdin/stdout pipe; the child blocks on it and the tool
> hangs until the 300 s MCP timeout (the sync itself takes ~1.3 s). The engine is
> therefore imported and called directly. Keep `run_sync()` the shared entry
> point for both the CLI and the MCP tool — do not reintroduce a subprocess here.
> Note the module resolves `CTX_DB` at **import** time, so
> `MAGIC_CONTEXT_DB` must be set *before* the import.

## Modes

* **digest** (default) — one `ENVIRONMENT` memory per Hermes session:
  date, project, session id, what the user asked about, the outcome. ~250
  tokens each. This is the useful default.
* **facts** — heuristically mines durable-looking sentences into
  `PROJECT_RULES` / `CONSTRAINTS` / `CONFIG_VALUES` / `NAMING` / `ARCHITECTURE`.
  Filtered against narration ("Let me check…"), but still heuristic — dry-run
  first and prune anything wrong.

## Budget discipline

Every memory is injected into **every** OpenCode turn for that project, against a
fixed budget (`memory.injection_budget_tokens`, default 4000; 12500 for DWGLS).
So the digest is deliberately small and strips harness plumbing
(`[OUT-OF-BAND …]`, `<system-reminder>`, background-process notices) that would
otherwise consume the budget. 21 sessions currently cost ≈1.5k tokens.

Prefer fewer, higher-value memories. If the block grows, lower `--days`,
use `--min-messages`, or archive old digests.

## Caveats

* **`--mirror-transcripts` is archival, not recall.** The raw transcript lands in
  `message_history_fts` under `hermes_<session-id>`, which preserves full history
  for `ctx_expand` and future project-wide search, but it is **not** reachable
  from an OpenCode session by `ctx_search` today (that search is session-scoped).
  Only the memories written by digest/facts mode are actually recallable.
* **Don't write directly to `compartments`.** Injection reads compartments by
  exact `session_id`; a synthetic one is never injected. A session-history block
  belongs to the OpenCode session that owns it.
* **Authority guard:** the plugin aborts direct memory writes for projects listed
  in `authority_managed` unless `context_privilege_state.enabled = 1`. Both are
  currently empty/0 here, so writes succeed. If a project ever appears in
  `authority_managed`, stop writing to it directly and route through the plugin.
* **Restart Hermes** after editing the MCP server for changes to take effect.
* **Verify after any write** with the check below; a row that violates the
  contract fails silently.

## Verify

```bash
PY=I:/tools/cloud-workspace/.venv/Scripts/python.exe

# ad-hoc verification: 42 checks over the sync engine + MCP bridge.
# Writes go to a throwaway copy; the live DB is only ever opened read-only.
$PY I:/tools/magic-context-mcp/verify_bridge.py
```

Expected: `AD-HOC VERIFICATION: 42 passed, 0 failed`, exit 0. Evidence from the
last run is kept in `verification-evidence.txt` alongside the code hashes.

For a quick manual check of live state:

```bash
PY=I:/tools/cloud-workspace/.venv/Scripts/python.exe
$PY - <<'EOF'
import sqlite3, time
c = sqlite3.connect("file:I:/cortexkit/magic-context/context.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
CAT={"PROJECT_RULES","ARCHITECTURE","CONSTRAINTS","CONFIG_VALUES","NAMING","USER_DIRECTIVES",
     "USER_PREFERENCES","CONFIG_DEFAULTS","ARCHITECTURE_DECISIONS","ENVIRONMENT","WORKFLOW_RULES","KNOWN_ISSUES"}
SRC={"historian","agent","dreamer","user"}
rows=c.execute("SELECT * FROM memories WHERE metadata_json LIKE '%hermes_session_id%'").fetchall()
bad=[r["id"] for r in rows if not (r["category"] in CAT and r["source_type"] in SRC
     and r["status"] in ("active","permanent","archived") and r["scope"] in ("project","ecosystem","universe")
     and r["project_path"] and r["normalized_hash"])]
print(f"synced={len(rows)}  would-be-dropped={len(bad)} {bad}")
proj="git:174753f2375c19a1c0ce1388ef592bfa02d2b056"
n=c.execute("""SELECT COUNT(*) FROM memories WHERE project_path=? AND status IN ('active','permanent')
               AND (expires_at IS NULL OR expires_at>?)""",(proj,int(time.time()*1000))).fetchone()[0]
print(f"injectable memories for {proj}: {n}")
EOF
```

Expected: `would-be-dropped=0`.

## Scheduling (optional)

Daily 04:00 catch-up so OpenCode sees the previous day's Hermes work:

```
schtasks /create /tn hermes-mc-sync /sc daily /st 04:00 ^
  /tr "I:\tools\magic-context-mcp\hermes_mc_sync.cmd 30"
```

Idempotent — re-running only rewrites rows whose content actually changed.

## Backups

`backups/context.db.pre-hermes-sync-*` — taken before the first write. Restore by
copying over `context.db` while OpenCode is closed.
