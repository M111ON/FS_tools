# Chat Index Map — build receipt (2026-10-28)

Scope was raised from "index one chat file" to "bring order to the whole system": every
corpus this machine keeps, joined into one queryable file, reusing the structure that
already exists instead of rebuilding it.

Result: `I:\tools\chat-index\chatmap.db` — 290.0 MB, built in 69.6 s, queried in 180–200 ms.

---

## Phase 0 — inventory (measured, not assumed)

| source | path | what actually exists |
|---|---|---|
| magic-context DB | `I:\cortexkit\magic-context\context.db` | 1,465 `session_meta` · 2,299 `session_projects` · 39.9k `message_history_source` · 23.7k `message_history_fts` (text) · 19.6k `message_fts_rowid_map` (event clocks, 85.5% filled) · 925 `compartments` (p3 filled for 100%) · 2,316 `memories` · 41.4k `tags` · 2,651 `compartment_chunk_embeddings` · 2,133 `memory_embeddings` · 487 `git_commits` |
| vault archive | `I:\.vault\exported_session` | 2,036 `.md` (66.0 MB) · 2,036 `.json` (666.9 MB) · 60,530 turn headings, but only 1,360 md carry turn structure — 674 are magic-context maps/dumps, not chats · 2,034 md carry a header (`# title`, `**Created:**`, `**Updated:**`) |
| deepseek export | `F:\Dowload_backup\Dow_October\deepseek_data-2026-09-29` | `conversations.json` 84 conversations / 6,008 turns on their longest paths · `turns (4).txt` 1,260,904 chars / 861 turns · `chat-dsk2.json` 1,053 chunks × 1,200 chars = 1,262,696 chars · `converted/*.json` 84 files |
| ANN | `I:\tools\ann\data\memcore` | `ann_index.jsonl` 263.3 MB / 63,915 vectors, dim 384 · `ann_tags.tsv` 63,915 rows over 12 kinds · live server `/health`: `entries=63915 routed=1 k=64` |
| git | MC `git_commits` | 487 commits, 2 projects (`git:29b0837d…` DWGLS, `git:174753f2…`) |
| other projects | MC `session_projects` | 9 distinct project keys (661 DWGLS sessions, 126 + 31 + 12 + … in other dirs) |

## Phase 0 corrections to the original handoff

The handoff assumed one corpus with recoverable ids. Measurement says otherwise:

1. **`chat-dsk2.json` is not the vault corpus.** It is a chunking of exactly one DeepSeek
   conversation — `74d5e3a5-136a-4f7c-9531-598aff4322f4`, title *"วัดระยะห่าง vertex 4D
   hypercube"*, 877 tree nodes, inserted 2026-10-22, updated 2026-10-28. Hash-matching its
   chunks against OpenCode sessions could never succeed; the corpus is different.
2. **Session recovery is an offset problem, not a hash problem.** The chunker consumed
   `turns (4).txt` (1,260,904 chars) and emitted 1,262,696 chars — same stream. Locating each
   chunk byte-exactly in that stream gives provenance with no guessing.
3. **Blocks do not need to be derived.** MC already holds 925 compartments with `p1..p4`,
   importance and message spans. Regenerating them would have violated zero-waste.
4. **ANN cannot be joined by session id.** Its `sid` is a content hash (`2273d7a5…`) and its
   only label is a `file` string plus an `ann_tags.tsv` kind. It stays a separate layer.
5. **MC's `updated_at` is ingest time**, not event time — every re-imported row carries one
   identical stamp. Ordering had to come from `message_fts_rowid_map.message_time_ms`
   and the vault header.

## Phase 1–2 — provenance

`scripts/map_ds_stream.py`

| measurement | value |
|---|---|
| turn markers parsed from `turns (4).txt` | 861 |
| turns matched to a conversation turn (role + 180-char head) | 860 / 861 (99.9%) |
| chunks located byte-exactly | 1,043 / 1,053 (99.0%) |
| chunks without provenance | 10 (`201,202,672,673,738,739,922,923,924,925`) |
| conversations covered | 1 / 84 |

The 10 orphans are new content: `turns (4).txt` was written 18:13 and `chat-dsk2.json` at
18:27 the same evening, so those chunks hold text added between the two snapshots. They are
kept with `cid = NULL` rather than being force-fitted to a conversation.

## Phase 3 — blocks

Reused, not rebuilt: 925 MC compartments across 56 sessions, each with `p1..p4`, importance,
episode type and a message span (`start_ord..end_ord`). All 925 have `p3`.

## Phase 4 — index layer

`chatmap.db` tables, all built from the sources above:

```
project 12      session 2,050    msg 39,865        vault_turn 29,341
block 925       fact 2,316       gitcommit 487     artifact 276,161
ds_conv 84      ds_turn 860      ds_chunk 1,053    ann 63,915
FTS5: msg_fts 22,421 · vault_fts 29,341 · ds_fts 860 · fact_fts 2,316 · block_fts 925
```

Artifact census: `file` 149,407 · `path` 60,612 · `mem` 37,664 · `sha` 27,127 · `cov` 1,284
· `pr` 67.

## Phase 5 — receipts and remaining gaps

Integrity checks, read back from the built database:

| check | result |
|---|---|
| messages whose session is missing | 0 |
| blocks whose session is missing | 0 |
| deepseek chunks with a conversation | 1,043 / 1,053 |
| sessions with messages | 967 |
| sessions that survive only as vault md (no MC messages) | 1,083 |
| facts that resolve to a session row | 1,229 / 2,129 |
| real event clocks attached to messages | 16,378 / 39,865 |
| message text still held by MC | 22,421 / 39,865 |
| session span (sessions with messages) | 2026-08-24 → 2026-10-28 |
| session span (all, incl. md-only) | 2026-06-03 → 2026-10-28 |
| query latency (`ask`, 3 hits/layer) | 181–203 ms |

Known gaps, stated rather than hidden:

- **Text coverage is 56%.** MC drops message text after reduction by design; the vault md is
  the archive for those turns, and the map keeps both layers side by side.
- **1,083 sessions are structure-only** (vault header + blocks, no message rows).
- **900 facts** point at sessions that exist in neither MC nor the vault.
- **10 deepseek chunks** are newer than the stream snapshot they were cut from.
- **ANN is a separate layer**: 63,915 vectors labelled by kind, not linked to `session.sid`.
- `sha` artifacts include hex-looking non-commits; `gitcommit` is the authoritative list.

## Phase 6 — operationalise

- `chatmap.py` — `stats`, `audit`, `ask`, `art`, `session`, `block`, `timeline`, `ds`.
- `refresh.cmd` — rebuilds atomically (`chatmap.db.tmp` → swap), scheduled task
  `chat-index-refresh` daily 05:10, log `I:\tools\chat-index\refresh.log`.
- Sources stay read-only; nothing in vault / MC / ANN / deepseek is ever written.
- Scheduler settings (verified from the task XML): `StartWhenAvailable=true` (a missed
  05:10 run fires as soon as the machine is back), `LogonType=S4U` (runs without the user
  being logged on), batteries allowed, 30-minute limit. The runner uses the absolute
  interpreter `I:\python3.14.4\python.exe` because a non-interactive token may not inherit
  the interactive `PATH`. Proof: `schtasks /run` returned Last Result `0` and rewrote
  `chatmap.db` (192 s under the service token vs ~70 s interactively).

## Phase 7 — Fail-Loud pass (2026-10-28, same day)

Two classes of confidently-wrong answer were named, and auditing for them exposed a third,
worse one that was already live:

| defect | symptom before | fix |
|---|---|---|
| hex text read as a commit | `ask <anything hex>` returned hits with no authority | `ask` checks `gitcommit` first; a non-commit is announced as *"hex-shaped but matches NO gitcommit"* |
| orphan fact | a fact pointing at a deleted session printed as a normal source | inline `[!] ORPHAN fact` / `[!] NO SOURCE recorded` + `warnings (n)` footer |
| **dead FTS layer** (found while testing the above) | `content=''` FTS5 stores no columns, so `join … on fact_id` matched nothing: **the blocks and facts layers had been returning zero rows for every query** and looked exactly like "no hit" | fact/block FTS rowids pinned to their primary keys, joins moved to `rowid`, build now aborts with `FAIL LOUD` if a layer joins to zero, `audit` reports per-layer joinability |

`audit` snapshot after the fix (2026-10-28 20:20):

```
gitcommit rows              487
sessions                  2,050   (967 with messages, 1,083 md-only)
orphan facts                900    facts with no source session 187
hex artifacts w/o commit  1,017    sha artifacts that ARE commits 974
deepseek chunks w/o conv     10
msg w/o text             17,469    msg w/o real clock 23,512
blocks without p3             0
fts joinable: msg 22,434/39,946 · vault 29,341 · ds 860 · fact 2,322 · block 927
```

Worst orphan-fact sources: `ses_f8a1d74a…` (98 facts), `ses_f2eacb3d…` (61),
`ses_f61fe62f…` (45) — sessions deleted from MC before the vault ever saw them.

## Phase 8 — orphan-fact forensics (2026-10-28, same day)

The owner pushed back on the raw number: *"900 orphan facts = 38.7% of all facts — is that by
design, a race at ingest, or a matching bug on our side?"* All three were tested.

### Verdict

**Systemic source-side loss, not by design, and not a matching bug.**

### 1. Id format — clean (rules out hypothesis 3)

| test | result |
|---|---|
| distinct sids behind the orphan facts | 77 |
| id shape | **77/77 valid** `ses_` + 16 hex + 10 base62 (30 chars) |
| truncation (11-char prefix collides with a live id) | 0 |
| near-miss (same length, hamming ≤ 2 from a live id) | 0 |
| id present verbatim in MC `session_meta` | 8 / 77 |

The 8 "prefix matches" in an earlier probe were exact matches, i.e. the same id — no
truncation, no typo, no format drift. The join logic was never wrong.

### 2. Retention — not by design (rules out hypothesis 1)

| probe | result |
|---|---|
| `pending_session_cleanup` (the designed delete queue) | **0 rows** |
| `message_history_orphan_sweep` | 2 cursors, `last_swept_at = NULL` |
| `event` table (deletion journal?) | 622 recent events, types are only `message/session.created|updated` — **no delete event retained** |
| orphan facts by month | 2026-08: 207 · 2026-09: 504 · **2026-10: 156** |
| orphan facts still `active` | 554 of 867 |

October orphans exist, so it is not a one-off migration window either.

### 3. Where the sessions went

| witness | sessions | note |
|---|---|---|
| `I:\OpenCode-data\opencode.db` (true source) | 1,678 | 39,643 messages, 137,258 parts — **674 sessions hold zero messages** |
| MC `message_history_source` | 967 | MC only ever ingested **917 of the 1,678** |
| MC `session_meta` (state only) | 1,465 | |
| vault md | 2,034 | covers **701 of the 761** MC never ingested |
| nowhere | — | 60 sessions |

- 72 of the 77 orphan sessions have no `session_meta`, no messages and no compartments.
- 7 of those 72 reappear in opencode.db as **empty stubs titled "Recovered from magic-context"**
  (41 of them bulk-created at `2026-10-04 22:43:16`) — a recovery run re-created session ids
  without their content.
- A raw text search of the vault finds the missing ids **quoted inside other sessions'
  transcripts** (`session-ses_f594ab0.md`, `ses_f3476d5e…json`) but never a file of their own:
  the session existed, was referenced, and its own export never happened.

### 4. What the forensics exposed in our own map (hypothesis 3, our side)

> **Superseded by Phase 9.** The "shell" classification below counted only MC messages and
> the vault; it ignored opencode.db's own `message`/`session_message` tables. Once those are
> counted the shell class collapses to **0 facts**. Kept here as the record of what was
> believed at the time; Phase 9 carries the corrected numbers.

The Phase-7 audit treated *"a session row exists"* as *"the fact is traceable"*. That is the
same class of error it was built to catch. Split now:

| fact class | count | meaning |
|---|---|---|
| session UNKNOWN to all 5 witnesses | **867** (554 active) | only the memory survives |
| session exists, **no transcript** (shell) | **145** (109 active) | id survives, content gone |
| no `source_sid` at all | 187 | provenance never recorded |
| openable | 1,132 | |

The shells are not uniformly empty: **97 facts sit on the "Recovered from magic-context"
stubs, 47 on meta-only sessions — and most of those still have compartments**, so
`ask` now points at the surviving block instead of only complaining:

```
fact #463 ARCHITECTURE imp=60 <- ses_f96116ed8ffeZBh4qAdZsEYmVF 2026-09-04 10:23
    DWGLS bipolar antipodal compression: octahedron 8-face model …
    [!] NO TRANSCRIPT — session exists as a shell (source=1 meta=0 archived=0),
        but 14 block(s) survive — try: chatmap session ses_f96116ed8ffeZBh4qAdZsEYmVF
```

Structural change: the `session` table now unions **five independent witnesses** (MC messages,
MC session_meta, vault md, block parents, opencode.db) and records `in_source`, `in_mc_meta`,
`archived`, `directory`, `parent_sid`, `src_msg`, so "known" and "has content" are separate
questions.

### 5. Still unproven

- Why the 65 remaining sessions vanished from both MC and opencode.db: no deletion journal
  survives, so the *when* is unknown and the *why* is not recoverable from this machine.
- The earlier `900` used a narrower rule (id absent from a 4-witness table), so it counted
  shell-backed facts as fine. Today's classification is 867 + 145 + 187 out of 2,331 facts
  from only **164 distinct source sessions** — the fact store is very dense (one session
  carries 98 facts), which is why 72 lost sessions cost ~900 facts.

## Phase 9 — corrections after tracing the recovery run (2026-10-28, evening)

Two claims from Phase 8 were **wrong**, and both came from measuring one layer and calling it
the whole. Traced properly, the picture inverts.

### 9.1 opencode.db is two databases in one file

| table | rows | role |
|---|---|---|
| `session` | 1,678 | v1 session rows |
| `session_v2` | 1,784 | v2 session rows (**127 ids exist only here**) |
| `message` | 39,643 | v1 messages |
| `part` | 137,258 | v1 parts |
| `session_message` | 45,256 | **v2 message projection — the live content store** |

`I:\OpenCode-data\opencode.db` is being written by opencode v2 while chat-index read only the v1
tables. Counting `message` alone makes a session with thousands of v2 rows look empty.

### 9.2 Retraction: "145 facts on empty shells" → **0**

The 18 sids behind that bucket are not empty:

| layer | rows found for those 18 sessions |
|---|---|
| `message` (v1) | 199 |
| `session_message` (v2) | 3,041 |
| export file | 1 of 18 |

`chatmap.py audit` now says:

```
facts -> session UNKNOWN anywhere        867
facts -> session with NO content at all    0
facts with no source session               187
sessions with no content in any layer      2
```

The only real orphan class is the 867 facts on **65 ids absent from every layer** — verified
against `session`, `session_v2`, `message`, `part`, `session_message` and the export cache:
65/65 absent everywhere. That verdict stands.

### 9.3 Retraction: the 2026-10-04 22:43:16 run did NOT create content-less sessions

| probe | result |
|---|---|
| stubs created by that run | **41**, all with the identical `time_created` |
| that cluster's content | **2,183 message rows + 2,183 parts + 2,139 v2 rows**; all 67 in `session_v2` |
| export files with content | 43 of 67 (1,135 messages) |
| stub writer still running | **no** — newest stub 2026-10-04 22:43, 0 in the last 7 days |
| other shared-timestamp clusters in 1,678 sessions | none (this is the only one ≥5) |

So the damage is **metadata, not content**:

- title → the literal `Recovered from magic-context` (67 sessions)
- created/updated → one constant for 41, so their real creation order is gone

11 of the 67 still carry their original title in the vault md header, and the rest can be
recovered from their first user message; the timestamps cannot be recovered.

**Corrected later the same day (Phase 12): the md rescues nothing.** 60 of the 67 have a vault
md whose header title is *also* the marker, because that md was written after the overwrite.
The "11 from md" figure above was wrong. The working rescue path is the first user message:
**53 of 67** titles rebuilt inside the map, 14 left as the literal marker (no user message
survives to derive one from). The source db was not touched.

**Guard (canary, not a patch):** no recovery script survives on disk (searched `I:\tools`,
`I:\cortexkit`, `~/.config` for the literal — only this repo's own docs match), so there is
nothing left to edit. `chatmap.py audit` now prints the two signatures of that run so a repeat
is visible the day it happens:

```
regression canaries (signatures of the 2026-10-04 recovery run)
   41 sessions share one created timestamp 2026-10-04 22:43
  sessions titled 'Recovered from magic-context'      67  (source title)
      of those, original title recoverable from md   11
  sessions with no content in any layer                2
```

### 9.4 The 674 no-content sessions are magic-context's own scaffolds

| family | count | what it is |
|---|---|---|
| `magic-context-compartment` | **669** | sessions the MC plugin spawns to run a compartmentalization pass (vault md shows the `<compartment_examples_from_other_projects>` prompt) |
| `New session - <ts>` | 5 | created, never used |
| drafts / failed runs / test sessions | **0** | — |

They hold no messages by design, their content lives in the prompt text exported to the vault
md (≈6 KB each), and **0 facts point at them**. Nothing to backfill, nothing to accept as loss.

Corrected coverage of **real conversations** (1,004 sessions that actually contain messages):

| | sessions | share |
|---|---|---|
| ingested by MC | 917 | 91.3% |
| exported to the vault | 940 | 93.6% |
| reachable through chat-index (any layer) | 1,004 | 100% |
| in **neither** MC nor vault | 63 | 6.3% |

So the honest ingestion gap is **63 real conversations**, not "40% of the source".

### 9.5 The pattern behind the two retractions

Both errors had the same shape: I counted rows in the store I happened to be reading and
reported the count as the world. The fix is structural, not a note — `session` now carries
`src_msg`, `src_v2`, `src_v2_msg`, `src_title` and a single `has_content` flag computed across
every layer, so "no content" can only be printed after all of them are consulted.

## Phase 10 — the 65 lost ids: hunted through every backup, closed (2026-10-28)

Before accepting "provenance lost", the whole disk was searched for the 65 ids that live only
inside 867 memories: **12,107 files / 226 s** across `I:\tools\cloud_ws\memcore\.opencode_export`,
`I:\.vault\exported_session`, `F:\Dowload_backup`, `F:\backup`, `I:\OpenCode-data`,
`I:\cortexkit`, byte-level (no extension filter assumptions).

| finding | count |
|---|---|
| ids appearing **somewhere as text** | 65 / 65 |
| ids with a real `session` row in `F:\backup\opencode.db` (3.77 GB, 1,658 sessions) | **0 / 65** |
| message rows for them in that backup | 0 |
| facts attached to a recoverable session | 0 of 867 |

The 43 "hits" in `F:\backup\opencode.db` were the ids quoted **inside conversation text**, not
session rows — the same for `I:\cortexkit\magic-context\context.db` (65 hits = the fact rows
themselves). **Verdict: unrecoverable, and now provably so.** The 867 memories keep their text;
only their link to a transcript is gone, and `ask` labels them `[!] ORPHAN fact` on every hit.

## Phase 11 — the refresh can no longer silently shrink the map

`chat-index-refresh` runs unattended at 05:10. The build guarded FTS joinability but not the
swap itself, so a locked or half-written source would have produced a smaller map and replaced
a good one — the map equivalent of a confident wrong answer ("we have no history for that").

The swap now has two gates, and refuses instead of guessing:

1. **floors** per layer (`session` 2,000 · `msg` 38,000 · `vault_turn` 28,000 · `block` 900 ·
   `fact` 2,300 · `gitcommit` 450 · `ds_chunk` 1,000 · `ann` 60,000 · `artifact` 250,000)
2. **never shrink** — any layer down >2% against the map being replaced aborts, as does any
   missing source file/dir

Drill (deliberate failure via `CHATMAP_FLOOR_SCALE=9`):

```
FAIL LOUD — refusing to replace the map:
   session=2226 below floor 18000
   ...
   previous map left untouched: I:\tools\chat-index\chatmap.db
exit=3     db untouched = True     (map still readable: 2,226 sessions / 40,114 messages)
```

## What this buys

Before: a question like *"which session decided the ANN bucket rule, and what was measured?"*
required three separate searches (MC search, vault grep, ANN recall) and manual id matching.

After: one command returns the block, the message coordinate, the fact that recorded the
decision, and the commit — all dated by event time, all with a stable coordinate to reopen.
