# chat-pool SPEC — cross-app group-chat pool + tower geometry (DWGLS)

Locked 2026-09-16. Code: `pool.py`, `tower.py`, `chat_tower.h` (stdlib/C only).

## 1. B-slice — pool (durability)

`log.jsonl`, append-only, one JSON/line (UTF-8). v:2 format:

```json
{"v":2,"t":1789539028.27,"app":"hermes","kind":"code","text":"...",
 "prev":"<prev-hash|GENESIS>","rkey":19450,"enc":730,"hash":"<sha256>"}
```

- `hash = sha256(prev+app+kind+text)` — INTEGRITY chain (0/0 measured).
- `rkey` = exact port of `core/rdh_capture.h` (RDH_CAPTURE_144) — ADDRESS
  hint only. Never integrity: single-RDH collides 1800/6072, flip-blind 9.04%.
- `enc = rkey % 1440` — free frame_seek view.
- Append = single `os.write` O_APPEND + fsync (cross-app atomic). Rules: append
  only, never edit by hand, never run `demo` outside tests (it wipes).

Flush (hourly, task `chat-pool-flush`): `flush_emit()` = sha256-changed →
`outbox/<ts>.json`, else SKIP. `push_outbox()` reuses
`hermes_pool_cli.py embed --session chat-pool` (venv python), archives `.pushed`,
empty = no-op. `.flush_state.json` = `{sha, offset}`.

## 2. A-slice — tower (geometry)

Tower = 1 tesseract = 1152 chunk-slots (**32×36**), 18 towers = FULL 20736.
Chunk = 144B. `towers/t<t0>/{header.json, cells.hex}`, fps recomputed on load.

Header: `{beacon: 111*(pair+1), t0, count, pair, view: A/B, tess}`.
`tower_idx = t0//1152`, `tess = idx%18`, view A = 32×36, B = 36×32.
Time-seed (LOCKED): `pair = (idx + PIS60[(cycle*7)%60]%9)%9`, cycle = idx//18 —
logical t only (replayable), shift = pair bijection (uniformity exact).

- **Ticker (dual-hand, LOCKED):** fast `enc(t) = (t*37)%1440`, bijection
  1440/1440, invertible. Slow hand Pisano-60 table `phase=Fib(t%60)%10`;
  zero-beacons at phase 0,15,30,45 (96/rotation, every 15 ticks, even spread).
  Pisano is NOT bijection/invertible → landmarks only, never addressing.
  Repdigit beacons `frame_enc(3k)=111k` (t=3→111 … 27→999); beacon k ↔ pair k.
  1440 = 24×60 = π(9)×π(10).
- **Sibling (B-twinview, LOCKED):** 9 pairs (i,i+9), transpose link.
  Proven 20736/20736, fixed 0. Transpose cycles: 2 fixed anchors + 10×115.
  Rule: single-writer canonical view, readers lock-free.
- **Fingerprint (LOCKED fp_vec8):** 144B → 9×16B sums, center v4,
  8 line-sums. 141568/141644 distinct (coll 0.054%), 16B. mod9 kept as
  1B prefilter only. kNN probe = random (1.1x) → vec8 is checksum, not feature.
- **p4m block (LOCKED):** tower 1152 + 4 header-sentinels = 1156 = 34×34 =
  4×(17×17). Sentinels = block-center 2×2 D4-orbit
  `{(16,16),(16,18),(18,16),(18,18)}` carrying
  (beacon,t0,count,pair) — self-describing, no sidecar. Tiling class p4m
  (all 6 generators PASS ×3 periods). Payload grouped Q0..Q3 ×288.
- `20736 = 18×32×36 = 4×72²`; factor table {4,8,9}→[16,32,36,64,72,81];
  9-cell full grid kept (zero-sum needs center); C4 proven, full-D4 impossible
  (axial-flip stabilizer mismatch) → C4+chirality.

## 3. Proven numbers (facts.jsonl: 6072 lines → 211497 chunks)

| test | result |
|---|---|
| ticker bijection/invert/roundtrip | 1440/1440, 0/211497 |
| mod9 distribution | uniform ~23.5k/bucket |
| chunk dedup (exact) | 33.03% |
| tower shootout 32 vs 36 | F-coll 0 both → dual axes, use both |
| sibling A-shift / B-twinview | 20736/20736, fixed 0, inv 0 |
| fp shootout vec8 vs mod9 | 0.054% vs shared → vec8 locked |
| tower pack | 184 towers, roundtrip mm 0 |
| E2E pool→towers / live demo | PASS (5 apps, beacon 111) |
| push | PASS (searchable, no-op, cleaned) |
| C parity (chat_tower.h vs tower.py) | 3/3 vectors byte-exact |
| RDH port parity vs rdh_capture.h | 5/5 (caught 1 axis swap) |

## 4. Limits (honest)

- `×8`=D4, `60=4×15`, missing-8/12345679 = base-10 coincidences, not mechanism.
- RDH sees low nibbles only — address, never link.
- Transpose duality = layout duality, not literal dimensions.
- vec8 ≠ semantic signal. Semantics live in encoder (pool.db/fastembed);
  geometry is MAP. Next F1: tower-routed recall vs global.

## 5. C-slice — blocks (เรื่องราว + วิธีการ)

What was SAID is chat/note/handoff; what is LINKED is a bond; what was LEARNED
was nowhere. A **block** is that third thing: the story of a piece of work plus
the method that produced it, written so another platform can pick it up without
replaying the chat. Added 2026-09-19.

A block is an ordinary chained line — `kind="block"`, same
`hash=sha256(prev+app+kind+to+text)`, same plane, same append rules, shown in the
feed, never hidden by the curator (protection rule `kind`). **No new field
rides outside the chain**: everything is inside `text`.

Canonical text — line-oriented, fixed key order, one value per line:

```
⟦block⟧ slug=av-window-view rev=1
title: อ่าน .tess เป็นหน้าต่าง 144x144 แบบ page-aligned
story: ... what happened and why it matters ...
method: 1) ... · 2) ... · 3) ...
evidence: numbers, files, tests (the part someone can re-run)
refs: 3fc8579d
status: closed            (open | closed | parked)
```

Rules (enforced by `block()`/`parse_block`, checked in `pool.py demo`):

- **one normal form**: `render_block(parse_block(render_block(x))) == render_block(x)`
  for every input — two readers that never spoke to each other still agree;
  strict parse (unknown key, repeated key, off-format header, rev < 1, no title
  → not a block, never a last-wins guess).
- **a revision is a new line** (`rev = latest + 1`); nothing is edited, so the
  chain keeps meaning what it means everywhere else. `block_revs(slug)` is the
  history; the live fields come from the last line for that slug.
- **`slug` is the address** (`[a-z0-9][a-z0-9._-]{0,47}`), stable across revisions;
  `revs` counts writes and `apps` remembers every platform that contributed.
- **`refs` are hash prefixes** (6–64 hex) of the lines the story was distilled
  from; `verify_block()` resolves them against the same hash index the plane
  uses, and reports dangling ones instead of ignoring them.
- **shared by design**: no per-app visibility (that is what `to=` is for in
  chat); `/blocks` is token-gated like every other data endpoint.

Surface:

| layer | how |
|---|---|
| python | `block(app, slug, title, story, method, evidence, refs, status)` · `block_get` · `block_revs` · `blocks(status, query)` · `verify_block` · `block_card` |
| CLI | `pool.py block <slug> --title … [--story --method --evidence --refs --status --as app]` · `pool.py blocks [--status --q --json]` · `pool.py block-show <slug> [--revs --json]` |
| HTTP | `GET /blocks?q=&status=` · `GET /blocks?slug=X&revs=1` (adds `verify`, `history`) · `GET /blocks/timeline` (dedup links) · `POST /block {app, slug, title, …}` |
| UI | header `▣ ก้อน` → cards above the feed (meta+title closed, six fields one tap, `ประวัติ rev` = chain history) · header `⏱ timeline` → vertical timeline with platform→when + similarity arrows |

Index: folded into `derived()` in the pass it already makes (no second walk),
updated per line by `_derived_absorb`, cached by `(size, mtime)` like everything
else — `blocks()` is a dict scan, not a parse of the log.

Limits (honest): the fold is O(lines) on rebuild, so at ~100k lines the first
read after another process appends pays one pass — same cost and same fix
location (`pool.db`) as the rest of the derived index. A block is text, not a
vector: semantic recall is still the encoder's job (pool.db/fastembed); geometry
stays a MAP.

## 6. Hide vs prune — one rule table, two consequences

Added 2026-09-20. The log is append-only and hash-chained, so **nothing
removes a line**. What a keeper can do with noise is bounded by that, and the
boundary is worth stating exactly, because "remove the junk" is the obvious
request and "delete" is the wrong answer to it.

| | hide (feed) | prune (plane) |
|---|---|---|
| line on disk | untouched | untouched |
| hash chain | intact | intact |
| effect | not drawn in the feed, revealable | **contributes no count to its cell** |
| a cell of only-noise | still on the map | **absent from the map** |
| toggle | 🧹 on the header | 🧹 prune in the plane bar |
| threshold | `POOL_CURATOR_HIDE` | `POOL_CURATOR_PRUNE` |

Both read the same `CURATOR_RULES` and the same `curator_protect()`, so a line
the feed may not hide is a line the plane may not drop. Three laws are unchanged:
storage/index/graph stay whole; the verdict is a pure function of the line (the
one exception, `repeat`, says so and reads a fixed `REPEAT_WINDOW`); and it never
decides alone — `curator_report()` / `curator_prune_report()` print the reasons
with examples, and `raw=True` (`?raw=1`) is a first-class switch.

**Prune rules** (a line is prunable when score ≥ threshold and nothing protects it):
`test-word` · `test-probe` · `stub` · `ack` · `sess-id` · `hex-blob` ·
`b64-blob` · `punct-soup` · `loud` · `repeat`.
**Protections**, checked before words and about shape first:
`kind` (bond/pointer/block) · `dm` · `mention` · `long` · `marker`.

**Prune reaches past the map.** A bond and a sighting are things that *name an
address*; the line that names one is protected (`kind`), so pruning can never
take it away — but the address it names can stop being drawn. The honest
answer is not to leave the edge out silently and not to draw it into nothing:
the edge keeps its place in the count, gains `dangling: true`, and is held back
by default exactly like a pruned cell. Same rule, one grain further out:

| layer | read | stranded when |
|---|---|---|
| bond | `/bonds` · `all_bonds()` · `bonds_for()` | either end's line is pruned |
| sighting | `/pointers` · `pointers_at()` | its address holds no clean line, judged at the level it declared |
| memory link | `/ctx/links` · `ctx_plane.links()` | the **log** end was pruned (the memory row stays — that module never writes) |

`raw=True` is the same escape hatch at every one of those, and `/ctx/links`
returns `total` and `dangling` with the answer so a client never has to diff two
requests to notice something was held back. One consequence is deliberate and
worth remembering when testing: `pointers_at()` keeps the **latest sighting per
app**, so four reads by one app are one current read, not four.

Three things this cost, all now asserted (`pool.py demo` for 1–3,
`ctx_plane.py demo` for the cross-system half, `prune_check.py` over HTTP):

1. `curator_protect()` compared `kind` case-sensitively, so `"CHAT"` was
   promoted to a protected kind and the keeper was **off** for every writer
   that used uppercase. Kinds are compared by value, never by spelling.
2. The index is both a **fold** (one line per append, `_derived_absorb`) and a
   **rebuild** (`derived`). Folding only the raw map left the pruned one
   describing the previous log while `key` claimed it was current. The demo now
   forces both paths and asserts they agree on every prune number.
3. The edge list is cached, and pruning can strand an edge **without the edge
   set changing** — so a cache key watching only the bond revision would keep
   drawing into a vanished address after a fold. The key carries the prune
   revision too, and the demo forces both paths and compares the stranded flags
   edge by edge.

One more, found by asking a cross-system question on a machine with no index:
`ctx_plane` reads its index through `_index_cells()` / `_index_has_rows()`, so an
unbuilt or absent index reads as an empty memory side. The module's own rule is
that the index is a cache and never the truth, and a reader on a fresh install
should get a truthful zero rather than a 500.

Cost: the full pass runs `curator_hits` per line (9 regexes, short lines).
Rebuild stays O(lines); the fold pays one verdict per append. Both maps come
out of the pass already being made, so pruning adds dicts, not a walk.

## 7. Bonds and sightings — what may be asserted, and by whom

Added 2026-09-20. The plane drew 11,656 dots and **zero edges**: no bond had
ever been written, so the visualiser had no graph to draw. Two different
questions had to be answered differently, and conflating them is what kept the
plane empty.

**A bond is a claim about the data; a sighting is a claim about a reader.**
A bond may only be written when the SOURCE already asserts the relation, and
the relation verb must name the evidence so a reader can audit the edge
(`x-next-in-session` = two rows consecutive in one session file;
`x-answers` = an `exchange` and the document returned for the same file and
term_hint). `bond_real.py` derives 10,641 of them from `source_file`, `seq`,
`term_hint`, `session_id` and `workspace_id` — column facts, nothing inferred.
A sighting may only be written when a read actually happened, which is why it
is the *view* that writes it: `&record=1` on a deliberate cell read.

Nothing had ever called `open_pointer(..., record=True)`, so `sightings_all`
was 0 — the mechanism existed and no one used it. Making the read declare
itself is what turns the feature on, and it is honest because a person
clicking a cell *is* the read the sighting describes.

Two rules keep both usable at real scale:

1. **Chain, never clique.** 359 rows in one session is 64,000 edges; row *n*
to row *n+1* is 358. Every cap is reported in the dry-run, never applied
silently.
2. **Keep a relation only while it discriminates.** A reference to a term that
400 other definitions also mention is vocabulary, not a relationship. Filtering
by that dropped 16,186 pairs, and `x-related-term` fell from 65% of the graph to
25%. Without it the specific edges were unreadable under the generic ones.

### Crossing platforms, and where rule 2 inverts

The corpus was two islands: 8 cross-platform edges out of 10,641, because each
platform's data arrived in its own file. But the bridge already existed in the
data — **opencode files a row under a subject label, hermes defines terms by
name** — so a label that is also a defined term is a relation the source
asserts, not one we invented. Two relations, and they need OPPOSITE treatments
of rule 2:

| relation | claim | evidence | rarity filter |
|---|---|---|---|
| `x-uses-term` | the line names a defined term | whole-word match in the BODY (not the label prefix) | **required** — ≤10 lines corpus-wide |
| `x-hint-is-term` | the line's own subject label IS the term | equality of label and term | **must not apply** |

That inversion is the point. Rule 2 exists to suppress vocabulary, and it reads
frequency as a proxy for "generic". For `x-hint-is-term` the proxy is exactly
backwards: a label shared by 273 rows is not a common word, it is a HUB, and how
many rows point at it is the entire signal — the filter would have deleted the
most informative edges in the graph.

Result: 2,931 cross-platform edges (hermes↔opencode 2,916, dashboard↔hermes 15),
up from 8. Both relations state something falsifiable, so they are re-checked
from the written log rather than trusted: `bond_real.py --verify` re-reads every
edge and confirms the term really appears (2,594/2,594) or the label really
matches (337/337), with zero contradictions.

### The bug this shook out: filter order in `pointers_at()`

`pointers_at(rkey, level)` used to filter by point **before** reducing to the
latest read per app. Both steps were present and each was right alone, but the
order meant a filtered read walked back through an app's history and returned
the sighting it had already superseded: ask "who is at cell A" after that reader
moved to cell B and the answer still said A — so the plane kept their circle on
an address they had left.

The contract is one pass with the steps in this order: **reduce each app to its
latest read, then filter to the address asked for.** A sighting supersedes its
app's previous one wherever that was. Asserted from both sides — `pool.py demo`
(identity) and `prune_check.py` over HTTP (visibility).

### `sighting_once()` — declaring a read is idempotent

Re-declaring the address an app is already reading adds no information, because
`pointers_at()` keeps the latest read per app. A view that re-reads on every
render would otherwise write one line per render, turning a record of where
people looked into a log of how often a page was drawn. `sighting_once()`
returns None for the same address and writes only when the read moves.

## 8. What "the plane is full" actually means

Asked directly: at 20,736 cells, what happens? The measurement says the cell
count is **not** the wall, and the real wall was broken.

**Touch count is a slow asymptote, not a cliff.** The plane reaches 48.8% at
25,230 lines and the slope is already collapsing (12k→25k lines bought 1,447
cells). Under a uniform hash, 90% needs ~47,700 lines and 99% needs ~95,500.
The real curve runs ~30% BELOW uniform, because the capture maps similar bytes
to similar addresses.

**Effective capacity is far smaller than 20,736, and already nearly spent.**
Diversity, not touch count, is what a sparse map needs:

| lines | cells touched | effective cells (1/Simpson) | worst cell |
|---|---|---|---|
| bond 13,572 | 2,651 | **1,407** | 32 |
| chat 6,631 | 5,070 | 3,749 | 13 |
| decision 5,025 | 4,451 | 4,018 | 4 |
| **all 25,230** | 10,118 | **3,848 (18.6% of the plane)** | 34 |

So the plane is 49% touched but **81% spent**: the binding constraint is the
information content of `rkey`, not the number of cells. Bonds are the worst of
it — 13,572 lines structured `<64hex> <rel> <64hex>` land in 2,651 cells, fewer
than a quarter of them distinct, because near-identical text captures alike.
That is a property of the capture rule, not a defect: it is what makes
"search by address" work.

**The overflow valve was broken, and that was the actual wall.** §5.4 says an
overflowing cell stacks: 144^d × VISION_BUDGET(64) lines per cell without the
plane growing. That only holds if the sub-axis `_axis(line, depth)` really
varies with depth — and it could not. The capture walks `steps =
max(len(data), 48)` bytes reading `data[(i + phase) % len(data)]`, so for any
input of 48 bytes or more it visits **every byte exactly once**: `phase`
reorders the sum, it does not change it, and the sub-cell came back identical at
every depth. Measured on the live log, a 132-line cell reported **132/1 at
depths 1, 2 and 3 alike** with `still_over` stuck at 1 — a cell past budget
could not be opened at all, and `_depth_for` could never find a fitting depth.
No real line is under 48 bytes, so no real line was ever subdividable.

A cell holds lines whose captures COLLIDED — equal sums. Therefore **no
function of that same sum can separate them, at any phase.** The sub-axis has to
use what the sum discarded: POSITION. Each byte is now weighted by where it sits
and by which depth is asked, so lines that summed alike still differ.

| | before | after |
|---|---|---|
| `_axis` varies across d1..d5 | 0/400 lines | **400/400** |
| 132-line cell at depth 1 | 1 bucket, max 132, `still_over` 1 | **9 buckets, max 29, `still_over` 0** |
| real cell 1614 (34 lines) | 24 buckets, max 24 | **29 buckets, max 2** |

Honest about the limit: this is a spread, not a uniform hash. 400 lines
differing only by a counter still peak at 40 per bucket (against max 2 on real
cells) — near-identical input keeps colliding, because that is what the capture
rule does. It is enough that an over-budget cell opens in one step, which is
the property §5.4 promised and did not deliver.

## 9. Explore — walking the graph, and naming its shape

The plane answers "what is WHERE". A reader who has arrived at an address asks
the other half: **what did this lead to, and what points back at it.** Both are
already on disk as bonds. Nothing in this layer invents a relation; it reports
which ones the data wrote down.

### 9.1 Shape is geometry, hub is weight

A shape is a claim about the WHOLE local picture, so it needs both degrees. A
single degree cannot name it: "has backlinks" is true of a lone reply and of the
definition 273 lines lean on.

| shape | claim | state |
|---|---|---|
| `isolated` | nothing links here, and it links nothing | out 0, in 0 |
| `source` | leads forward, nothing cites it | out > 0, in 0 |
| `sink` | cited, leads nowhere | out 0, in > 0 |
| `chain` | both directions — mid-conversation | out > 0, in > 0 |
| `edge` | **this line IS an edge**, not a node | `kind="bond"` |

`edge` is not decoration. 13,572 of 25,231 lines here are bonds; scoring them by
their own degrees would file half the corpus as islands when they are the
linking. The shape comes from the degrees, `hub` (`in >= 5`) is reported
separately, and the two are never collapsed — one backlink is not a hub.

### 9.2 Reach walks, and dedups by line

`reach(h, hops)` BFSes out of a line in BOTH directions, levels kept apart: a
line that leads somewhere and a line that cites you are opposite facts, and a
pooled walk would let a reader mistake an outgoing hop for a backlink.

A hop is deduped by **line**, not by edge — two verbs at the same line are one
neighbour with two reasons, and listing it twice reports an address that does
not exist.

`_hop_nodes` ranks by the far end's own degree and reports `total` honestly:
the `ctd` hub has **273** in-edges, and sending all of them is a wall of text.
"showing 14 of 273" is the truth; "14" is not.

### 9.3 A cell is not a line

`explore_cell(rkey)` exists because the plane's unit of reading is an
ADDRESS. A cell raises a question one line cannot: five plain chats and the
definition everything cites look identical as one count, so the answer carries
both altitudes — `shapes` (its lines') and `in`/`out` (its edges'). An untouched
address still answers (`shape: null`, `label`, `why`), which is the honest report
of an empty address.

### 9.4 Census, and the finding it made visible

`pattern_census()` is O(lines + edges) off the adjacency maps. Two invariants
are asserted: **every line lands in exactly one shape bucket**, and `by_app`'s
edges add up to the same total as the `edge` bucket — one number, two readings.

That census is what surfaced the corpus's actual shape:

```
25,231 lines · 13,572 edges · 2,939 cross-platform
edge 54% · chain 25% · isolated 12% · source 6% · sink 3%

app        lines   islands   %  (bonds written)
opencode   6,072       253    4%        0
hermes     5,056     2,637   52%        0
dashboard    524       107   22%        0
index          0         0    —    13,572
```

**opencode is almost entirely linked; hermes is over half islands.** Not a bug:
opencode's sessions chain (`x-next-in-session`) and its lines reach hermes
definitions (`x-uses-term`), while hermes is the SINK — the definitions others
cite, which do not chain among themselves. No single cell can show this.

### 9.5 Cost, and why the cache key is the same pair

One extra pass, `_brief()` (0.40 s at 25k lines), builds hash→text for the walk's
previews; `_adj()` is 0.04 s. Both are keyed on `(bonds_rev, prune_rev)` — the
same pair the edge cache uses, because pruning can strand an edge without the
edge set changing, and a walk into a vanished address is exactly the mistake
that key was invented for. Warm: `reach` 1 ms · `explore_cell` 53 ms ·
`pattern_census` 50 ms.

### 9.6 API

| | |
|---|---|
| `GET /explore?hash=<64hex>&hops=1..4&cap=1..60` | walk one line |
| `GET /explore?rkey=<int>&hops=1..4&cap=1..60` | walk one address (adds `shapes`, `lines`) |
| `GET /patterns` | the whole plane's link shape + hubs + rels + per-app |

`reach()` returns `gone: true` for a pruned line rather than walking into a
removed address. The trail a reader leaves in the UI is **client-side only** — a
sighting says "someone read here", which is a fact; a trail says "I got lost
here", which is not, and writing it would drown the real sightings.

## 10. The magnifier — a viewport over a torus

A lens held over the plane, not another plane scale. Three rules make it a
viewport rather than a second map:

1. It magnifies the SAME data (`PV.cells`), so the glass cannot show a cell the
   map disagrees with. There is no second picture to keep in step.
2. It stops at the **real scale**: the cap is the zoom at which one cell fills
   the lens, because a cell is the finest ADDRESS that exists. Past that the
   glass would only enlarge a single number — so it stops and states the cell's
   count instead.
3. Its position is stored in **plane coordinates**, so changing the base view or
   the mode never teleports the glass.

### 10.1 The cap is derived, not chosen

```
pvLensZMax() = floor( 2r / pxPerCell )
```

`pxPerCell` is px-per-**plane-cell**, which is why the cap means the same thing
in every view: in block view a block is `step` px and a block is 12x12 cells, so
both the drag step and the cap use `step/12`. Expressing either in block px
would have made the glass fly 12x too fast and stop 12x too early.

### 10.2 The level names the RESOLUTION, not the zoom

×4 is "cells" on a laptop and "blocks" on a phone. A multiple therefore means
different things on different screens, so the label reports the level — which
means the same thing everywhere:

| level | test | claim |
|---|---|---|
| `L2` | cell < 3 px | still a density map |
| `L1` | 3–7 px | the 12x12 grid is visible |
| `L0` | >= 7 px | cells are separable; the number IS the data |
| `จริง` | cell fills the lens | stopped — only contents remain |

### 10.3 The glass wraps with the plane

The lens is drawn once per wrapped copy that intersects the canvas (±`px` on
each axis). Near the seam its rim reappears on the far side — the same glass,
not a second one, because content is placed by torus deltas and there is no
edge case to handle. A magnifier that stopped dead at the border would be the
one thing on this torus that does not loop.

### 10.4 A radius needs a band to move in

A lens of radius `r` cannot be centred closer than `r` to the edge of the view.
On the full 144x144 plane that puts ~25 rings permanently out of reach, so the
base becomes a 48x48 WINDOW: the glass gets a band, and the window still pans,
so nothing is unreachable — only out of frame until you slide.

Two consequences, both deliberate:

- **Grab the glass = move the glass; grab the canvas = move the frame.** Two
  different things to hold, told apart by which one the pointer is on.
- `pvLensFollow()` slides the frame when the glass reaches the band edge.
  Without it the glass simply vanishes when dragged past the window edge, and a
  magnifier you can lose is not a viewport.

### 10.5 The feedback loop in the canvas size

The canvas box was `flex:0 0 auto`, so `pstep()` read the box's own
`clientHeight` to size the box it was in. Adding one toolbar row shrank it **and
it stayed shrunk** (observed: 156 px). Both halves now use `min-height:0` and
the canvas box clips, so the measurement is real every frame and every frame
agrees.

### 10.6 Two bugs the lens found in the shape layer

Reporting a cell's shape from its lines' shapes got both of these wrong, and
neither was visible until the lens put a cell's label next to its count:

1. **`edge` was missing from the order.** 13,572 lines are bonds, so an address
   holding only edges is common — those were reported `ว่าง` (empty) while the
   `count` beside them said 9.
2. **A node shape outranked a majority of edges.** A cell of 32 edge lines and
   2 plain ones was called `เกาะ` (nothing links here) — a claim the address
   does not support, since it IS the linking.

Rule now: **majority decides.** Edges name the address only when they outnumber
the lines that have a shape of their own; otherwise the most specific node shape
wins. Asserted both ways in the demo.

## 11. Ops

Hourly: `schtasks chat-pool-flush` → `python pool.py` (flush+push or SKIP).
Manual: `pool.py demo` (wipes!), `tower.py` self-check, `latest --session chat-pool`.
Layout: `pool.py tower.py chat_tower.h SPEC.md | log.jsonl towers/ outbox/`.
GPU: `gpu_fp_vec8.cu` (sm_61, 1 thread/chunk, XOR sink, bilingual comments) —
parity 0/11520 vs CPU, 0.234ms vs 165.9ms (709×) on 10-tower batch.
`gpu_transpose.cu` (A 32×36 → B 36×32 reorder, uint4 moves, 1 thread/slot) —
parity 0/1,658,880 bytes vs CPU, 0.138ms on 10 towers; mapping == sibling().
Vulkan: `vulkan_fp.comp` (1 invoc/chunk, bit-identical uint wrap) on reused
harness `I:/DWGLS/bench/vulkan_bw_pull.c` (CHUNK 144, sums 32B, wg=(n+255)/256,
push 36, C ref + full dump) — parity 0/92160, upload 642MB/s (same wall),
device-read 0.48GB/s (their cooperative-1024B pattern hits 29.2 — headroom
via WG-cooperation restructure, future detail).
`gpu_chain.cu` (fp+transpose, one transfer pair, phased events) — parity 0/0;
10 towers: CPU 1.29ms (fp 1.09+tr 0.20), GPU resident 0.53ms (2.4×),
GPU full-path 5.9–12ms (WDDM transfers lose). Rule: compute proven on GPU;
end-to-end must ride the fast bridge (DRamTile/Gear2), never naive copies.

## 12. The input path — a write refuses loudly, it never fails silently

The plane has a section because it is hard. The input path has one because it is
where a silent failure costs the most and gets noticed the least: `pool.py demo`
kept passing **306/306** through all three faults below, because the Python was
correct and every fault lived in the seam between the handler and the browser.

### 12.1 `json.loads` was unguarded in all four write endpoints

`do_POST` read its body with `json.loads(self.rfile.read(length))` in
`/register`, `/append`, `/block` and `/session-end`. A malformed or empty body
raised straight out of the handler, so the socket closed with **no response at
all**:

```
POST /append '{bad json'   →   curl: http=000                         (before)
POST /append '{bad json'   →   400 {"error":"body must be a JSON object"}  (after)
```

`http=000` is indistinguishable from a dead server. A write path may refuse; it
must not leave the caller guessing which. Bodies now arrive through
`_read_json()`, which returns `None` for absent / malformed / non-object, and
every branch answers.

### 12.2 The label has to name the real cause

Answering with the *wrong* 400 is half a fix. Falling through to the app checks
turned a malformed body into `? not registered — POST /register first`, which
sends the caller to fix the registry. Now that the client **shows** this error,
a label that lies costs a real debugging cycle, so a malformed body is named as
one — and `stream_check` asserts the string, not merely the status.

### 12.3 The composer destroyed the draft on failure

`send()` cleared the `<textarea>` **before** the request resolved:

```js
el.value='';                                  // ← the text is already gone
const r=await fetch('/append',…).then(r=>r.json());
```

Measured with `fetch` rejecting (server restarting, phone off the LAN, rejected
token):

```
before:  input=""   toast=""            unhandledrejection=["Failed to fetch"]
after:   input="ข้อความสำคัญที่ยังไม่ได้บันทึก"
         toast="ส่งไม่ถึงเซิร์ฟเวอร์ — ข้อความยังอยู่ในช่อง (กด Enter อีกครั้ง)"
```

There was no `.catch` at all, so the rejection was unhandled and the UI said
nothing. The rule now: **the draft is dropped only inside `if(r.ok)`** — the
moment the line is in the chain. A message that could not be sent is
recoverable; a message that was erased is not.

The same block guards double-submit: three rapid `send()` calls (Enter held
down) produce **one** `/append`, and the button is disabled while in flight. The
old code got deduplication for free by clearing the box synchronously — which is
exactly the trade that lost the draft.

### 12.4 A rejected token retried forever, in silence

`stream.onerror` was `setTimeout(listen,3000)` — no cap, no diagnosis.
`EventSource` never reports *why* it failed, so a 401 and a dropped connection
look identical; and because `refresh()` runs only on `onopen`, a phone with a
stale QR link never reached the code that shows the QR panel. It reconnected
every 3 s forever, with no message.

`onerror` now probes `/health` — which *can* say 401 — before deciding:

| probe | reading | action |
|---|---|---|
| 401 | permanent refusal | show QR panel, **stop** |
| anything else | transient drop | back off 3 → 6 → 12 → 24 s, capped at 24 s |

A torn SSE frame is no longer fatal either: `JSON.parse` is guarded, so one
malformed frame is skipped instead of killing the feed.

### 12.5 Cost

One extra function call per write; the probe runs only on error. No dependency,
no protocol change, no new endpoint.

## 13. Management — one value, one view

Every fault in the ☰ panel had the same shape: **one value drove two things, and
they were allowed to disagree.** The page rendered, the server answered 200, and
only the meaning of what you were looking at was wrong.

### 13.1 `setIdentity` changed what the page showed, not what the stream sent

The SSE stream carries the identity in its own `as=` filter, captured when
`listen()` builds the URL. Switching "มุมมอง" called `refresh()` — which re-read
*history* once — but never rebuilt the stream, so the **next** line arrived under
the old filter:

```
setIdentity('hermes')  →  stream.url = /stream?n=200      (measured, no `as=`)
after the fix          →  stream.url = /stream?n=200&as=hermes
```

The header said `hermes` while the feed kept showing admin's every line — and
because the feed is the thing you *read*, the wrong one wins. Re-reading once
fixes the past; `listen()` is what fixes the next line.

### 13.2 The picker could display an identity that was not in effect

`#id-sel` was rebuilt from the apps present in the **loaded window**. An identity
with no lines in it was not among the options, so
`if([...isel.options].some(o=>o.value===icur))` failed and the select fell back to
its first option while the `identity` variable stayed put:

```
identity = 'zzz-not-in-window'   select displayed 'admin'   option present: no
```

A picker that disagrees with the value it picks is worse than no picker: the
next choice is compared against something that was never true. Built from
`known ∪ {identity}` now, and set from `identity` — never from the window.

### 13.3 A failed registry read hid the section

`catch{regbox.style.display='none'}` made *"cannot reach the server"* and
*"nobody is registered"* pixel-identical. Same rule as §12: the panel now stays
visible and says which one it is.

### 13.4 Escape closed the modal and then did nothing

The plane modal is `z-index:50` and the drawer is `z-index:40`, and only the
modal had an `Escape` handler. So Escape dismissed the top overlay and left the
scrim on — dismissable only by tapping it — while the drawer itself had no
keyboard dismissal at all. One handler now peels topmost-first, in *stacking*
order rather than by preference.

`/flush` got the same treatment as the composer (§12.3): it is the one button
whose entire job is "did it reach the cloud", so it may not fail in silence.

### 13.5 The check, and why it lives in `ui_check`

`ui_check.py` gained four invariants here (seven in total, once §14 and §15 added
their own), and the first two versions of them were **wrong in an instructive
way**:

1. `identity=a;` `.{0,300}` `listen(` — passed on the broken code, because the
explanatory comment between them was longer than the window.
2. narrowed to a marker slice — still passed, because the comment *itself*
contains the word `listen()`.

The rule is now a **line-anchored statement** (`^\s*listen\s*\(\s*\)\s*;`) inside
a marker-delimited slice: prose cannot satisfy it. Verified by six mutations that
each re-introduce exactly one fixed bug — all six are caught, baseline clean.

> A test that cannot fail is not a test. Both failed versions above looked green
> on the code they were written to protect.

## 14. The plane's cost — a cached base, and a glass that repaints only itself

The magnifier "struggled" because three separate costs were stacked on every
frame. Each one is measurable, and each fix is a different kind of fix.

### 14.1 The backing store was reallocated 30 times for 30 draws

`pvDraw` opened with `pcv.width=px*dpr;pcv.height=px*dpr`. Assigning
`canvas.width` reallocates the backing store **and clears it**, so every call was
a fresh allocation of the whole surface. Measured: **30 assignments for 30
draws**. It is now guarded by a size check, so a redraw into the same size
reuses the surface.

### 14.2 A zoom frame repainted the entire map

One plane-mode draw painted all **10,131 cells in 27.7 ms** (max 37 ms, ~27 fps)
— and the glass was drawn into the *same* canvas, so every wheel tick and every
drag frame paid for the whole plane before the lens put down a pixel.

The base picture is a function of inputs that do not move during a gesture, so
it is rendered once into an offscreen canvas and blitted. The key is the exact
input set, taken by **object identity** wherever the input is a large structure
(`cells`/`bonds`/`sights` are replaced wholesale, never edited in place), so a
stale cache is not a risk to reason about — it is a key mismatch.

`PV.origin` is in that key for a reason worth keeping: in window mode it moves
every cell **without touching `PV.cells` at all**. §10.4's "the frame slides so
the glass cannot be lost" would otherwise leave the base frozen behind the glass
— a bug that would look like a rendering artefact and be blamed on the lens.

### 14.3 The edge arrays were rebuilt for every wrapped copy

`pvGraphEdges()` maps all **13,572 bonds into fresh objects** per call, and
`pvLensOne` calls it once per wrapped copy of the glass — up to four complete
rebuilds per frame. `pvDrawnEdges()` then re-ran the full degree ranking each
time, and `pvKeep()` rebuilt an adjacency map over every edge, also once per
copy. All three are now memoised on the same object identities.

After the base was cached, a drag frame was still **12.8 ms**; that number is
what these three arrays cost. With them memoised: **2.6 ms**.

### 14.4 The cache is not *allowed* to be stale — proved, not argued

Run in the real page against the real corpus:

| check | result |
|---|---|
| cached base vs a fresh `pvBasePaint` of the same inputs | **0 of 504,100 bytes differ** |
| the visible canvas with the glass off | **0 bytes differ** from the base |
| same inputs, twice | 2 hits, 0 misses |
| new `PV.cells` | 1 miss (invalidates) |
| `PV.origin` moved, `PV.cells` untouched | 1 miss (invalidates) |

### 14.5 Numbers

| | before | after |
|---|---|---|
| plane-mode base draw | 27.7 ms (max 37.3) | **0.02 ms** (max 0.1) |
| backing-store reallocations / 30 draws | 30 | **0** |
| zoom drag frame | 12.8 ms (max 19.4) | **2.6 ms** (max 6.3) |
| wheel zoom step | 12.5 ms (max 15.8) | **2.1 ms** (max 5.6) |
| `pvKeep()` | full adjacency walk, per wrapped copy | **0.013 ms** |

### 14.6 Device pixels, and the limit that remains

A fractional `devicePixelRatio` (measured 1.232) against an integer css size gave
a **non-integer backing store**. Now `bw = Math.round(px*dpr)` and the transform
is `bw/px`, so the size and the scale agree by construction rather than by luck.

**What is not fixed:** the cell *grid* still lands on fractional device pixels —
2 css px at dpr 1.232 is 2.465 device px, so cell edges are still slightly soft
at plane scale. Aligning the backing store removes the mismatch between canvas
size and scale; it does not make the geometry land on integers. That would mean
choosing `step` in device pixels and accepting a float css step, which changes
`pstep()` — and `pstep()` is the one function every placement asks (see §10.5),
so it is a change to make deliberately and measure, not as a side effect.

## 15. The popup — clipping made structurally impossible

Measured on the phone breakpoint (540×709):

```
#plane-canvaswrap   overflow:hidden · 165px tall
#plane-pop          164px tall · top=4px → bottom 168px > 165px
clippedBottom = true        ← the bottom row of buttons was cut off
```

The clamp could not have fixed it. `wr.height - popH - 10` evaluates to **-9**,
and `Math.max(4, ·)` puts the popup straight back inside:

> **A popup taller than its container cannot be clamped into it.** No choice of
> number fixes this; only a different container does.

So the popup and the tooltip are now `popover` elements. The **top layer**
escapes `overflow:hidden` by construction, and cannot be painted under the wrong
thing by accident either — which is the other half of the complaint (the two
overlays in §13.4).

Positioning now clamps against the **viewport**, with
`max-height:calc(100dvh - 24px)` and `overflow:auto`, so the clamp is always
satisfiable: the popup **scrolls** instead of being chopped. The tooltip's
`white-space:nowrap` (which produced a 350px box at a 540px canvas) now wraps
under a max-width and clamps to the viewport too.

Verified on the phone breakpoint: the popup's bottom edge is **486px against a
box that ends at 462px** — it extends past the old clipping box and is fully
visible, close button included; the tooltip's right edge is 534 ≤ 540.

And because the popup is anchored to a **point on the canvas** rather than to the
box, a resize or a scroll moves what it points at. It is now re-placed on both;
before, it was placed once and never again.

## 16. The modal's chrome — 272px of a 709px screen

Measured on the phone breakpoint before any of this was touched:

```
plane-head    108px   ← 3 rows, for a caption and seven buttons
plane-bar     115px   ← 14 controls in a wrapping row
plane-ribbon   49px   ← the tick strip
─────────────────────
chrome        272px = 38% of a 709px screen
canvas        175px
```

Three separate causes, and they needed three different kinds of fix — none of
them was "make it smaller".

### 16.1 A 166-character caption took a row of its own

The header's status line was one long `textContent` concatenation
(`เซลล์ · lines · budget · prune · edges · sightings · hub · memory · who/when`).
At 678px wide it wrapped to a full row, which is what made a **caption** the
second-tallest thing in the modal.

Nothing about it was wrong except its volume, so nothing was deleted: it writes
a **short form to the screen and the whole thing to its `title`**. 36 characters
visible, 166 in the tooltip. The numbers are not less true for being short.

### 16.2 Two controls for one axis, in two rows

The time axis appeared **twice**: the scrubber and `▶ replay` in the options bar,
the coloured strip in the next row down. Two controls for one axis in two places
reads as two axes. They are now one row — strip, label, scrubber, replay — which
is also where the reader already looks for "when".

The strip's own label was 349px wide, and a wrapping flex row decides its line
from the **basis** sum, so a label as wide as its content pushed the scrubber
off the row by itself. Fixed basis + ellipsis; full text in its tooltip. Same
rule as 16.1.

### 16.3 Fourteen controls, all of them shouting

The drawing options are worth having and not worth *showing*. They are behind
`⚙ ตัวเลือก`, which names what is behind it, and the state is kept in a variable
so opening it stays open across a mode change — a panel that closes itself every
time you touch something is worse than no panel.

### 16.4 The zoom cluster

46px buttons over the canvas, in one place, with the level readout between `+`
and `−` because the level is the feedback for the two buttons above it.

Two rules make it an instrument rather than three buttons:

* **it stops where the model stops.** `+` disables at the real scale (the same
  place §10.1 promises to stop) and `−` at ×1. A `+` that keeps working there
  would be the UI contradicting the model.
* **⟲ undoes BOTH kinds of zoom** — the magnifier's ×level *and* the view being
  narrowed to a block or a window — and re-centres the glass on the selection, so
  it doubles as "I dragged it somewhere and lost it". The difference between
  "what magnification" and "which patch" is *our* distinction, not the reader's;
  two resets would just make them guess which one they meant.

On a phone the cluster lies down: a 170px column beside a 235px canvas was **72%
of the canvas height** — a control that takes over the thing it controls. As a
row it is 60px, 25%.

### 16.5 `hop` → `ทอด`, and a radius needs a centre

`1/2/3 hop` was labelled with a graph-theory term on a button that did nothing
until a cell was selected — it only produced a toast telling you so. A control
that cannot do its job must not sit there looking available: `1–3 ทอด` are
**disabled until there is a point to be a radius of**, and the tooltip says why
and, once enabled, how many cells are left.

### 16.6 Numbers

| | before | after |
|---|---|---|
| header | 108px / 3 rows | **72px / 2 rows** |
| options bar | 115px, always visible | **70px, behind ⚙** |
| ribbon | 49px | 49px / **1 row** |
| visible chrome | 272px (38% of screen) | **121px (17%)** |
| canvas | 175px | **235px** |
| zoom cluster over canvas | 10px text in the header | **46px buttons, 60px tall (25%)** |

**What is left:** the header is 2 rows, not 1 — a title+status row and a controls
row. Getting to one row would mean cutting the mode buttons to icons, and an icon
row that needs a legend is not obviously better than two honest rows.

## 17. The words — the app is read by people who did not build it

Reading the *screen* as a newcomer, rather than the code, made one thing plain:
of 174 user-facing strings, the load-bearing ones were the names the wire uses.
`tick 3133` is not a name — its meaning lives in `tickOf()`. `chain ✓ 25263
links` is a fact about a hash chain, addressed to someone who has never heard of
one. `flush ไม่ผ่าน: body must be a JSON object` is an English sentence with an
HTTP status attached, shown to a person who can act on neither.

### 17.1 Three kinds of string, three different fixes

Treating them alike is how a UI ends up half-translated:

| kind | example | what it needed |
|---|---|---|
| a **label** naming a thing | `tick`, `registry`, `curator`, `prune`, `bonds` | a plainer word, at *every* place that word appears |
| a **sentence** reporting an outcome | `flush ok`, `copied a1b2c3d4`, `explore: TypeError` | Thai, and no bare identifier left in it |
| an **error** naming a cause | `hermes not registered — POST /register first` | translation at the point of display, not on the wire |

### 17.2 The wire keeps its English; the error is translated where it is read

`error` is an API contract. `pool.py`, the CLI, `stream_check` and every agent
posting JSON read it, and `stream_check` asserts on its wording (`"JSON object"
in error`). Rewriting it in Thai would be translating a machine's vocabulary for
other machines. What was wrong is that the *browser* printed it verbatim. The fix
is layered, not global:

* the wire is unchanged — `{"error": …, "howto": …}` stays byte-identical;
* the dashboard gains `sayError(r, st)`: patterns for the causes it can name in
  Thai, then a table from HTTP status to meaning, then an honest generic;
* `sayRaw(r, st)` keeps the original wording, and `toast(text, detail)` puts it in
  the toast's `title` — one hover away from whoever needs the exact string.

Measured on the live page:

| the wire said | the toast says | the tooltip keeps |
|---|---|---|
| `zzz not registered — POST /register first` | แอป “zzz” ยังไม่ได้ลงทะเบียนไว้กับพูล — ลงทะเบียนก่อนจึงจะเขียนได้ | ข้อความจากเซิร์ฟเวอร์: … · HTTP 403 |
| `body must be a JSON object` | ข้อมูลที่ส่งไปอ่านไม่ได้ — ข้อความนี้ยังไม่ถูกบันทึก | … · HTTP 400 |
| `token required — open /qr from the server screen` | ยังไม่ได้เข้าสู่ระบบ — สแกน QR ใหม่จากหน้าจอเซิร์ฟเวอร์ | … · HTTP 401 |
| `no ctx index yet — run: python ctx_plane.py scan` | ยังไม่มีข้อมูลความจำกลาง — ต้องรันตัวสแกนที่เซิร์ฟเวอร์ก่อน | … · HTTP 503 |
| anything unrecognised with HTTP 500 | เซิร์ฟเวอร์ทำงานผิดพลาด — ลองอีกครั้ง ถ้ายังไม่หายให้ดู log ที่เซิร์ฟเวอร์ | — |

The order is deliberate: a **named cause** beats a **status code** beats a
generic sentence. So an unknown error under a known status reads as that status —
`weird-message` + 400 gives the 400 sentence, which is more useful than whatever
greeting the server happened to emit.

`send()` also had to stop throwing the status away. `fetch(…).then(r=>r.json())`
discards it, so the code that says *whether to retry* never reached the sentence
that says *why not*. It keeps the `Response` now, and a non-JSON body is caught
instead of becoming an unhandled rejection.

### 17.3 The vocabulary, and where the old word still lives

The plain word goes on the screen; the internal one stays in the tooltip, the log,
the CLI and these docs. Nothing was renamed on the wire — a reader who loses
`tick` has lost nothing, but a log that renames `tick` breaks every grep:

| the wire / the code | the screen |
|---|---|
| `tick` (feed + ribbon, = `floor(seq/8)`) | **ช่วงที่ N** |
| `seq` (a line's ordinal) | **บรรทัดที่ N** |
| `hop` / radius depth | **ทอด** (ทอดที่ N) |
| `chain_ok`, `links` | **✓ ประวัติครบ · N ข้อต่อ** / **✗ ประวัติถูกแก้** |
| `registry` | **แอปที่เชื่อมต่ออยู่** |
| `inbox` | **ข้อความถึงคุณ** |
| `curator`, `noise` | **ตัวกรองขยะ** |
| `prune` | **ตัดขยะ** |
| `bonds` / `edges` | **เส้นเชื่อม** |
| `sighting` | **รอยอ่าน** · ช่องที่มีคนบันทึกว่าอ่านแล้ว |
| `hub` / `degree` | **จุดรวม** / **วงตามจำนวนเส้น** |
| `bundle` | **รวมเส้น (คู่เดียวกัน)** |
| `rel` | **ความหมายของเส้น** |
| `memory` / `ctx` / `lifted` | **ความจำกลาง** · เส้นที่เชื่อมข้ามระบบ |
| `rkey` | **ที่อยู่** (คอลัมน์ x, แถว y) |
| `block` | **ความจำที่บันทึกไว้** |
| `flush`, `cloud (pool.db)` | **สำเนา** · สำเนาที่ค้นได้ |
| `lines` / `last append` | **บรรทัดทั้งหมด** / **เขียนล่าสุด** |
| `rev`, `addr` | **แก้ครั้งที่ N**, **ที่อยู่** |
| `open` / `closed` / `parked` | **กำลังทำ / จบแล้ว / พักไว้** |
| `session-block`, `session-end` | **ความจำที่ปิดงานแล้ว** / ปิดงาน |
| `who/when` layer | **ข้อมูลผู้เขียน** |
| `blast radius` | เส้นที่แตะจุดที่เลือกไว้ |
| `View as admin` | **ผู้ดูแล (เห็นทุกอย่าง)** |

### 17.4 Two collisions the pass exposed

Translating a whole screen forces you to notice where one word was doing two jobs.
Both were live:

* **`ช่วง` vs `ทอด`.** The feed's unit is `floor(seq/8)` — a chunk of 8 lines — and
  the rail *already* called it `N ช่วง` while the group header said `tick 12`: one
  thing, two names, on one screen. Renaming both to `ช่วงที่` then collided with
  the plane's hop levels, which rendered `ช่วงที่ 1 · N บรรทัด` for a graph depth —
  a hop is not a span of lines. Hop levels are `ทอดที่` now, the same word the
  buttons use.
* **`บรรทัด` meaning two different numbers.** `#pv-when` showed a *line* ordinal
  labelled `tick`; `#pv-ribbon-info` showed a *chunk* labelled `tick`. Different
  quantities, and they say so now: `บรรทัดที่ 5,000/25,263` against
  `ช่วงที่ 625/3157`. The ribbon's buckets print `แท่งละ 14 ช่วง`, because an
  aggregate that hides its bucket size lies about its own resolution.

### 17.5 The filter's reasons are translated too — and checked across two files

The noise filter names its reasons in code: `test-word`, `hex-blob`, `loud`,
`repeat`… Those names are the audit trail, so they stay on the wire, in the report
and in the CLI. On screen they become **คำทดสอบ**, **ชุดรหัส**, **ตัวอักษรซ้ำมาก**,
**ข้อความซ้ำ**. Same for why a line is protected: `kind`, `dm`, `mention`,
`long`, `marker` → **ไม่ใช่ข้อความคุย (เป็นข้อมูลของระบบ)**, **ส่งถึงคนเดียว**,
**มีการเรียกชื่อ**, **ข้อความยาว**, **มีคำสำคัญอย่าง สรุป/ตัดสิน**.

The failure mode here spans two files: add a rule to `CURATOR_RULES` in `pool.py`
and forget the word in `RULE_TH` in the dashboard, and the screen quietly shows
`hex-blob 3` again. So the check reads **both** files — the rule names out of
`pool.py`, the tables out of the script — and fails on the difference.

### 17.6 Three new invariants, and the mutation test

All three fail invisibly: the page renders, every id exists, the server answers
200, and `pool.py demo` is 306/306.

```
no internal vocabulary on screen          -> CAUGHT  ['the tick label']
  (a text node or a static tooltip carries a word the wire uses)
server errors are translated, not printed -> CAUGHT  ['toast(...+r.error)']
  (the `error` field may be READ only inside sayError/sayRaw)
every filter rule has a Thai word         -> CAUGHT  ['hex-blob', 'loud']
  (a rule added to pool.py with no RULE_TH entry)
```

They are proven the way the rest are — by reverting the bug and watching the
check fail. 17 mutations, 17 caught (12 old + 5 new), baseline clean:

```
restore the tick label                  -> CAUGHT  no internal vocabulary on screen
put prune back in a tooltip             -> CAUGHT  no internal vocabulary on screen
print the raw server error again        -> CAUGHT  server errors are translated
drop a rule word from RULE_TH           -> CAUGHT  every filter rule has a Thai word
add a rule to pool.py without a word    -> CAUGHT  every filter rule has a Thai word
add a protection reason without a word  -> CAUGHT  every filter rule has a Thai word
popup clamped to the clipping box again -> CAUGHT  the popup is clamped to the viewport
… + 10 from §12–16                                 all caught
```

Two details of the vocabulary check are worth recording. It reads **text nodes and
static tooltips only** — a `title` built by the script (`title="'+x+'"`) is skipped,
because its words come from data and policing data is not this check's job. And a
presence test is not a leak: `if(r.error)` decides whether to speak, so it stays;
what may not come back is the string being *rendered*.

```
ui_check  OK (128,902 bytes · 12/12 invariants)     stream_check  14/14
pool.py demo  306/306 PASS    ctx_plane  PASS     prune_check  13/13
```

### 17.7 The check: `error_check.py`, two sources and three tiers

The translation is a *list*, and a list drifts. `ui_check` cannot see it drift —
it only reads the dashboard and never asks the server anything. `pool.py demo` is
306/306 through every one of these errors, because the server side was always
correct. What is being checked is the **join between two files**, so the check
drives a real server over a real socket:

* **LIVE** — 39 malformed requests (every endpoint, every way to be wrong),
  keeping the `(status, error)` pairs the server really answers;
* **SOURCE** — every `{"error": …}` in `pool_server.py`, parsed with `ast` so an
  f-string is expanded the way Python expands it. Two 401s are unreachable from a
  loopback test (`_authorized` trusts 127.0.0.1), and *unreachable by this test*
  must not mean *unchecked*;
* **DASHBOARD** — the translator sliced out of the page and executed in node, so
  the test and the app cannot disagree about which pattern matches.

Each error is then classified by the app's **own** pattern list:

| tier | meaning |
|---|---|
| **NAMED** | a pattern matched — the sentence says what actually went wrong |
| **STATUS** | no pattern, but the status is in the table: true, but generic |
| **GENERIC** | nothing matched — the reader gets no cause at all |

PASS requires NAMED for every error and zero GENERIC anywhere. The gap between
NAMED and STATUS is the point of the test: `text required` and `?n=<int> required`
are *both* 400s, and `ส่งข้อมูลไปไม่ครบหรือรูปแบบไม่ถูกต้อง` tells the person nothing
they did not already know from the failure itself.

### 17.8 What it found on the first run

The first run failed three of six checks — and one of the three was a real bug of
the class §12 already fixed once, on the other side of the protocol:

```
FAIL every-probe-answers       no response at all: tail-n-text -> status=0;
                               view-n-text, stream-n-text, curator-n-text,
                               curator-hide-text, curator-at-text
FAIL every-error-is-named      8 fell back to the HTTP status
FAIL no-traceback-on-bad-params  6650 bytes of stderr
```

`int(qs.get("n")[0])` sat **unguarded** in `/tail`, `/view`, `/stream` and
`/curator` (twice more for its `hide`/`at` thresholds), so `?n=abc` raised straight
out of `do_GET`: the client got **no response at all** (curl reports `http=000`)
and stderr got a traceback. §12's rule — *a request that fails silently and a
server that is gone look identical, which is the one thing an error path must
never be* — was written for POST bodies and never applied to a GET parameter.

All four now answer, through one helper (`_int_param`) that also removes the
inline `int(…) if … else None` casts:

```
/tail?n=abc          http=400  {"error": "?n=<int> required"}
/view?n=abc          http=400  {"error": "?n=<int> required"}
/curator?n=abc       http=400  {"error": "?n=<int> required"}
/curator?hide=abc    http=400  {"error": "?hide=<int> / ?at=<int>"}
/stream?n=abc        http=400  {"error": "?n=<int> required"}
```

…and the 8 unnamed errors each got a sentence. Three, to show why NAMED matters:

| the wire said | status-only said | now |
|---|---|---|
| `title required — it is the one line the next platform reads first` | ส่งข้อมูลไปไม่ครบหรือรูปแบบไม่ถูกต้อง | ต้องมีชื่อเรื่อง — เป็นบรรทัดแรกที่คนอื่นจะอ่าน |
| `status must be one of ('open', 'closed', 'parked')` | ส่งข้อมูลไปไม่ครบหรือรูปแบบไม่ถูกต้อง | สถานะไม่ถูกต้อง — ใช้ได้แค่ กำลังทำ / จบแล้ว / พักไว้ |
| `ctx_plane unavailable: no such table: ctx` | เซิร์ฟเวอร์ทำงานผิดพลาด — ลองอีกครั้ง | อ่านข้อมูลความจำกลางไม่ได้ — ลองรันตัวสแกนใหม่ที่เซิร์ฟเวอร์ |

Note the third: the server's reason (`no such table: ctx`) is a Python detail, so
the *sentence* names the situation and the tooltip keeps the reason — the same
split §17.2 uses for the wire text.

Final state, and the mutation test:

```
  PASS every-probe-answers            39 of 39 probes answered
  PASS harvest-covers-endpoints       21 distinct errors from 39 probes
  PASS no-error-falls-to-the-generic  25 translated, 25 named, 0 status-only
  PASS every-error-is-named           25 named
  PASS tooltip-keeps-the-wire-text    25 carry the server's own wording
  PASS no-traceback-on-bad-params     0 bytes of stderr

unguard /tail?n=                     -> CAUGHT  every-probe-answers
unguard /view?n=                     -> CAUGHT  every-probe-answers
drop one Thai pattern                -> CAUGHT  every-error-is-named
drop EVERY Thai pattern              -> CAUGHT  every-error-is-named
a new error with no sentence         -> CAUGHT  every-error-is-named
sayRaw stops keeping the wire text   -> CAUGHT  tooltip-keeps-the-wire-text
```

The fifth mutation is the one worth having: adding `{"error": "a brand new cause"}`
to `pool_server.py` — an ordinary thing to do while writing a feature — fails the
test until the dashboard can say it in Thai. A cross-file rule enforced by the
check instead of by memory.

```
ui_check  OK (130,576 bytes · 12/12 invariants)     stream_check  14/14
pool.py demo  306/306 PASS    ctx_plane  PASS     prune_check  13/13
error_check  6/6 PASS   (39 probes · 25 errors · 25 named · 0 status-only)
```

## 18. The block map — a second address space, and two gestures

### 18.1 The 12x12 map drew all 144 blocks in one row

Measured in the browser, on the block map as it shipped:

```
Object.keys(PV.cells).length                          144
new Set(keys.map(k => pvXY(k).join(','))).size         12
squares where pvRkeyAt(x,y) is not a block's corner    132 / 144
```

Twelve positions for a hundred and forty-four blocks. `pvDerive` keyed the
aggregation by a **block index** (`by*12+bx`) while `pvXY` reads **cell
addresses** (`(rk%144)/12`, `rk/144/12`), so every square was drawn through the
wrong reader: all of them landed in row 0, overdrawing each other twelve deep,
while the edge lines around them (which carry real addresses) were placed
correctly. The picture looked like a green heat row on top of a cloud of lines —
and since a heat map has no labels, the only symptom available to a person was
*"this map is confusing"*.

The same split had three quieter consequences, all of them lies on screen:

* the `writer` and `recent` colour layers read `PV.cellApp[rk]` / `PV.cellSeq[rk]`
  with a block index, so they were colouring each square by an unrelated cell;
* the radius mask asked `keep.has(blockIndex)` against a set of cell addresses,
  so it never matched anything and dimmed **every** square;
* the glass asked `PV.cells[cellAddress]` of a block-keyed map, so it drew a full
  map as empty and its readout said `×2 1,1 · 0 line` over a block holding 2,228.

### 18.2 One address space, or none

The fix is not a corrected formula in `pvXY`; it is removing the second
numbering. The aggregation now keys each block by its **first address**
(`(by*12)*144 + bx*12`), which is what `pvRkeyAt` already returned, which is what
`pvXY` already consumed, and which `pvCellApp`/`pvCellSeq`/`pvKeep`/`openPoint`
all already spoke. One line changed; four readers became correct at once.

`pvValAt(rk)` is the remaining case where two address spaces must meet: the glass
asks for the value **at a cell**, and on the block map the entries are blocks. So
a cell borrows the count of the block containing it — stated in one function, so
the glass, the tooltip and the level readout cannot each invent their own answer.

```
keys are cell addresses again        new Set(pvXY(k)).size   144
disagreements between the picture and the click   0 / 144 squares
```

### 18.3 Panning the block map

The block map shows the whole plane, so a drag there cannot reveal new ground —
but the plane is a **torus**, and moving the seam is a real view position: the
picture slides rigidly under the finger and comes round after twelve blocks. On
a grid of blocks that is exactly what panning a wrapping map looks like, and it
is the same modulo the window already pans with. So the drag is shared: one
`panDrag`, one click-swallow, two coordinate pairs, `PV.bOrig` in **block**
coordinates (0..11) and `PV.origin` in cells. Measured: dragging right three
blocks and down one gives `bOrig {x:9, y:11}` (= -3, -1 mod 12) and the drawn
grid matches a rigid rotation of the previous one **144/144 squares**.

The info line says what a drag does here before it is tried, because the honest
answer ("every block is already on screen") is not the one the gesture suggests:

```
ผังย่อ 12×12 · ทั้งระนาบอยู่ในจอแล้ว — ลาก = เลื่อนขอบผัง (วนกลับที่เดิมทุก 12 บล็อก)
```

`⟲` resets the seam too: "show me the whole thing again" cannot restore
everything except the thing the reader actually moved.

### 18.4 The pinch walks extent, not magnification

A phone has no wheel, and the `±` cluster magnifies the **glass**. What a map
gesture means to everyone is *how much am I looking at* — so the pinch walks the
extent ladder, which is the mode buttons' own ladder with the frame sizes the
picker offers, and nothing in between:

```
   12  ->  24  ->  36  ->  48  ->  72  ->  blocks  ->  plane
  (out)                                    (in)
```

An extent with no name is an extent the reader cannot come back to, so the
ladder and `#pv-win`'s `<option>`s must be the same five numbers — an invariant
tests exactly that, cross-control. It stops where the frame stops: outward at the
whole plane (a picture smaller than the screen is all frame and no plane),
inward at 12x12, where the glass takes over because a frame can go no finer than
one cell per square. Both stops **say** they stopped.

Pinching out of the block map opens the frame over the block under the fingers:
measured from a spread at the block map's (6,8), `PV.win` becomes 12 and the
origin `{wedge:72, ring:96}` = that block. Pinching in walks back out:
`24, 36, 48, 72, blocks, plane` — six rungs, then the stop.

What the touch code had to add is only bookkeeping, and it is bookkeeping that
fails invisibly:

| must happen | why | wrong answer looks like |
|---|---|---|
| the second finger drops `panDrag` and `lens.drag` | one pointer both panning and pinching moves the frame twice per frame | "the map jumped" |
| a pinch sets the click-swallow | lifting two fingers would open whatever cell was under them | "it opens a cell I did not touch" |
| a cancelled pointer releases the frame | a system gesture leaves a drag with no finger on it | "it pans on its own" |

### 18.5 The invariant RUNS the code

The first version of this check was a regex over the source — *pvXY mentions
`PV.bOrig`, pvRkeyAt calls `pvBlockKey`* — and it **passed on the bug**: both
functions mentioned the seam, and the arithmetic that turned a block key into a
cell address was still wrong (`key*12` is the block's column at the wrong row).
A shape check cannot see arithmetic. So the check now extracts `pvWrap`, `pvB`,
`pvXY`, `pvRkeyAt`, `pvDerive` and `pvValAt` out of the served script, hands them
to node with a stub `PV`, and asserts that they agree:

```
15 seam offsets x 144 squares:  the address is a block's first cell,
                                the square reads back to the same square,
                                every cell inside the block lands on that square
144 blocks x 3 cells:           a cell reads the count of its own block
```

Every mutation caught (7/7), including the two that were invisible before:

```
the block map keys squares by a block INDEX again -> CAUGHT  336 disagreements
the block click ignores the seam                  -> CAUGHT  8064
the picture ignores the seam                      -> CAUGHT  8064
pvB stops wrapping at twelve                      -> CAUGHT  5648
the lens asks a block key for a cell's value      -> CAUGHT  288
pinch step off the picker's ladder                -> CAUGHT  the ladder invariant
two fingers keep the drag they interrupted        -> CAUGHT  the gesture invariant
```

Verified in the browser as well, because the probe is not the page: a tap reads
a cell, a click after a pinch does not, the glass drags and zooms, a frame still
pans with one finger.

```
ui_check  OK (138,016 bytes · 15/15 invariants)     stream_check  14/14
pool.py demo  306/306 PASS    ctx_plane  PASS     prune_check  13/13
error_check  6/6 PASS   curator_check  37/37   reconnect_check 14/14   latency_check  8/8
```

## 19. The kind layer — what KIND of line landed here, and two bugs it shook out

Added 2026-10-03. A colour layer, not a mode: `PV.colour='kind'` joins
`density`/`heat`/`writer`/`recent` and reads `cell_kind[rk]` — the kind of the
**last** line to land on that address. Free, for the same reason `cell_app`
was (§14.2's fold, already being made): the live edge of the log answers "what
was this cell last" exactly after every append, while counting per kind would
cost a dict per cell to say less.

It is not decoration, because the plane cannot answer this without it. A cell
holding 34 lines shows the number **34** whether those are 34 bonds (one hub)
or 34 chats (one conversation) — the two are opposites to a reader, and §9.1
already showed that a shape read off the wrong altitude is a claim the address
does not support.

### 19.1 What the corpus says, measured before building it

```
32,685 lines · CHAT 13,746 · bond 13,572 · DECISION 5,026 · pointer 302 · block 32
cells occupied 13,210   (cell,kind) pairs 16,750 = 1.27×   1 kind: 9,919 · 2: 3,042 · 3: 249
```

Three candidate AXES were measured and two were rejected, which is why this
shipped as a colour and not as a mode:

| axis | verdict | the number |
|---|---|---|
| **kind as geometry** | rejected | CHAT centroid (71.6, 74.2), DECISION (71.9, 69.9) — **4.2 cells apart** of 144. Two views of the same picture. |
| **time as geometry** | rejected | 32,685 lines need **227 rows** of 144; the plane has 144, so it wraps at row 83 and paints over itself — an image that lies about its own extent. |
| **app as swimlanes** | **kept for later** | the corpus is three non-overlapping eras: hermes `0–11,655` · index `11,656–25,229` · chatdrop `25,521–32,635` = 79% of the log. The plane shows **none** of it, because it sums every line into one number per cell. |

`bond` is the one kind that does separate on its own: its own median spread is
**16** cells against CHAT's 49 and DECISION's 51, and its centroid sits 42.2
from CHAT's. It is a distinct mass on the plane that nothing could name — which
is exactly what a missing label looks like.

### 19.2 The bug the layer shook out, twice, in the same shape

**`pretty()` compared `kind == "chat"` exactly** ([pool.py](pool.py)), and the
corpus writes `CHAT`. Every one of **13,746** plain chat lines rendered as
`hermes/CHAT` instead of `hermes`. It is the defect §6 already paid for in
`curator_protect()`, in a function nobody was looking at.

**The feed badge did the same thing.** `kind==='chat'?'':` is false for `CHAT`,
so all 13,746 chat lines grew a badge reading **"CHAT"** — the one wire name
the screen has a plain Thai word for. And while fixing it: `KIND_TH` existed
**twice**, one inside `card()` for the feed and one for the plane. Two tables
for one job is the §17.5 drift in miniature — whichever copy a renderer reads
can lag the other, and the page renders perfectly either way. There is now one
table, at the top of the script, read by both, and a check that fails if a
second one appears.

> A kind is a **value**, never a spelling. The rule was already written down,
> in a different file, about a different function — which is what "the rule
> lives in the code and not in the reader's head" costs.

### 19.3 The check's own comment satisfied the check

The new invariant counts literal `==='chat'` comparisons and requires exactly
one. Its first run failed on **correct** code: the sentence explaining why
`kind==='chat'` is wrong *contains* `kind==='chat'`. That is §13.5 inverted —
that rule stopped a check being satisfied **by** a comment, and this one was
satisfied **by** one. `_code_matches()` blanks `/* */` blocks and skips any
match preceded by `//` on its line, so prose can neither pass nor fail a rule.

Accepted limit, stated rather than hidden: a `/*` inside a JS regex or string
would be read as a comment. The dashboard has none, and the failure direction
is a check that under-reports — never one that passes broken code.

### 19.4 The whole map, per kind, is the NEXT step and it is cheap

```
cells occupied 13,210  →  (cell,kind) pairs 16,750   = 1.27× the current map
per-kind payload as JSON ≈ 187 KB vs ≈ 147 KB now
```

A `kind → {rkey: count}` fold answers "show me only bonds, dim the rest"
without a second walk, and the cells that are pure (`1 kind: 9,919`) dominate
precisely because the mixture is in the **minority**: 3,291 cells hold more
than one kind, so a per-kind map is 1.27× rather than 8×. The cost is not
storage — it is that "dim the rest" needs `PV.cells` to keep BOTH maps at once,
which is a decision about what the plane is allowed to forget.

```
ui_check  OK (164,554 bytes · 29/29 invariants)      pool.py demo  315/315
stream_check 14/14   error_check 6/6   curator_check 37/37
reconnect_check 14/14   latency_check 8/8   ctx_plane PASS   taskboard 16/16
mutations: drop a word / add a table / revert the badge / drop a colour /
           leave a word untranslated                            → 5 of 5 CAUGHT
```

## 20. The kind filter — showing less, never more

Added 2026-10-03. §19 shipped the *label* of a cell ("what KIND of line landed
here last"). §19.4 named the NEXT step and its price; this is that step. One
`<select>` beside the colour picker: pick a kind, and the plane draws only the
cells holding it, everything else in neutral grey.

It is a filter and **not** a mode, and that distinction is the whole design.
§18's modes (`plane`/`blocks`/`window`) answer *where is this address*; a filter
answers *how much of the picture is this*. It touches no addressing function,
inherits §18's "one address space, or none" rule for free, and cannot disagree
with the counts because it reads the same `rkey` keys.

### 20.1 The map, split by kind — measured, not estimated

```
cells 13,210 (122 KB)  →  (cell,kind) pairs 16,750 (155 KB)  = 1.26×

kind        lines  cells  lines/cell   %lines   %cells
CHAT        13746   9532       1.44     42.0%    56.9%
bond        13572   2651       5.12     41.5%    15.8%
DECISION     5026   4452       1.13     15.4%    26.6%
pointer       309     77       4.01      0.9%     0.5%
block          32     32       1.00      0.1%     0.2%
msg             6      6       1.00      0.0%     0.0%
bridge          1      1       1.00      0.0%     0.0%
```

The fold is inside the one that was already running (§19.2's `derived()`), so
there is no new pass and no new walk — the same §14.2 argument that made the
kind *layer* free makes the kind *filter* 26% cheaper than a second map would
have been, because it rides the same dict walk.

The table is the finding, and it is not what §19 predicted. `bond` holds **41.5%
of all lines in 15.8% of the cells** — 5.12 lines per cell against CHAT's 1.44.
§19 measured bond's centroid as 42.2 cells from CHAT's and called it "a distinct
mass"; the filter makes that concrete, because filtering to bonds does not
dim 16% of the picture, it **deletes 84% of the cells and keeps the loudest
ones**. A reader switching kinds is not recolouring a map, they are seeing a
different map: 9,532 scattered cells become 2,651 hubs.

> Two thirds of the extra payload is `bond` and `pointer` — 2,728 cells costing
> 33 KB. The cheap kinds are the ones nobody filters for.

### 20.2 The contract: the picture may show LESS, never more

A filter is the one feature that can make a map lie in a new direction. Not by
crashing, not by showing nothing — by showing **more** of something than the
plane holds, which no reader has any way to catch. Three rules, each one a
function so it can be run and not merely read:

```js
function pvFilt(){return(PV.kindOnly&&PV.kinds[PV.kindOnly])||null}   // absent kind ⇒ NO filter, never a phantom one
function pvKindN(rk){const f=pvFilt();return f?(f[rk]||0):0}        // a cell holding none counts 0, not the total
function pvInkAt(rk,rel){                                             // filtered-out ⇒ grey, never a kind hue
  const f=pvFilt();
  if(f&&!f[rk])return 'rgba(139,148,158,0.13)';
  return pvCellInk(rk,rel);}
function pvDrawMax(){                                                  // ramp ceiling = busiest cell OF THE FILTER
  const f=pvFilt(),vals=f?Object.values(f):Object.values(PV.cells);
  return vals.length?Math.max(...vals):1;}
```

`pvDrawMax()` is the one that is easy to leave out and impossible to notice.
Without it the colour ramp is still normalised to the *unfiltered* busiest cell,
so filtering to `pointer` — 4.01 lines/cell but only 77 cells — tints the map
without moving a single shade: the filter appears to do nothing on the one kind
where it would help most.

**The numbers a reader sees stay TOTAL.** The picture counts the filter; the
printed counts do not. This is not a compromise, it is the only choice that
keeps both true: a cell holding 34 lines printed as `0` while the screen shows
something there is a contradiction, and printed as `34` while the screen shows
nothing of it says the filter did nothing. So the tooltip carries **both**,
labelled — `ที่อยู่ 0a81e5cf (คอลัมน์ 12, แถว 99) · 34 บรรทัด · เส้นเชื่อม 12
(เหลือ 22 ชนิดอื่น)` — and the legend states the split outright:
*สีและความสว่าง นับเฉพาะชนิดนี้ · ช่องที่ไม่มีชนิดนี้จางเป็นสีเทา ·
ตัวเลขที่ผู้อ่านเห็น (ช่อง / แว่น / ทิป) นับทุกชนิดเหมือนเดิม.*

The status line counts what is lit (`สว่าง 2,651 ช่อง · 13,572 บรรทัดของชนิดนี้`)
so a reader can always see the denominator without hovering.

### 20.3 Three places the filter had to be threaded, and one it nearly wasn't

A filter that reaches the main paint but not the rest is a filter that works
on the picture you are looking at *now* and lies the moment you move.

| surface | what had to change | what happens without it |
|---|---|---|
| base + ghost passes ([pool_server.py](pool_server.py)) | both call `pvInkAt()` and size their ramp with `pvDrawMax()` | the ghosted under-layer would stay fully coloured under a greyed filter |
| the magnifier ([pool_server.py](pool_server.py)) | `pvLensOne()` reads the filtered count for its caption | the lens zooms in on a map that is not the one on screen |
| the **time scrub** (`pvCellsUpTo()`) | `if(only&&row[3]!==only)continue;` | drag time back with "bonds only" on and every other kind comes straight back |

The scrub is the one that matters. It rebuilds the plane from `PV.timeline`
rather than from `PV.cells`, so it is a second implementation of the same
counting rule — and it is the only surface whose numbers a reader can watch go
backwards. The invariant that guards it is one line of regex:

```
a filter on the time scrub is the same filter as on the live plane
```

And the honest failure mode is stated rather than designed out:
`PV.kindOnly` naming a kind with no cells **keeps the filter on and says the
kind is empty here**. Falling back to the unfiltered map would draw the whole
plane under a filter that claims to be on — the one answer this layer must
never give.

### 20.4 The probe that tested a copy of the code

`ui_check` gained a node probe for the filter, and its **first version pasted
its own copies** of the four functions. Three mutations of the real code passed
it: `pvKindN` returning the total, filtered-out cells painted as if they
matched, the ramp ceiling ignoring the filter.

> A test that cannot fail is not a test. Neither is one that tests a copy of
> the thing — §13.5 again, from the other direction: that rule stopped a check
> being satisfied **by** a comment, and this one was satisfied **by** itself.

`_js_function()` now slices the four functions out of the **served script** and
runs those. Two supporting fixes were needed first, both of which had been
failing silently in the three existing node probes:

- `_probe_count()` reads the trailing number off `bad.join(' | ') + ' :: ' + n`
  instead of stripping the line and testing it for emptiness — `.strip()` +
  `rpartition(" :: ")` ate the leading space, so **every probe reported failure
  on passing code**. Three call sites replaced.
- the probe writes its source with `chr(10)`, because `"\n"` inside the Python
  heredoc that builds it arrives as a real newline and splices the JS string
  across two lines. It bit three times.

With the real functions running, the invariant is one sentence and it is the
section title: **the kind filter may show less, never more** — asserted by
comparing `pvKindN(rk) > PV.cells[rk]` for every cell, which no regex can see.

```
ui_check  OK (169,392 bytes · 32/32 invariants)       pool.py demo  321/321
stream_check 14/14   prune_check 13/13   reconnect_check 14/14   error_check 6/6
curator_check 37/37   latency_check 8/8   ctx_plane PASS   taskboard 16/16
taskboard_check 17/17
mutations: pvKindN returns the total / filtered-out painted as a match /
           ramp ceiling ignores the filter / an absent kind becomes no filter
                                                       → 4 of 4 CAUGHT
run_all  11/11 suites PASS (26.7s)
```

### 20.5 The browser found five, and every check was green

Everything above was written before the feature was ever **opened**. A sandbox
server (a copy of the corpus on its own port, never the reader's 8390) then
drove the real control. It did not work on the first try, and not on the fifth.

| # | what a reader saw | why every check was green |
|---|---|---|
| 1 | **the dropdown never opened at all** | `pvKindOptions` guarded its one line with `ks.join('|')`, and `ks` is a **count object**. `ks.join` is `undefined`, so the guard itself threw a `TypeError` on every path that had kinds to show. Four correct maths functions sat behind an unreachable `<select>` |
| 2 | it was **disabled with one option** before the layer loaded | a disabled `<select>` cannot fire the handler that loads the layer it needs. The only way in was to first pick a meta-backed **colour** — a control the reader has no reason to touch |
| 3 | the fix for 1 wrote `if(have[0]!=='')return` | true when the list is **empty** (`have[0]` is `undefined`), so it returned early on the one case it existed to handle |
| 4 | every kind read **`(0)`** | `pvTopKinds` counted with `n[a]=n[a]||0` — read a count, wrote it back, added nothing. Sorting a column of zeros still looks sorted |
| 5 | the list said **`เส้นเชื่อม (1961)`**, selecting it lit **2,651** | the list quoted the **colour layer** ("cells whose last line was this") while the filter counts cells that **hold** one. Two numbers about one click |

and then, once it worked, two more that only exist under time:

| # | what a reader saw | why |
|---|---|---|
| 6 | scrub time back and the line still said **13,572** while the picture held 1,421 | the status line counted the filter over the **whole log**. The prune figures three lines above had already been fixed for exactly this reason ("say it only about the LIVE plane"); the filter had not learned it |
| 7 | the first fix for 6 said **7,938** against a picture holding 1,421 | while scrubbing `pvCellsUpTo` has **already** applied the filter, so `src` *is* the answer — summing the whole-log `f[k]` over it counts bonds that have not happened yet |
| 8 | the fix for 7 reported **"no data in this view"** while 943 cells were lit | `lit=scrubbed?src:Object.keys(src).filter(…)` makes `lit` an **array** on one branch and an **object** on the other, then asks `lit.length`. An object has no `.length` |

**The pattern is one bug, eight times.** Every one of them is a *wiring* fault
between correct arithmetic and a `<select>`/a caption — and every check in this
file is arithmetic or a regex. `kind_filter()` runs the four maths functions and
proves `pvKindN(rk) ≤ PV.cells[rk]` for every cell; that is a true statement
about a feature that could not be opened once.

So two checks were added, both of which failed on the code that shipped:

- **`kind_list()`** builds a `<select>` in node out of the **real**
  `pvKindOptions` and the **real** `pvTopKinds`, then asks whether the reader
  gets a list they can use. `_js_arrow()` slices the arrow-shaped one, because a
  stubbed count passes a function that returns zeros.
- **"a control that needs a layer can ask for it"** — the static rule that no
  `sel.disabled=!ks` may come back.

Its first run failed on **correct** code for the third time in this file, and
the reason was its own harness: the stub compared `$` against `'pv-kind'` while
the real `$` receives `'#pv-kind'`, so `if(!sel)return` fired and the probe
reported every clause broken while testing nothing.

> A check that cannot see its own harness is worse than no check, because it
> looks like coverage.

```
ui_check  OK (173,486 bytes · 35/35 invariants)       pool.py demo  321/321
stream_check 14/14   prune_check 13/13   reconnect_check 14/14   error_check 6/6
curator_check 37/37   latency_check 8/8   ctx_plane PASS   taskboard 16/16
taskboard_check 17/17   run_all 11/11 suites PASS

mutations  11 attempted · 11 CAUGHT
  arithmetic: pvKindN returns the total / filtered-out painted as a match /
              ramp ceiling ignores the filter / an absent kind becomes no filter
  the list:  ks.join guard reverted / the select disabled again /
              __load option dropped / no-churn guard back to an early return /
              pvTopKinds counting without the +1 /
              nLit quoting the colour layer / sorting by the colour layer
  the frame: figures counted over the whole log / the scrubbed branch summing
              f[k] / either branch ceasing to be an array

verified in a browser, against ground truth read from the corpus:
  LIVE   screen "สว่าง 2,651 ช่อง · 13,572 บรรทัด"  =  2,651 cells / 13,572 lines
  SCRUB  screen "สว่าง   943 ช่อง ·  1,421 บรรทัด"  =    943 cells /  1,421 lines
```

### 20.6 The mode is a name, not a cell count

`pstep()` decides how large a cell may be drawn, and it decided by asking how
many cells there were:

```js
const lo=w===144&&h===144?2:8, hi=w===144&&h===144?8:84;
```

Read plainly, the plane's rule was **"if there are many cells, make them
tiny"**. That is a coincidence that happens to hold for exactly one mode. Every
mode that is not 144 wide inherits the plane's pitch range by accident, and the
day a mode *is* 144 wide and is not the plane, it will be told it is. §19
noticed it; §14.6 forbade fixing it as a side effect, because at the time
`PV.n` was one number standing for two axes and the fix had nowhere honest to
stand.

Both of those are now behind us. `PV.n` is split into `PV.w`/`PV.h` across all
eighteen call sites in its own commit — each named for what it means (the
ghost-zone bound, the half-window, the lens's wrap stride and the hit-test are
per-axis; the canvas stays square and is sized by the longer side) — so the
cell count is two honest numbers rather than one coincidence. And the mode is
already a name, already set by every mode switch in the file. The fix is two
lines:

```js
const [lo,hi]=PV.mode==='plane'?[2,8]:[8,84];
```

`pvCenter()` had the same shape one line of truth away: it tested
`PV.mode==='plane'&&PV.w===144` for the torus ghost-zone bound, after
`pvDerive()` had *already* set `PV.w=PV.h=T144` in that mode. The second test
could only ever agree with the first — or, the day a plane mode is not 144
wide, disagree with it and clamp the wrong bound. It is now `PV.mode` alone.

Rendered output is byte-identical in all three modes, which is the point: the
inference was never load-bearing, it was load-bearing **only by accident**.

The invariant that guards it is negative and structural — *a cell count never
stands in for a mode* — because a positive test would just be a second copy of
the rule that could drift from the first. It asserts no `=== 144` survives in
executable code, and that `pstep` branches on `PV.mode`. Two mutations prove it
is not self-satisfying: putting the inference back (`w===144&&h===144?2:8`) and
tying the pitch range to a different mode name (`'blocks'`) are both caught,
and the first is caught by the regex alone.

```
ui_check OK (174,914 bytes · 37/37 invariants)   pool.py demo 321/321
run_all 12/12 suites PASS   ui_browser_check 18/18 PASS
```

### 20.7 What is still not answered

The kind filter answers "which cells hold this kind". Both axes §19.1 measured
are now settled — one rejected, one paid for — and one is still owed:

- **app as swimlanes** — the one axis the corpus has and the plane does not.
  The corpus is three non-overlapping eras: hermes `0–11,655` · index
  `11,656–25,229` · chatdrop `25,521–32,635` = 79% of the log, and the plane
  sums every line into one number per cell, so it shows none of it. The kind
  filter is the same *mechanism* `by_kind` is — a fold that partitions cells by
  a second value — so `by_app` is the identical line of code with a different
  key, and the measured cost is already known (26%). It is a filter on identity,
  not a mode, and needs none of the six addressing functions §18 demanded.

## 21. The browser check — a suite that clicks something

Added 2026-10-03. `ui_browser_check.py` boots a sandbox pool-server, opens the
real dashboard in headless Chrome, **drives the real control**, and grades what
the page *rendered* against the corpus read independently in Python. It is the
twelfth suite in [run_all.py](run_all.py) and costs about ten seconds.

§20.5 recorded eight bugs that reached a green `run_all`, all of them between
correct arithmetic and a `<select>`. This is the check that class of bug needed.

### 21.1 No dependencies, on purpose

There is no playwright, no selenium, no jsdom and nothing to install. Chrome is
launched with `--remote-debugging-port=0`, which makes it **print** the
DevTools socket rather than be told where to listen, and that socket is driven
with node's **built-in** `WebSocket` (node ≥ 22). A hard-coded debugging port is
a collision waiting for the day `run_all` is run twice; that is why the port is
read out of the child's stderr rather than chosen.

The search for a browser is deliberately broad — five Windows paths plus five
bare names — because a check that only knows one browser path is a check that
silently stops running on the other machine. If neither Chrome nor Edge can be
found it **SKIPS, loudly, naming what it did not run**. It never reports a pass
it did not earn (§13: a check that cannot fail is not a check, and one that
pretends to have passed is worse).

### 21.2 The truth is computed on the other side of the process

The page reports what it saw; Python decides whether that was right. The corpus
is read with `pool.derived()` in the sandbox — the same library that will serve
it — and the sandbox's own log is built with a deliberate kind mix, so the
filter has something to choose *between* and the cells that hold two kinds are
not a hoped-for accident.

That split is the whole design. A page grading itself proves only that its
arithmetic agrees with itself, which is the defect §20.5 shipped: four correct
functions behind an unreachable `<select>`. Here the dropdown's number, the
status line's number and the frame's own cell map are each compared to a
figure derived from the log, and "the picture may show less than the corpus
holds, never more" is asserted per kind against that figure.

Two numbers are graded against **two different truths**, because they answer
two different questions and the corpus disagrees with itself about them:

```
filter   by_kind        "which cells HOLD this kind"      CHAT 28 · bond 18
layer    cell_kind      "which cell was LAST this kind"   CHAT 21 · bond 16
```

Shipping the layer's number in the filter's box is one of the eight bugs, and a
check that used one truth for both would not have caught it.

### 21.3 It was wrong three times before it was right

The first version printed **4/15 PASS** on a dashboard that had opened, loaded
its layer and listed every kind correctly — because it read the steps off the
wrong level of the driver's answer (node wraps the page's reply as
`out.report`), so every field came back `None` and `None` compared equal to
nothing. Three checks passed on absent data:

> The lesson from §20.5 again, one level up. A check that cannot see its own
> harness is worse than no check, because it looks like coverage.

Absence is now failure everywhere: a frame that reports no count fails the
comparison, a driver that answers nothing fails before any number is read, and
`the browser actually ran` asserts the dashboard's own script executed rather
than that Chrome started. The suite also learned to wait for **the app**
(`typeof PV === 'object'` and the plane button's `onclick` attached) rather
than for `document.readyState`, which is `'complete'` on the blank page it
started from — polling it alone clicks the button before the script has run and
reports a dashboard that will not open, which is the failure it exists to catch.

### 21.4 What it proves

```
ui_browser_check  18/18 PASS   (chrome headless, sandbox corpus of 93 lines)
mutations 8 attempted · 8 CAUGHT — every bug §20.5 lists, reintroduced one at a
  time: the ks.join guard / the disabled control / the missing __load option /
  the count that never increments / the box quoting the colour layer / the
  figures counted over the whole log / the scrubbed branch summing f[k] /
  `lit` an object asked for .length

run_all  12/12 suites PASS (41.1s)   ui_check 36/36 invariants
```

One of the eight was **missed on the first pass** and is worth recording: the
count that never increments. Zeroing `pvTopKinds` no longer breaks the
dropdown, because the dropdown counts a *different* map — so the browser check
was blind to it while `ui_check` was not. Adding the legend assertion closed
it, and the legend was worth grading anyway: it is user-visible text that said
`(0)` for every kind for as long as the bug lived.

```