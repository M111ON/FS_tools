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

## Next levers (not done)

- **Widen the neighbourhood** (2-hop, or co-file expansion) to lift recall for
  sparse files — trade scan cost for recall.
- **Front-tier fallback**: return graph hits, and fall back to the full scan
  when the graph subset is tiny or the top score is below a confidence floor.
- **Graph + anchor**: once a route IS trained, use the graph to *bias the bucket
  order* (the structural axis) rather than to propose candidates.

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
