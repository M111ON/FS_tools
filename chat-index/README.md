# chat-index — one order map over every corpus on this machine

`chatmap.db` joins the five things this machine already keeps, read-only, into one file you
can query in ~200 ms from a terminal or an agent.

**Docs:** `docs/CHAT-INDEX-REPORT-2026-10-28.md` (final report: inventory, trust model,
proven limits) · `docs/WHEN-PROVENANCE-2026-10-28.md` (`when` command + JSON/log schema) ·
`docs/CHAT-INDEX-ANN-KV-PLAN-2026-10-28.md` (how to wire the coordinate into ANN and KV) ·
`docs/CHAT-INDEX-PRODUCT-ANGLES-2026-10-28.md` (9 things this can become) ·
`docs/CHAT-INDEX-MAP-2026-10-28.md` (build log phase by phase, corrections kept).

| layer | source | what it holds |
|---|---|---|
| sessions | MC `context.db` + `I:\.vault\exported_session` + `I:\OpenCode-data\opencode.db` (v1 **and** v2) | 2,226 sessions, five witnesses, real clocks, titles |
| messages | MC `message_history_source` + `_fts` | 40,160 rows, text where MC still keeps it |
| vault turns | `I:\.vault\exported_session\*.md` | 29,341 turns of archived text |
| blocks | MC `compartments` | 930 blocks with p1..p4 (reused — never re-derived) |
| facts | MC `memories` | 2,338 facts, each pointing at its source session |
| commits | MC `git_commits` | 487 commits with `project_path` |
| deepseek | `F:\Dowload_backup\Dow_October\deepseek_data-2026-09-29` | 84 conversations, 860 stream turns, 1,053 chunks with provenance |
| ann | `I:\tools\ann\data\memcore` | 63,915 vectors, 12 kinds (no chat coordinate yet — see plan) |
| artifacts | derived | 276,288 path / file / PR / memory-id / coverage marks |

## query

```bat
python I:\tools\chat-index\chatmap.py stats
python I:\tools\chat-index\chatmap.py audit                             :: the trust ledger
python I:\tools\chat-index\chatmap.py ask "streaming capo load"      :: every layer at once
python I:\tools\chat-index\chatmap.py art tess_load_stream           :: file / path / PR / #memory
python I:\tools\chat-index\chatmap.py session ses_f9cba8fe0          :: one session + its blocks
python I:\tools\chat-index\chatmap.py block 454                      :: one block + span messages
python I:\tools\chat-index\chatmap.py timeline --limit 20            :: newest sessions first
python I:\tools\chat-index\chatmap.py ds 201                         :: deepseek chunk -> conversation
python I:\tools\chat-index\chatmap.py when 0.9627                   :: when did this value change (8 layers)
python I:\tools\chat-index\scripts\monitor.py                        :: daily snapshot + cliff/trend alarms
```

`ask` tries every layer and prints the source coordinate for each hit
(`session#ordinal`, `turn N`, `fact #id`, `block id`, `commit sha`).

`when` is the intersection view: time (normalised numbers), vault, fact, commit window,
artifact path, repo doc, block, and the ANN query log. Every layer reports how many rows it
examined, so "not found" and "never searched" cannot look alike. `--json` emits schema 1 (see
`docs/WHEN-PROVENANCE-2026-10-28.md`).

`monitor.py` writes `snapshots/snap-YYYYMMDD.json` and judges it: a layer losing >5% is a
cliff, three consecutive lower days is a trend, a missing day is a `[gap]` and never a trend
input. Exit 2 on alarm. Scheduled daily 05:40 after the refresh (S4U, catches up after the
machine was off). See `docs/INTEGRITY-MONITOR-2026-10-29.md`.

## fail-loud contract

A hit that cannot be traced to ground truth is marked, never printed as if it were solid.
`ask` ends with a `warnings (n)` block and marks rows inline:

| marker | meaning |
|---|---|
| `[!] … hex-shaped but matches NO gitcommit` | the query looks like a sha but no commit has it — hits are text, not a commit |
| `[!] ORPHAN fact — source session not found` | the fact points at a session absent from both MC and the vault |
| `[!] NO SOURCE recorded` | the fact carries no `source_sid` at all |
| `[!] query error … layer unreadable` | the layer raised instead of returning empty |
| `[!] DEAD` in `audit` | an FTS layer whose join returns 0 rows (looks identical to "no hit") |

`audit` counts all of them: orphan facts, facts with no source, hex artifacts that are not
commits, chunks without provenance, messages without text or clock, and per-layer FTS
joinability. The build refuses to finish (`FAIL LOUD`) if any FTS layer joins to zero rows.

## rebuild

```bat
I:\tools\chat-index\refresh.cmd
```

Rebuilds `chatmap.db` atomically (writes `chatmap.db.tmp`, then swaps). Sources are opened
read-only and never written. Scheduled task `chat-index-refresh` runs it daily at 05:10,
after `vault-export` (04:30). Log: `refresh.log`.

## rules this map obeys

- **read-only on every source** (vault, MC, ANN, deepseek export).
- **event time, not ingest time** — MC's `updated_at` is when a re-import touched the row,
  so message clocks come from `message_fts_rowid_map.message_time_ms` and session clocks
  from the vault header (`**Created:**` / `**Updated:**`).
- **reuse structure, never regenerate it** — blocks are the existing compartments, not a
  new summarizer; facts are the existing memories; nothing is re-embedded.
- **fail loud** — every remaining gap is listed in `docs/CHAT-INDEX-MAP-2026-10-28.md`,
  and `ask` marks untraceable hits inline instead of returning them as solid.
- **the swap is gated** — floors per layer plus a never-shrink check, so the unattended 05:10
  refresh can never replace a good map with a partial one (drill: `CHATMAP_FLOOR_SCALE=9` →
  exit 3, old map untouched).
- **`audit` is the trust ledger** — orphan facts, hex-shaped artifacts, FTS joinability, and
  the regression canaries for the 2026-10-04 recovery run.
- **contentless FTS5 joins by rowid only.** `content=''` tables store no column values, so
  `join fact x on x.fact_id = f.fact_id` silently returns zero rows. fact/block FTS rowids
  are pinned to their primary keys, and the build + `audit` both verify joinability.
