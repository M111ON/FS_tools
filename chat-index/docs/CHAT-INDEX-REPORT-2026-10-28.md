# Chat Index — final report (2026-10-28)

**One file:** `I:\tools\chat-index\chatmap.db` (277 MB, integrity `ok`)
**One command:** `python I:\tools\chat-index\chatmap.py ask "…"` (≈0.2 s)
**One question it answers:** *where does this memory / decision / commit come from, and what
exactly did the conversation say?*

Built from five corpora that already existed. Nothing was re-generated, nothing upstream was
written to.

---

## 1. What is in it

| layer | rows | source | notes |
|---|---|---|---|
| `session` | 2,226 | MC + vault + opencode v1/v2 + block parents | 5 witnesses, see §3 |
| `msg` | 40,160 | MC `message_history_source` + `message_history_fts` | 22,452 with text |
| `vault_turn` | 29,341 | `I:\.vault\exported_session\*.md` | 2,034 md, the durable transcript copy |
| `block` | 930 | MC `compartments` | p1..p4 reused, never re-derived |
| `fact` | 2,338 | MC `memories` | each keeps its source session id |
| `gitcommit` | 487 | MC `git_commits` | the authority on what a sha is |
| `ds_conv` / `ds_turn` / `ds_chunk` | 84 / 860 / 1,053 | DeepSeek export | one conversation, cut into chunks, provenance by byte offset (1,043/1,053) |
| `ann` | 63,915 | `I:\tools\ann\data\memcore` | 12 tag kinds; **no chat coordinate yet** (§ plan) |
| `artifact` | 276,288 | MC artifact tables | 149k files, 60k paths, 37k memory refs, 27k shas |
| FTS5 | 5 layers | msg, vault, ds, fact, block | rowid-pinned, joinability asserted |

Session span **2026-06-06 06:31 .. 2026-10-28 21:50**. Rebuild 70–83 s, atomic swap.

## 2. Commands

```
chatmap.py ask "streaming capo load"   full-text across msg/vault/ds + facts + blocks
chatmap.py art tess_load_stream        artifact -> session coordinate, PR, #memory
chatmap.py session ses_f9cba8fe0       one session: header, blocks, message span
chatmap.py block 454                   one block + its message range
chatmap.py timeline [--limit N]        newest real events (event clock, not ingest clock)
chatmap.py ds 201                      deepseek chunk -> conversation -> turn
chatmap.py stats | audit               inventory / trust ledger
```

`ask` marks untraceable hits inline — `[!] ORPHAN fact`, `[!] NO TRANSCRIPT ANYWHERE`, and for
hex-shaped strings it says whether the id is a real commit or a text artifact.

## 3. Why the numbers can be trusted

**Five witnesses decide whether a session exists.** A session id is real if *any* of these has
it: opencode `session`, opencode `session_v2`, MC `message_history_source`, MC `session_meta`,
a vault md, or a compartment parent. Checking one of them (the original design flaw) made live
sessions look deleted and empty shells look fine.

**`has_content` is computed across every layer** — MC messages, opencode v1 `message`, opencode
v2 `session_message`, vault md, blocks. "No transcript" is only printable after all five say
so. Result: **2 sessions of 2,226** have no content anywhere.

**Event time, never ingest time.** MC's `updated_at` is an ingest stamp (a re-import writes one
value across a whole session). Real clocks come from `message_fts_rowid_map.message_time_ms`
(85.5% coverage) and the vault md `**Created:**/**Updated:**` header.

**The swap is gated.** Floors per layer plus a never-shrink check (>2% on any layer aborts), and
any missing source aborts. Drill: `CHATMAP_FLOOR_SCALE=9` → `exit 3`, old map untouched. So the
unattended 05:10 refresh can never quietly replace a good map with a partial one.

**`audit` is the trust ledger** (1.5 s): orphan facts, hex artifacts, FTS joinability,
regression canaries.

## 4. Corrections made during the build (kept on the record)

| claim I made | truth | cause |
|---|---|---|
| "145 facts sit on empty shells (109 active)" | **0** — those sessions have 199 v1 messages + 3,041 v2 rows | I counted MC rows only; opencode.db holds two generations in one file (`session`/`message` **and** `session_v2`/`session_message`, 127 ids exist only in v2) |
| "the 2026-10-04 recovery run created 41 content-less sessions" | it created 41 sessions **with** content (2,183 messages, all in v2); what it destroyed is metadata: title → one literal, created time → one constant | same single-layer reading |
| "11 of the 67 original titles survive in the vault md" | **0** — 60 md were written after the overwrite, so their header carries the marker too | measured the wrong predicate |
| "900 orphan facts" | **867** on **65** ids, after witness set grew to 5 sources | baseline changed (and MC is live: 25 memory mutations in 2 h) |

Every one of these is now impossible to repeat silently: the fields that produced them
(`src_msg`, `src_v2_msg`, `has_content`, `title_src`) are stored, and `audit` asserts them.

## 5. Known limits (closed, with evidence)

- **867 memories point at 65 session ids that exist nowhere.** Proven by a byte-level sweep of
  **12,107 files** across the export cache, the vault, `F:\backup`, `F:\Dowload_backup`,
  `I:\OpenCode-data`, `I:\cortexkit`: all 65 appear only as *text* (quoted ids inside other
  transcripts and inside the fact rows themselves). `F:\backup\opencode.db` (3.77 GB, 1,658
  sessions) holds **0/65** as session rows. Unrecoverable — do not re-open the hunt.
- **187 memories have no source session at all** (never recorded one).
- **63 conversations that actually contain messages are in neither MC nor the vault.** 53 of
  them are child/subagent sessions (`parent_id` set), 51 are `magic-context-compartment`
  scaffolds; the genuinely missing user conversations are **3**.
- **675 sessions hold no messages at the source**: 669 `magic-context-compartment` (plugin
  scaffolds, content = the prompt exported to md, 0 facts point at them) + 5 unused
  `New session`. By design, nothing to backfill.
- **17,636 messages have no text in MC** (MC drops text after compaction); the vault md is the
  fallback and both are in the map.
- **ANN rows carry no chat coordinate.** Measured: labels are content strings, only 275 distinct
  session titles exist (1,020 sessions share `magic-context-compartment`), so a title join
  resolves **2.2%**. Bridging ANN needs a `ref` field — see the plan doc.
- **DeepSeek export**: 10 chunks are newer than the snapshot stream they were cut from.

## 6. Operation

| | |
|---|---|
| daily | `chat-index-refresh` 05:10, after `vault-export` 04:30 |
| account | S4U — runs with no user logged on; `StartWhenAvailable` catches a missed 05:10 at next boot |
| missed while powered off | runs at next boot; while *asleep* nothing wakes the machine (no `WakeToRun`) |
| log | `I:\tools\chat-index\refresh.log` |
| atomicity | builds `chatmap.db.tmp`, then `os.replace`; readers never see a partial map |
| registration | `~/.config/opencode/AGENTS.md` LOGIC GATE → chatmap first for "which session/block/commit" |

## 7. Honest summary

The map is finished and self-protecting. The corpus it describes is **not** complete, and the
completeness is now known to the row: of the 1,004 sessions that actually contain conversation,
**91% are in MC**, **94% in the vault**, **100% reachable through the map**, and **3**
conversations exist nowhere. The 867 orphan memories are a fact of the corpus, proven
unrecoverable, and labelled on every hit rather than hidden.

The next move is not more map-building: it is wiring the coordinate into ANN and KV, which is
specified in `CHAT-INDEX-ANN-KV-PLAN-2026-10-28.md`.
