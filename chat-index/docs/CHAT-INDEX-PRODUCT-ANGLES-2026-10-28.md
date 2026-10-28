# What our own machinery can become — 9 angles, ranked (2026-10-28)

Constraint on this whole document: every angle is built **only** from capabilities we already
have or can prove we have. Where a capability is missing, it says so with a number instead of
a promise. No angle may claim compression, speed over clusters, or a capability borrowed from
HNSW/ANN literature (owner positioning #7498, MAP-not-COMPRESS #6192).

---

## 1. Capability inventory — what is actually on the shelf

| # | capability | status | receipt |
|---|---|---|---|
| C1 | **Coordinate + provenance lookup**: any word/phrase → session, ordinal, block, fact, commit | **working** | `chatmap.py ask` ~0.2 s, 40,179 msgs / 29,341 vault turns / 5 FTS layers |
| C2 | **Event clock on everything textual** | **working** | msg 40,179/40,179 have a clock; facts 2,338/2,338 have `created_ms`; blocks 930/930 |
| C3 | **Five-witness existence + has_content** | **working** | 2,226 sessions, 0 false "empty" after fix |
| C4 | **Semantic route over 63,915 vectors**, 12 kinds, live server | **working** | `/v1/state/search`, recall@5 .94 measured (#7775) |
| C5 | **KV frame timeline** (turn → base/delta artifact, `sid` slot) | **struct ready, data demo-scale** | `core/kv_frame_index.h`; 14 `.kvcb` + 4 `.kvd`, no `kv_timeline.idx` |
| C6 | **Compartment summaries p1..p4** | **narrow** | 930 blocks but only **56 sessions** (197 sessions have ≥20 msgs → 77% of long sessions have none) |
| C7 | **Categorized facts with source** | **working, 92%** | 2,338 facts: 2,151 with source sid; 867 point at 65 lost ids |
| C8 | **Commit history with time** | **working, unlinked** | 487 commits, but MC's table has no session column; time+project proximity joins only **81/487 (17%)** |
| C9 | **Trust ledger** (audit / canary / swap guard / floors) | **working** | 1.5 s audit, 41-session canary, exit-3 swap drill |
| C10 | **Durable text copy of every turn** | **working** | vault md 2,034 files, 29,341 turns, 59.7 MB |

The shelf is deep on **provenance and verification** and thin on **live capture** (KV demo data) and
**summaries** (blocks cover 2.5% of sessions). Angles below are ranked by that reality.

---

## 2. Angles

### P1 · Memory that answers with receipts (agent-facing)
**Question it kills:** *"did we decide this, or am I about to invent it again?"*
**Backend:** C1 + C3 + C7 + C9 (+ C4 for semantics).
**Shape:** one call returns `{answer, coordinate, quote, event_time, evidence[], flags[]}`; if the
link is broken it returns `[!] ORPHAN` rather than a confident sentence. Exposed as an MCP tool
so every agent session uses the same memory instead of six different stores.
**Missing:** `coord` CLI (Phase A of the ANN/KV plan), ANN `ref` for semantic hits.
**Readiness: 80%.** First slice: wrap `ask`+`session`+`block` as an MCP tool — ~1 h, usable the
same day. Gate: an answer must never be returned without a coordinate or an explicit "no
coordinate" flag.

### P2 · Decision ledger (living ADR)
**Question it kills:** *"what is the current truth about X, and what did it replace?"*
**Backend:** C7 + C2 + C8.
**Shape:** per project and category, a table of decisions with `status: active | superseded |
retracted`, `decided_at`, `evidence coordinate`, `superseded_by`. Throws the owner's own rule
back at the system: *ผิดได้ แต่ไม่ผิดซ้ำสอง*.
**Measured material:** 2,338 facts, all with time + category (ARCHITECTURE 754, CONFIG_VALUES
558, CONSTRAINTS 458, PROJECT_RULES 368); **200 facts (9%) mention a supersession/replacement**
("superseded", "แทนที่", "ยกเลิก", "no longer", …) — those are the candidate pairs to resolve.
**Missing:** the resolver (which fact supersedes which) and a precision gate — a wrong
"superseded" is worse than no ledger.
**Readiness: 50%.**

### P3 · Provenance bisect — "is this regression today or pre-existing?"
**Question it kills:** the one you ask most: *"เลขนี้เปลี่ยนไปเมื่อไหร่ และใครเปลี่ยน"*.
**Backend:** C2 + C1 + C8 (+ C4 for "similar wording").
**Shape:** `chatmap.py when "0.9627"` → first mention, last mention, every session in between,
what the value was before and after, and which commit landed in that window. Also supports
"which facts mention this file, oldest first".
**Measured material:** every message is time-stamped; 487 commits are time-stamped; 100% clock
coverage means the timeline has no holes.
**Missing:** term-timeline query + per-value diffing (cheap: FTS by date bucket).
**Readiness: 75%.** This is the highest value-per-hour on the shelf.

### P4 · Corpus integrity monitor (silent-loss radar)
**Question it kills:** *"did anything disappear since yesterday?"*
**Backend:** C9 + C3.
**Shape:** daily `audit` snapshot appended to a history table; alarms when a layer shrinks, a
new shared-timestamp cluster appears, orphan facts jump, or an FTS layer stops joining.
**Why it matters:** a recovery run overwrote 67 session titles and nothing noticed for 24 days;
65 sessions vanished with no journal. This angle makes that class impossible to miss.
**Missing:** history table + threshold policy (both trivial), one scheduled task.
**Readiness: 90%.** ~0.5 day including a week of backfilled history from existing receipts.

### P5 · Continuity brief (handoff that cannot invent)
**Question it kills:** *"what was I doing, what is decided, what is untested?"*
**Backend:** C7 + C2 + C8 + C6 (where blocks exist) + C5 (where KV frames exist).
**Shape:** a brief where every line ends in a coordinate; plus, for captured sessions, a KV
resume handle. Today we write this by hand every session — this manufactures it from evidence.
**Missing:** blocks cover 56 sessions; KV frames are demo-scale; commits are 17% linkable.
So v1 = facts + timeline + open questions only (works for all sessions), v2 adds blocks, v3 adds
KV resume.
**Readiness: 60% (v1), 20% (v3).**

### P6 · Cross-session shared desk
**Question it kills:** *"what happened in the other sessions right now?"*
**Backend:** chat-pool board (exists) + C1 (coordinates as card payload).
**Shape:** cards carry `sid#ord` + one-line claim; any agent expands a card by coordinate instead
of copying prose. Kills the copy-paste handoff and the "context lost between sessions" failure.
**Missing:** discipline, not code — the board exists.
**Readiness: 70%.**

### P7 · Intent-routed hybrid retrieval (FTS + ANN + relation)
**Question it kills:** *"find it even when I don't remember the words, without breaking relation queries"*
**Backend:** C1 (exact/structure) + C4 (semantic) + coordinate graph (relation).
**Shape:** query-intent classifier picks the path: value → ANN+FTS union; relation → graph
traversal only; shape → ANN; unknown → safe baseline. This is the direct consequence of the
measured regression (#7819: unconditional union dropped relation recall .940 → .719).
**Missing:** an eval set with real queries and ground-truth coordinates (chat-index can generate
it: pick a fact, use its own wording as the query, its coordinate as truth).
**Readiness: 30%** — and it must not ship before the eval set exists.

### P8 · Dataset factory for downstream training (the "LoRA" stage)
**Question it kills:** *"where does training data come from that is actually verified?"*
**Backend:** C1 + C7 + C6 + C4.
**Shape:** export `(question, decided outcome, evidence coordinate, project, date)` pairs; dedup
by session; every row traceable to a transcript, so a model error can be audited back to a
source. Blocks with p1..p4 (930) are natural seed labels.
**Missing:** leakage filter (secrets/paths), dedup policy, quality sampling.
**Readiness: 40%.**

### P9 · Write-through memory (no fact without a coordinate)
**Question it kills:** *"how do we stop producing orphan facts?"*
**Backend:** C3 + C9 + the ingestion side of MC.
**Shape:** every new fact/message/block carries `(sid, ordinal)` + event clock at write time;
the build **rejects** facts without a source coordinate (or stores them with a `NO_SOURCE` flag
that `ask` prints).
**Why:** 867 orphan memories exist because the write path never required provenance; the map
detects the disease but does not cure it.
**Missing:** a change in the MC/plugin write path — outside this repo, needs owner approval.
**Readiness: 40%.**

---

## 3. Ranked shortlist (value × readiness ÷ effort)

| rank | angle | why now | first slice | gate |
|---|---|---|---|---|
| 1 | **P3 provenance bisect** | data is 100% complete, no dependency on ANN/KV work | `chatmap.py when "<term or number>"` — occurrence timeline + commit window | prints first/last mention with coordinates; a value with no prior mention says "appears once" instead of implying history |
| 2 | **P4 integrity monitor** | prevents the exact failure we just spent a day on | snapshot table + daily task + alarm thresholds | injected shrink (scale floors) raises an alarm; a normal day raises none |
| 3 | **P1 memory-with-receipts (MCP)** | makes every session use the map, which is how it earns trust | MCP tool over `ask/session/block` | no answer without coordinate or explicit flag |
| 4 | P6 shared desk | board already exists, zero new machinery | card template + one example | an agent can expand a card into a transcript by coordinate alone |
| 5 | P5 continuity v1 | daily pain, but blocks/KV are thin | handoff brief from facts + timeline | every line has a coordinate; "unknown" is allowed, invention is not |
| 6 | P2 decision ledger | high value, needs a precision gate | resolve the 200 supersession candidates, sample 50 | ≥90% correct pairs on the sample, else ship read-only |
| 7 | P7 hybrid retrieval | needs an eval set first | build eval set from facts (query = paraphrase, truth = coordinate) | relation subset must not regress below baseline .940 |
| 8 | P8 dataset factory | depends on P1/P2 quality | 500 verified pairs export | zero rows without coordinate; leakage scan clean |
| 9 | P9 write-through memory | right long-term, needs MC change | proposal to the MC writer | a fact without coordinate cannot be inserted |

## 4. What NOT to build (and why)

- **A chat UI / note app** — duplicates the tools the owner already uses; no field advantage.
- **A bigger vector index** — recall is already .94 at K=24; the constraint is placement, not size.
- **Block re-derivation** — 930 blocks exist; generating more would violate zero-waste and the
  boundary would drift from MC's.
- **KV resume as a headline feature** — 14 artifacts of 50–102-byte prompts is a proof, not a
  product; it becomes real only when cold capture runs on actual conversations.
- **Anything that claims storage savings** — this layer routes; it does not shrink bytes.

## 5. If only one thing ships

**P3 provenance bisect.** It needs no new source, no ANN coordinate, no KV data — every input is
already complete and time-stamped, and it answers the question the owner asks in every audit:
*"is this new, or was it always like this?"* P4 is the safety net that should ship beside it
because it is nearly free.
