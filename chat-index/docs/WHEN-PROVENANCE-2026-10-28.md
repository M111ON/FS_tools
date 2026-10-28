# `when` — intersection provenance (built 2026-10-28)

Answers one question: **"when did this change, and what moved with it?"**
It is not a causal chain. It prints the layers that can be intersected, each with the number of
rows it actually examined, so `not found` and `never searched` can never look the same.

```
python I:\tools\chat-index\chatmap.py when 0.9627
python I:\tools\chat-index\chatmap.py when "streaming capo" -k 40
python I:\tools\chat-index\chatmap.py when 1.18 --json
```

## Layers (all read-only, all inside chatmap.db)

| layer | covers | why it is trustworthy | known blind spot |
|---|---|---|---|
| **time** | 208,826 numeric cells / 40,221 messages, 100% clocked | normalised decimals (1–6 places, comma groups included), raw spelling kept | vault rows added before the clock existed have no time |
| **vault** | 29,341 archived turns | survives MC text truncation | 0 clocks of its own for numbers (time comes from `metric`) |
| **fact** | 2,343 memories, 2,151 with a source session | event time on every row | 867 point at lost session ids → flagged per hit |
| **commit** | 487 commits, a **time window** (±3 days) | `committed_ms` on all | no file column, no session column, and only 2026-08..10 |
| **artifact** | 276,312 file/path/mem/sha marks | derived from messages | `sha` marks include hex that is not a commit |
| **doc** | 220 repo md (DWGLS 166, ann 23, chat-index 6, PLAYBOOK 25) | values and rules often live here, not in chat | md only, no PDF/DOCX |
| **block** | 930 compartments over 56 sessions | MC's own boundaries, reused | **sparse** — absent for 97.5% of sessions; reported as `block_absent` |
| **querylog** | `ann_query-*.jsonl` (written by `ann_search.c`, ≤400 files) | one line per ANN search, with the coordinate that asked | only counts searches made after 2026-10-29 |
| not searched | `kv_resume`, `ann_vector` | — | printed in `not_searched` with the reason |

## The number trap this command exists to close

`0.9627` is written in chat as **`.9627`** (no leading zero).

```
LIKE '%0.9627%'   -> 0 hits        FTS match "0.9627" -> 0
LIKE '%.9627%'    -> 16 hits       FTS match 0.9627   -> syntax error
```

So every decimal token is normalised to a float at build time (`metric` table) and each hit keeps
`matched_via` = the spelling that was actually in the text. A query for `0.9627` resolves to
9 distinct events and names both spellings in `raw_forms` + the `mixed_spellings` flag.

## JSON schema (schema 1)

```json
{
  "tool": "chatmap", "schema": 1, "build": "2026-10-28 23:46",
  "query": "0.9627", "query_kind": "number|term", "value_num": 0.9627,
  "layers": { "time": {"searched": true, "n": 9, "why": "…"}, "commit": {"searched": true, "n": 16, "why": "window ±3d"}, "…": {} },
  "hits": [
    {"layer": "time", "src": "msg", "sid": "ses_…", "ordinal": 386,
     "ts_ms": 1761200520000, "ts_iso": "2026-10-23 11:02",
     "matched_via": ".9627", "occurrences": 2, "snippet": "…", "mirror": "vault#385"},
    {"layer": "fact", "fact_id": 7961, "sid": "ses_…", "category": "CONSTRAINTS",
     "status": "active", "source_known": true, "ts_iso": "…"},
    {"layer": "commit", "sha": "c525454", "short": "c525454", "ts_iso": "2026-10-23 11:07"},
    {"layer": "doc", "path": "I:\\DWGLS-native-fs\\docs\\ANN-UPGRADE-2026-10-23-PHASE0.md"},
    {"layer": "artifact", "kind": "file", "ref": "…"},
    {"layer": "block", "block_id": 42, "sid": "ses_…"},
    {"layer": "vault", "sid": "ses_…", "ordinal": 385}
  ],
  "total_hits": 31,
  "flags": ["normalized", "mixed_spellings", "multi_commit_window",
            "outside_commit_coverage", "dense:see_by_month", "truncated", "block_absent",
            "appears_once", "orphan_fact:1234", "not_found_in_any_searched_layer"],
  "raw_forms": [".9627", "0.9627"],
  "by_month": {"2026-10": 40}, "by_month_undated": 10, "span_days": 5.0,
  "not_searched": {"kv_resume": "no kv_timeline.idx", "ann_vector": "no chat coordinate"},
  "next": ["chatmap session ses_…", "chatmap ask '0.9627'"]
}
```

Field rationale, one line each:

- `layers.<name>.n` + `.why` — a bare list of searched layers cannot say "searched, zero rows".
- `matched_via` per hit (not per answer) — different layers match differently (number vs text).
- `build` — two runs can only be compared against the same map.
- `raw_forms` / `mixed_spellings` — shows the normalisation instead of hiding it.
- `by_month` / `dense:see_by_month` — a value with 1,146 occurrences needs a shape, not a dump.
- `outside_commit_coverage` — commits only exist for Aug–Oct, so a Jan hit has no commit layer.
- `block_absent` — blocks cover 56 of 2,226 sessions; absence is data, not an error.
- `not_searched` — the honest list of what this command cannot see yet.
- `mirror` — the same words in the vault md and in MC, merged into one event, not two.
- `next` — the command that continues the trace, so the answer is actionable.

## Gates (all pass today)

| gate | result |
|---|---|
| deterministic | two runs → byte-identical JSON (`Get-FileHash` equal) |
| number normalisation | `0.9627` → 9 events, `matched_via` `.9627` AND `0.9627`, `mixed_spellings` |
| not found ≠ no history | `0.99311` → `[]` + `no hit in any searched layer — this is 'not found', not 'no history'` + the searched list |
| dense value | `1.18` → `by_month` summary instead of 1,146 rows |
| block absence | reported as `block_absent`, never silent |
| latency | 0.2–0.4 s per query |

## Round 2 (2026-10-29, after an audit of the same code)

Four ways the first version could still mislead, and what replaced them:

| defect | fix | seen on a real query |
|---|---|---|
| `layers.<name>.n` was "rows in the display page", so `-k 5` made a layer look like it had 5 hits | `n` is the **total occurrence count**, the list is only the page; `why` says `total occurrences (list is the first N)` | `when 0.9627`: `time=63` rows, 10 distinct events; with `-k 3` the count no longer moves |
| the layer `printf`-built SQL with `%`-unescaped user text — a `%` or `_` in the query widened the match silently | every value goes through `_like()` and every pattern carries `escape '\'` | `when 50%` → `time=997` rows, not the whole 29,179-cell layer |
| a Thai query was only ever searched in the ASCII layers, so Thai text looked absent | Thai terms reach the same layers (SQLite `LIKE` is byte-wise, so the prefix/suffix windows still span the term) | `when ท่อ`: `time=2958 vault=87 fact=41 commit=199 doc=76 block=157` |
| the block-count SQL bound 4 placeholders while 8 values were supplied — the count threw instead of answering | one parenthesised group per normalised form | `when 0.9627` runs clean; the error is gone |

Integer-shaped queries are still weak on purpose: `when 454` reaches `vault=348 artifact=202`
by substring, which is honest (the digits do appear) but not selective. Use `when` for numbers
with a decimal point, identifiers, hashes, and Thai phrases — the cases where a substring is
already the answer.

## Round 3 (2026-10-29, metric extraction)

The layer above only knew about numbers shaped `\d*\.\d{2,6}`. Three shapes it got wrong, all
fixed in `scripts/build_chatmap.py` before the table is filled:

| shape | old behaviour | now |
|---|---|---|
| `1,234.5678` | matched as `234.5678` (a comma is not a word char, so the lookbehind let the tail through) — a *wrong* value, not a miss | comma groups are their own alternative and are stripped before `float()`: `20,736` → `20736.0` (23,103 such cells) |
| `1.5ms`, `0.5B` | dropped: the unit letter tripped the trailing guard | a trailing unit is allowed; scientific notation `1.2e5` is still excluded because it means 120000, not 1.2 |
| `1.5` | not indexed at all, so `when 1.5` answered "not found" while the value is everywhere | 1–6 decimal places (2,230 cells, 754 vault turns) |

Version strings stay out by construction: `python3.14`, `qwen2.5`, `v1.18`, `2.0.1` all fail the
`(?<![\w.])` / `(?![\d.])` pair. Build size after the change: 208,826 cells / 4,034 distinct
values (was 29,179 / 1,918); the map grows 302.7 → 352.6 MB. `eval_when.py` is still 19/19 with
per-layer recall block 3/3, doc 6/6, time 12/12, vault 16/16.

Two metric-layer defects from the audit are deliberately **not** fixed, because they are display
questions rather than wrong answers: a cell knows `sid` + `ordinal` but not which side of the
turn it sits on, and vault rows carry no clock of their own (their time is borrowed from the
matching message). Both are recorded here instead of silently "solved".


## What this proves on a real case

`when 0.9627` returns the four messages where the ANN bucket number was measured (2026-10-23
11:02–11:30, session "ANN upgrade 2026-10-22"), the one memory that recorded the rule
(#7961), two documents, and a commit window containing `c525454` — *"docs(ann): phase 2b — K
ladder, topb trade, rotation invariance proof, live receipts"* at 11:07, five minutes after the
message. That is the intersection: message → fact → commit, with the block layer honestly empty
for that session.

## ANN request logging — the schema this influences

Per-query line (append-only jsonl, one object per request). What the server can actually
measure, and what it must carry so it is not a new orphan batch:

```json
{"v":1,"ts_ms":1761200520000,"coord":"ses_xxx#386","input_hash":"sha1:…","input_chars":57,
 "K":24,"index_fp":"sha1:…","n_cand":16329,"budget":24000,
 "top":[{"sid":"…","score":0.984,"rank":1}, "…"],"latency_us":4123,
 "mode":"search","model_hash":"…"}
```

Rules: `coord` may be `null` but never absent; `ts_ms` is the request clock (never an ingest
time); **no `recall@5`** — recall needs an oracle and belongs to the bench receipts
(`sift*_receipts.jsonl`), not the server; `index_fp` ties the answer to the exact index that
produced it; `v` allows the format to change without ambiguity.
