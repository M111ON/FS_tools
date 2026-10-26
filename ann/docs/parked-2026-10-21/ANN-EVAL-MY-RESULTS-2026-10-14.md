# ANN index evaluation — MY independent prototype results (2026-10-14)

Companion to the delegated report. All measurements run on the REAL index
`I:/tools/ann/data/memcore/ann_index.jsonl` (61,241 × 384-dim e5-small vectors),
ground truth = exact numpy cosine top-10 oracle. Python: `/i/tools/cloud-workspace/.venv`
(numpy 2.5.2) and `/i/Anaconda/python` for hnswlib.

## Live server baseline (measured via HTTP :8096)
- `mode: anchor+p`, `items_scored = 24000 / 61241` (pruned walk already active)
- latency: min 36 ms, mean 158 ms, max 1241 ms (includes embed sidecar round-trip)
- route.us_per_item ≈ 0.8–1.2 µs

## Test 1 — IVF (kmeans buckets) recall@10 vs fraction scanned (n=8000, 50q)
| K | topb | recall@10 | frac_scanned |
|---|---|---|---|
| 8 | 1 | 0.758 | 0.127 |
| 8 | 2 | 0.866 | 0.254 |
| 32 | 1 | 0.626 | 0.035 |
| 128 | 1 | 0.518 | 0.012 |
| 512 | 1 | 0.402 | 0.004 |

**Finding:** recall collapses as you prune harder — classic curse of dimensionality.
K=8 is the sweet spot for the *existing* config; more centroids ≠ better recall at
fixed budget.

## Test 2 — 2-D geometric routing (PCA → grid, 3×3 window) — the user's idea
| grid | cells | recall@10 | frac_scanned |
|---|---|---|---|
| 4 | 16 | 0.972 | 0.546 |
| 8 | 64 | 0.852 | 0.292 |
| 16 | 256 | 0.598 | 0.130 |
| 32 | 1024 | 0.316 | 0.041 |
| 64 | 4096 | 0.184 | 0.010 |

**Finding:** 2-D routing is *comparable* to IVF at matched scan fraction — NOT
better — and recall collapses under pruning exactly like IVF. The 2-D projection
discards almost all 384-dim structure; grid=4 needs to scan 55% of the index to
get recall 0.97. **The quadtree/octfold geometry does not beat plain buckets here.**

## Test 3 — Multi-view (2 independent partitions) union/intersect (n=8000, K=16)
| mode | recall@10 | frac_scanned |
|---|---|---|
| single A (topb=1) | 0.588 | 0.070 |
| union A∪B (topb=1) | 0.713 | 0.097 |
| intersect A∩B (topb=1) | 0.500 | 0.044 |
| single A (topb=2) | 0.755 | 0.142 |

**Finding:** union beats single-A at topb=1 (0.713 vs 0.588) but **costs more scan
(0.097 vs 0.070)**; compared at matched scan (union 0.097 vs single topb=2 at
0.142 → union is more efficient per scan). So multi-view has a *mild* efficiency
edge, but it is NOT free redundancy — it is a re-partition, same as IVF with more
probes. No new capability.

## Test 4 — HNSW (the decisive comparison)
n=20000 sample:
| M | build | ef | recall@10 | ms/query | speedup vs exact |
|---|---|---|---|---|---|
| 16 | 27 s | 50 | 0.989 | 0.75 | 8.8× |
| 16 | | 200 | 0.998 | 2.98 | 2.2× |
| 32 | 31 s | 50 | 0.995 | 0.67 | 9.8× |
| 32 | | 100 | 0.998 | 1.00 | 6.6× |

Full 61,241 index:
```
EXACT numpy scan: 22.0 ms/query
HNSW build: 85.7 s
HNSW query: 0.77 ms  -> 28.4x faster than exact
   ef  recall@10   ms/query  speedup
   10     0.9680      0.205     107x
   20     0.9845      0.493      45x
   50     0.9930      0.962      23x
  100     0.9950      1.241      18x
  200     0.9955      3.055       7x
mem: vectors 94MB + HNSW graph ~16MB = ~110MB
(raw jsonl on disk: 230MB)
```

**Finding:** HNSW at ef=50 gives recall@10 = **0.993 at 0.96 ms** — ~23× faster
than the exact scan, and ~100× faster than the live server's mean latency (which
is dominated by the embed sidecar, not the scan). At ef=20, recall 0.984 at 0.49 ms.

## Bottom line (my independent read)
1. The quadtree×octfold geometric hybrid does **not** beat plain IVF at 384-dim;
   the 2-D projection is the killer. This confirms REPORT.md section E's result
   (C == O, no new capability) *and* adds: even the pure 2-D structure loses to
   simple buckets.
2. HNSW is the clear winner: 0.99+ recall at <1 ms, ~110 MB, 86 s build.
3. The live server's real bottleneck is the **embed sidecar round-trip** (mean
   158 ms vs 36 ms min), not the vector scan — so a scan speedup alone may be
   masked by embed latency.
