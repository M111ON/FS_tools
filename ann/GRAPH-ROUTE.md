# Graph route — the train-free rough map (2026-10-08)

## The doctrine this implements

> "ระบบนี้ต้องเทรนก่อนครั้งนึง เพื่อเปิด path … กรณีที่เราทำแบบนั้นไม่ได้ graph
> เข้ามาช่วยทำแผนที่คร่าวๆ ให้เป็นแนวทาง ระบบจะแม่นและเร็วขึ้นหลังจากปัก tombstone"

Two failure modes of "you must train first":

1. **No trained route yet** — a fresh index has no `kv_anchors.bin`, so the
   server falls back to `mode:"brute"`: a full O(N) cosine scan.
2. **Train can't run** — small/streamed indices below `LZ_ANCH_MIN`, or a
   dimension the trainer won't touch.

The **graph route** replaces that full scan with a guided subset drawn from the
file-relationship graph (`data/graft/`): the graph already knows which entries
mention which files and how files relate, so it can *propose candidates* with no
training at all. The tombstone then pins the routes actually walked.

## What was built

`ann/src/ann_search.c`:

- **sid→entry CSR** (`g_file_ent_off`/`g_file_ent_ids`) — per file id, the
  entries that carry it. Built once in `graft_init`.
- **`graft_candidates(out, cap)`** — for the query's file keys, score entries
  carrying the exact file (`1.0`) plus entries carrying a **neighbour** file
  (`edge affinity`), and return the top-`cap`.
- **`lz_mem_search` no-anchor branch** — when there is no trained route and the
  graph can propose, it scores that subset (`route.mode = "graph"`,
  `budget = |candidates|`). Falls back to the full scan when the graph proposes
  nothing, so a **novel query keeps exact behaviour**.
- Knob: `ANN_GRAFT_CAND` (candidate cap; default = index size).

Env-gated by `ANN_GRAFT=1`; **off by default**, strict no-op when off.

## Measured — fixture (deterministic, `I:\tmp`, 6/6 PASS)

| check | result |
|---|---|
| no anchors + graft ON → `mode:"graph"` | PASS |
| guided subset scored < total | PASS (2/5) |
| top-1 carries the named file | PASS (`e0`) |
| unknown file → `mode:"brute"` full scan | PASS (5/5) |
| graft OFF → full scan (no-op) | PASS (5/5) |

## Measured — real 57k index (honest, with the anchor route DISABLED)

`ANN_ANCH_PATH` pointed at a missing file to force the no-anchor path; brute
(`graft OFF`) is the exact reference.

| query | graph mode | scored (graph) | scored (brute) | R@10 vs brute |
|---|---|---|---|---|
| `core/geo_wang_latch.h tombstone warm route` | graph | 20 | 57076 | **0.00** |
| `tools/gguf_lazy_serve.c anchor route buckets` | graph | 266 | 57076 | 0.60 |
| `core/anchor_route.h centroids nearest bucket` | graph | 180 | 57076 | 0.60 |
| `core/geo_id_route_log.h p3 log crc` | graph | 11 | 57076 | **0.00** |
| `core/gguf_box.h routing engine zero-copy` | graph | 96 | 57076 | 0.10 |
| `tools/maze_walk_cli.c greedy descent` | graph | 206 | 57076 | 0.10 |
| `geofs mdim multivolume` (no file token) | brute | 57076 | 57076 | 1.00 |
| `no such file zzz/nope.xyz` | brute | 57076 | 57076 | 1.00 |

**Mean R@10 over file-named queries: 0.343.**

## Honest read (do not overclaim)

1. **The win is scan cost, not recall.** The graph scores **11–266 of 57,076**
   entries (0.02%–0.5%) — a **200×–5000×** smaller scored set — but recall@10
   against exact brute is only **0.34**. It is a *rough* map, exactly as the
   doctrine says, not an exact route.
2. **Bimodal.** Files well-represented in the index (`gguf_lazy_serve.c`,
   `anchor_route.h`) hit **0.60**; sparsely-represented files
   (`geo_wang_latch.h` → 20 entries, `geo_id_route_log.h` → 11) hit **0.00**.
   The exact top-10 for a semantic query is mostly entries that are *near but do
   not name the file*, and pure structure cannot see them.
3. **The fallback is correct.** A query with no known file token, or an unknown
   file, returns `mode:"brute"` and is exact (R@10 = 1.00). Novel recall is
   untouched by design.
4. **This is a front tier.** It belongs in front of the exact path for
   file-centric traffic (fast, rough), with the tombstone making repeats cheap —
   not as a replacement for the anchor route or exact search.

## Backward / reciprocal ("magnetic") score — what can be computed backwards

The score is our own formula, so it is **invertible** (measured, exact):

```
squash:  s = w/(w+k)   ->  w   = k·s/(1-s)        (max abs error 8.5e-14)
magnet:  v = x·ret^rho ->  ret = (v/x)^(1/rho)    (max abs error 2.2e-16)
```

So a stored score can be run back to its raw weight and its return strength —
nothing is lost. The interesting "backwards" is on the **graph direction**:

**The graft graph is a dependency DAG.** 0 of 2,091 non-self file pairs have a
reverse edge, so a *direct* reciprocal (`min`/`geomean` of A→B and B→A) is 0
everywhere and is useless here. The magnet has to be measured **indirectly**:

- `ret(A,B) = |out(A) ∩ out(B)| / |out(A)|` — how much B's orbit points back into
  A's orbit. **275 / 2,091 pairs (13%)** have a non-zero return, touching
  **259 / 836** files. This is implemented as `magnet()` in `scripts/graft_bias.py`
  (`--magnet indirect --rho 0.5`).

**Backward candidate expansion** (`ANN_GRAFT_BACKWARD=1`): a file that points at
the query's file (an **in-edge**) contributes its entries. This is the transpose
of the forward walk and it is where a leaf/header's context lives:

| file | forward degree | in-degree | candidates F → B |
|---|---|---|---|
| `core/geo_wang_latch.h` | 1 | 8 | 20 → **101** |
| `core/anchor_route.h` | 0 | 20 | 180 → **370** |
| `core/geo_id_route_log.h` | 0 | 5 | 11 → **21** |
| `core/gguf_box.h` | — | — | 96 → **238** |

### Measured (real 57k index, anchor route disabled, brute = reference)

| query | sc_F | sc_B | R@10_F | R@10_B |
|---|---|---|---|---|
| `core/geo_wang_latch.h tombstone warm route` | 20 | 101 | 0.00 | 0.00 |
| `tools/gguf_lazy_serve.c anchor route buckets` | 266 | 266 | 0.60 | 0.60 |
| `core/anchor_route.h centroids nearest bucket` | 180 | 370 | 0.60 | 0.60 |
| `core/geo_id_route_log.h p3 log crc` | 11 | 21 | 0.00 | **0.10** |
| `core/gguf_box.h routing engine zero-copy` | 96 | 238 | 0.10 | 0.10 |
| `tools/maze_walk_cli.c greedy descent` | 206 | 206 | 0.10 | 0.10 |

**Mean R@10: forward 0.233 → backward 0.250.** Backward widens the candidate set
(and lifts the sparsest file off 0.00), but the recall gain is small — because the
true top-10 for a semantic query is mostly entries that do **not** name the file
and are not structurally adjacent to it. Structure (forward or backward) is a
rough map, not the semantic answer. Both directions remain env-gated; forward is
the default.

## Bake + graph+anchor (2026-10-08, measured on the real 57k index)

**Magnet baked into `data/graft`.** `scripts/graft_bias.py --magnet indirect
--rho 0.5` regenerated the shipped tables (275/2,091 non-zero neighbour weights;
backup of the one-way tables kept). Recall vs the one-way tables is **unchanged**
(mean R@10 0.233) but the candidate set is **smaller** (e.g. `gguf_lazy_serve.c`
266 → 56) — magnet drops the one-way-only edges and keeps the reciprocal ones, so
it is more *selective*, not more accurate.

**Graph + anchor bucket union** (`ANN_GRAFT_BUCKETS=1`): when a trained route
exists, the buckets the graph's proposed entries land in are **added** to the
walked set. Measured at `LZ_ROUTE_BUCKETS=4` (a genuine subset), `topk=5`:

| | mean R@5 vs exact | buckets |
|---|---|---|
| A4 anchor, 4 buckets, λ=0 | 0.967 | 4 |
| AG4 anchor + graph buckets, λ=0 | 0.967 | 4–7 |

The union is **monotone** (adding buckets never lowers recall) but buys **no
recall here** — the 4-bucket anchor route already covers the exact top-5.

**Honest caveat — the bias, not the union, moves ranking.** With the graft bias
ON (λ=0.05), the same config *lowered* mean R@5 to 0.367 — the +λ cluster nudge
promotes structurally-related entries that are **not** the cosine top-5. The
bucket union is safe; the **bias** is the thing that reorders results. Do not
attribute the drop to the union. This is exactly why the bias is default-OFF and
separate from the routing knobs.

## Next levers (not done)

- **Front-tier fallback**: return graph hits, and fall back to the full scan
  when the graph subset is tiny or the top score is below a confidence floor.

## Reproduce

```sh
# force the no-anchor path by pointing anchors at a missing file
ANN_GRAFT=1 ANN_GRAFT_DIR=data/graft \
ANN_INDEX_PATH=data/memcore/kv_index.jsonl \
ANN_ANCH_PATH=I:/tmp/none.bin ANN_ORDER_PATH=I:/tmp/none.order \
  ./build/ann_search.exe --serve 8096
# then POST /v1/state/search {"query":"tools/gguf_lazy_serve.c route","topk":10}
# route.mode == "graph", items_scored << items_total
```
