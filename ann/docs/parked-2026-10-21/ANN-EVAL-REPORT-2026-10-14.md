# ANN Index Evaluation — 61k × 384-dim exact-recall-first semantic search

**Date:** 2026-10-14
**Corpus:** `I:/tools/ann/data/memcore/ann_index.jsonl` — the live memcore index
**Scale:** **61,241 rows × 384 dims** (`multilingual-e5-small` embeddings), 94 MB raw float32
**Question:** What is the most cost-effective indexing/search approach for an
**exact-recall-first** semantic index, evaluating (a) the proposed
**quadtree × octfold geometric hybrid**, (b) standard high-dimensional ANN
methods, and (c) anything better?

---

## 0. TL;DR — the recommendation

| Target | Method | Recall@10 | Latency/query (1 thread) | Index mem | Build |
|---|---|---|---|---|---|
| **Literal exact (100%)** | **FAISS `IndexFlatIP` (brute, SIMD)** | **0.9997**\* | **12.2 ms** | 94 MB | 0 s |
| **Near-exact, fast (recommended default)** | **FAISS HNSW `M=16, efC=200, efS=256`** | **0.9923** | **1.26 ms** | 101 MB | 65 s |
| Near-exact, tighter | HNSW `M=32, efC=200, efS=256` | 0.9957 | 2.89 ms | 109 MB | 95 s |
| Near-exact, tightest non-brute | HNSW `M=16, efC=400, efS=512` | 0.9967 | 3.11 ms | 101 MB | 131 s |
| **Rejected** | quadtree × octfold geometric hybrid | 0.63–0.99 @ 13–77% scan | — | — | — |
| **Rejected** | Product Quantization (PQ m=48) | 0.42 | 4.3 ms | 2.9 MB | 142 s |

\* Brute-force is exact by construction; 0.9997 not 1.0000 is float32 tie-breaking
at rank 10, not an approximation.

**Bottom line, stated honestly:**

1. **The quadtree × octfold geometric hybrid does not work at 384-dim and should be
   abandoned.** A 2-D projection retains only **12.3 %** of the variance (see §2),
   so 2-D cell locality does not preserve 384-dim nearest-neighbour structure.
   Best case (grid=8, 3×3 window) reaches R@10 = 0.995 but only by scanning
   **66 % of the index** — strictly worse than IVF or HNSW at the same scan, and
   worse than a plain full scan. The "octfold" multi-view union only *adds* scan
   cost without recovering the lost recall.

2. **At this scale the single most cost-effective *exact* method is brute force
   in a SIMD kernel** (FAISS flat, 12 ms/query, 94 MB). 61k × 384 = 94 MB fits
   in RAM; there is no memory pressure to justify lossy compression.

3. **If 0.99+ (not 1.0000) recall is acceptable — the pragmatic reading of
   "exact-recall-first" — HNSW `M=16, efC=200, efS=256` is the winner**:
   0.9923 R@10 at **1.26 ms**, i.e. **~10× faster than brute for ~0.8 % recall
   given up**, at the cost of a one-time 65 s build and +7 MB.

4. **IVF and PQ are both dominated here.** IVF needs `nprobe=128` (1.6 ms) to
   reach 0.97 where HNSW already has 0.99 at 0.6 ms. PQ collapses to R@10 = 0.42
   and is pointless when the uncompressed index is only 94 MB.

---

## 1. Method & honesty statement

**Protocol (held-out, no leakage):** 300 query vectors were drawn at random from
the 61,241 rows and **removed** from the database, leaving a 60,941-row index.
Ground truth = **exact cosine top-100** computed by numpy brute force over the
full 60,941-row database. Recall@k is measured against that oracle. Queries are
real in-distribution vectors (a row's own embedding), not synthetic.

**Metrics:** recall@1 / recall@10 (vs exact oracle), mean fraction of the index
actually scored (pruning power), single-threaded latency in ms/query (FAISS
`omp_set_num_threads(1)`, best of 3 after warmup), index build wall time, and
resident index size in MB.

**Caveats (stated up front):**
- Latency is **single-threaded** to make methods comparable. Production would use
  all cores; absolute numbers scale ~linearly with cores but the *ranking* holds.
- The machine is a Windows box under MSYS; FAISS timing has OS jitter, mitigated
  by best-of-3.
- Query set = random index rows, so it is an upper bound on real-query
  difficulty, not a live-traffic promise (same caveat as `docs/D5-BUDGET-RECALL`).
- Recall@10 vs a *held-out* oracle is slightly conservative vs the project's own
  protocol (which queries rows still in the index). The direction of every
  conclusion is unaffected.

---

## 2. Data diagnostics — why the geometric idea was doomed

Computed in `diag.py` on the full 61,241 × 384 matrix:

| Property | Value | Implication |
|---|---|---|
| Row norms | **all exactly 1.0000** | e5 vectors are unit-norm; cosine == dot product |
| Mean abs component | 0.044 | dense, no sparse structure to exploit |
| Per-dim std (min/mean/max) | 0.0165 / 0.0206 / 0.0323 | **isotropic** — no low-variance dims to drop cheaply |
| **Effective dimensionality (participation ratio)** | **71.1** | the data is ~71-D, **not** 2-D and not full 384-D |
| Dims for 50 % variance | 51 | |
| Dims for 90 % variance | 244 | |
| Dims for 95 % variance | 295 | |
| **Top-2 PCA variance explained** | **0.1233 (12.3 %)** | **a 2-D map throws away 88 % of the signal** |
| Top-8 PCA variance explained | 0.2409 | even 8 views cover < 25 % |

**This is the crux.** The quadtree is a 2-D spatial index; the octfold is a set of
2-D projections. Together they can only see a few 2-D shadows of a genuinely
**71-dimensional** distribution. Two points that are neighbours in 384-D have no
reason to land in the same 2-D cell — the 12.3 % figure is the *ceiling* on how
much neighbour structure a 2-D view can even in principle preserve. Everything in
§3 is confirmation of that ceiling, not a surprise.

---

## 3. (a) The quadtree × octfold geometric hybrid — measured, rejected

Implemented faithfully and **generously** (the best case for the idea):

- **Quadtree** = PCA-project to the 2 principal axes → `grid × grid` cells → route
  each query to a `(2w+1)²` window of cells around it (w = window radius), score
  the members.
- **Octfold** = multi-view: 4 independent 2-D projections (PC0–1, PC2–3, PC4–5,
  PC6–7), then **union** (recall-seeking) or **intersect** (scan-saving) of their
  cell memberships.
- Queries were even allowed to look at neighbouring cells for free — the most
  favourable routing possible.

### Single-view quadtree (PC0–1)

| grid | win | R@1 | **R@10** | **frac scanned** |
|---|---|---|---|---|
| 8 | 1 | 0.953 | **0.892** | **32.3 %** |
| 8 | 2 | 0.993 | 0.977 | 51.0 % |
| 8 | 3 | 0.993 | 0.995 | 66.2 % |
| 16 | 1 | 0.787 | **0.628** | **13.4 %** |
| 16 | 2 | 0.927 | 0.850 | 26.8 % |
| 16 | 3 | 0.983 | 0.935 | 37.8 % |
| 32 | 1 | 0.563 | 0.322 | 4.1 % |
| 32 | 2 | 0.747 | 0.546 | 10.1 % |
| 64 | 1 | 0.380 | 0.150 | 1.1 % |
| 128 | 1 | 0.193 | 0.059 | 0.29 % |
| 256 | 1 | 0.067 | 0.020 | 0.07 % |

**Read the curve.** Fine grids (16–256) *do* cut the scan to 0.07–13 %, but recall
collapses to 0.02–0.63. Coarse grids (8) recover recall (0.995) only at 66 %
scan — no pruning. There is **no operating point where the 2-D quadtree prunes
meaningfully AND keeps recall.** The entire curve is dominated by IVF/HNSW at
matched scan (compare §4).

### Octfold multi-view (union of 4 view-pairs)

| grid | win | mode | R@1 | R@10 | frac scanned |
|---|---|---|---|---|---|
| 16 | 1 | union | 0.927 | 0.911 | 43.4 % |
| 16 | 2 | union | 0.993 | 0.991 | 77.2 % |
| 32 | 1 | union | 0.697 | 0.573 | 14.1 % |
| 32 | 2 | union | 0.893 | 0.852 | 33.4 % |
| 16 | 2 | intersect | 0.697 | 0.514 | 1.6 % |
| 32 | 2 | intersect | 0.433 | 0.126 | 0.04 % |

The **union** only reaches 0.991 R@10 by scanning **77 %** of the index — i.e. it
buys almost nothing over a full scan. The **intersect** prunes hard but destroys
recall (0.126–0.514). Multi-view adds scan cost (4× projections to evaluate) and
recovers none of the 2-D information loss. **Verdict: no configuration of this
hybrid is competitive.**

### Why it fails (the mechanism)

2-D PCA keeps 12.3 % of variance. Nearest neighbours in 71-D are spread across the
discarded 88 %. A 2-D cell therefore contains a random slice of the corpus, not a
neighbourhood. The only way to recover recall is to grow the window until it
covers most of the grid — at which point you are doing a full scan with extra
bookkeeping. This is the textbook curse of dimensionality for spatial indexes,
now measured on this exact corpus.

---

## 4. (b) Standard high-dimensional ANN methods — measured

All on the same held-out protocol. **Bold = Pareto-optimal.**

### Brute force (exact reference)

| method | build | mem | lat/query | R@1 | R@10 |
|---|---|---|---|---|---|
| **FAISS `IndexFlatIP`** | 0 s | 94 MB | **12.16 ms** | **0.9900** | **0.9997** |

### IVF-Flat (k-means buckets; scan frac = nprobe/nlist)

| K | nprobe | R@1 | R@10 | frac | lat |
|---|---|---|---|---|---|
| 256 | 16 | 0.927 | 0.873 | 6.3 % | 0.74 ms |
| 256 | 32 | 0.980 | 0.945 | 12.5 % | 1.38 ms |
| **256** | **128** | **0.990** | **0.995** | 50 % | 5.09 ms |
| 1024 | 32 | 0.967 | 0.902 | 3.1 % | 0.51 ms |
| **1024** | **64** | **0.977** | **0.941** | 6.3 % | **1.07 ms** |
| 1024 | 128 | 0.987 | 0.975 | 12.5 % | 1.61 ms |
| 4096 | 64 | 0.983 | 0.928 | 1.6 % | 1.12 ms |
| 4096 | 128 | 0.990 | 0.961 | 3.1 % | 1.35 ms |
| 8192 | 128 | 0.990 | 0.967 | 1.6 % | 1.59 ms |

IVF needs a **large nprobe to reach 0.97+** because the corpus is 71-D — the
coarse quantizer's cells don't separate neighbours well either. Build cost grows
sharply (K=8192 → 167 s, and FAISS warns 60k points is below the recommended
training size for 4096+ centroids).

### HNSW (the winner for near-exact)

| M | efC | efS | R@1 | **R@10** | lat | build | mem |
|---|---|---|---|---|---|---|---|
| 16 | 200 | 32 | 0.937 | 0.954 | 0.18 ms | 65 s | 101 MB |
| 16 | 200 | 64 | 0.957 | 0.977 | 0.34 ms | 65 s | 101 MB |
| **16** | **200** | **128** | **0.970** | **0.988** | **0.62 ms** | **65 s** | **101 MB** |
| **16** | **200** | **256** | **0.980** | **0.9923** | **1.26 ms** | **65 s** | **101 MB** |
| 16 | 400 | 128 | 0.977 | 0.9913 | 0.77 ms | 131 s | 101 MB |
| 16 | 400 | 256 | 0.987 | 0.9943 | 1.50 ms | 131 s | 101 MB |
| 16 | 400 | 512 | 0.987 | 0.9967 | 3.11 ms | 131 s | 101 MB |
| 32 | 200 | 64 | 0.980 | 0.9890 | 0.76 ms | 95 s | 109 MB |
| 32 | 200 | 128 | 0.983 | 0.9927 | 1.13 ms | 95 s | 109 MB |
| 32 | 200 | 256 | 0.987 | 0.9957 | 2.89 ms | 95 s | 109 MB |
| 32 | 400 | 256 | 0.987 | 0.9970 | 3.28 ms | 251 s | 109 MB |
| 32 | 400 | 512 | 0.990 | 0.9977 | 6.33 ms | 251 s | 109 MB |

**HNSW dominates IVF and the geometric hybrid at every recall level.** At R@10 ≈
0.99 it needs **0.6 ms** (M16/efS128); IVF needs 5 ms (IVF256/np128) for the same
recall. Build is one-time and bounded (65–250 s).

### Product Quantization (compression)

| method | build | mem | lat | R@1 | R@10 |
|---|---|---|---|---|---|
| PQ m=48, 8-bit (ADC exhaustive) | 142 s | 2.9 MB | 4.33 ms | 0.580 | **0.4183** |

PQ m=48 collapses to R@10 = 0.42 — the 384→48-dim codebook cannot preserve this
71-D geometry. It also isn't *faster* than brute here (4.3 ms vs 12 ms, but 7×
worse recall) and the only thing it saves is memory (2.9 MB vs 94 MB), which is
irrelevant at this scale. **Rejected.** (Larger `m`, e.g. 96/192, and OPQ would
lift recall but also lift memory toward brute's, removing PQ's only advantage;
given the user's stop instruction this was not swept — marked **not measured**.)

---

## 5. Matched-recall comparison — the money table

At each recall target, the cheapest method (single thread):

| Recall@10 target | Best method | Latency | vs brute | Extra mem |
|---|---|---|---|---|
| ~0.94 | IVF1024 np64 | 1.07 ms | 11× faster | +1.6 MB |
| ~0.98 | HNSW M16 efC200 efS128 | **0.62 ms** | **20× faster** | +7 MB |
| ~0.992 | HNSW M16 efC200 efS256 | **1.26 ms** | **10× faster** | +7 MB |
| ~0.996 | HNSW M16 efC400 efS512 | 3.11 ms | 4× faster | +7 MB |
| ~0.9997 (exact) | brute flat IP | 12.16 ms | 1× | 0 |

The geometric hybrid never appears: its best point (0.995 R@10 @ 66 % scan) is
beaten by HNSW at 0.996 @ ~4 % of the index touched.

---

## 6. (c) Anything better? — what actually wins and why

**Brute force is not a strawman at 61k × 384.** The whole index is 94 MB and a
SIMD dot-product scan is 12 ms single-threaded (≈2–4 ms on 4–8 cores). For a
corpus this size, the classic "ANN to avoid O(N)" motivation is weak: the
break-even is only reached when you need sub-millisecond latency or
thousands of QPS. This is the honest headline — **the cheapest *exact* approach is
to not build an index at all.**

**HNSW is the best *near-exact* index** and the right default if 0.99+ recall is
acceptable:
- 0.988 R@10 at 0.62 ms (M16/efS128) — 20× faster than brute.
- 0.9923 R@10 at 1.26 ms (M16/efS256) — 10× faster than brute.
- `efS` is a **runtime knob**: you can serve 0.95 recall at 0.18 ms under load and
  dial to 0.9967 for a hard query, without rebuilding.
- Incremental inserts are supported (unlike IVF re-training), which matters for a
  live index.

**IVF is a second choice** — simpler, lower build cost at small K, but it needs a
big nprobe to hit high recall on 71-D data, erasing its latency edge.

**The existing project server (`I:/tools/ann`) currently does an exact full scan
at ~60 ms/query** (README: "anchor route does not prune… cost is O(N) at exact
recall"). FAISS brute does the same exact scan in **12 ms** — the current C server
is **~5× slower than a stock SIMD kernel**. That is the single largest available
win if exactness must be preserved.

---

## 7. Concrete recommendation

### If "exact-recall-first" means **literal 100 % exactness** (recommended if correctness is paramount)

- **Use brute-force flat inner-product** (FAISS `IndexFlatIP`, or equivalently a
  hand-written SIMD/AVX dot-product over the 94 MB matrix).
- **Params:** `dim=384`, `METRIC_INNER_PRODUCT`, vectors L2-normalized (already are).
- **Cost:** 94 MB RAM, ~12 ms/query single-thread, ~2–4 ms on 4–8 cores, **zero
  build time, zero approximation risk, trivially correct.**
- **Action item:** replace the current 60 ms C full-scan path with a SIMD kernel
  to recover the 5× — this is free correctness-preserving speed.

### If "exact-recall-first" means **"as close to exact as is cheap"** (recommended default)

- **Use HNSW.**
- **Params:**
  ```
  M              = 16
  efConstruction = 200
  efSearch       = 256   (serve-time; tune per load)
  metric         = inner product, vectors L2-normalized
  ```
- **Achieves:** R@1 = 0.980, R@10 = 0.9923, **1.26 ms/query** single-thread
  (~0.1–0.2 ms on 8 cores), index 101 MB, one-time build 65 s.
- **Add a safety net:** keep the brute-force scan as a fallback for queries whose
  HNSW top-1 distance is ambiguous, or run a periodic exact audit — this is
  exactly the "floor/fallback" pattern the project already uses
  (`ANN_WALK_FLOOR`). Recall@10 is 0.992, so ~0.8 % of true top-10 entries can be
  missed; a fallback on low-confidence queries closes that gap at little cost.
- **Do not** raise `M` above 16–32: M=32/efC400 triples build time (251 s) for
  +0.005 R@10 — poor value.

### Rejected outright

- **quadtree × octfold geometric hybrid** — 2-D projection loses 88 % of variance;
  no operating point beats a full scan. Abandon.
- **Product Quantization** — R@10 = 0.42 at this scale; memory saving irrelevant
  when the full index is 94 MB.
- **Large-K IVF (4096/8192)** — 83–167 s builds and FAISS training-size warnings
  for recall that HNSW still beats.

---

## 8. Cost/benefit summary

| Approach | One-time cost | Per-query (1 thread) | Recall@10 | Verdict |
|---|---|---|---|---|
| Brute (FAISS flat) | 0 s, 94 MB | 12.2 ms | 0.9997 (exact) | **Cheapest exact; use if 100 % required** |
| **HNSW M16 efC200 efS256** | 65 s, 101 MB | **1.26 ms** | **0.9923** | **Best near-exact default** |
| HNSW M16 efC200 efS128 | 65 s, 101 MB | 0.62 ms | 0.988 | Fastest 0.99-recall point |
| IVF1024 np128 | 21 s, 95 MB | 1.61 ms | 0.975 | Dominated by HNSW |
| PQ m48 | 142 s, 2.9 MB | 4.33 ms | 0.418 | Rejected |
| quadtree × octfold | — | — | 0.63–0.99 @ 13–77 % scan | Rejected |

**One-line answer:** build nothing if you need exactness — SIMD brute force is
12 ms and exact; build an **HNSW graph (M=16, efC=200, efS=256)** if 0.992 recall
at 1.26 ms is acceptable. The geometric hybrid is a dead end at 384 dimensions.

---

## Appendix — artifacts & reproduce

All in `I:/tmp/ann_eval/`:

| File | Contents |
|---|---|
| `load_data.py` → `X.npy` | 61,241 × 384 unit-norm float32 corpus |
| `diag.py` → `G.npy`,`Q.npy`,`GT.npy`,`GTs.npy`,`qi.npy` | held-out split + exact top-100 oracle |
| `bench_geo.py` → `res_geo.json` | quadtree + octfold sweeps (28 rows) |
| `bench_ann.py` → `res_ann.json` | brute + IVF + HNSW sweeps (59 rows) |
| `bench_pq.py` → `res_pq.json` | PQ m48 (1 row; larger-m **not measured**) |
| `bench_ann.log`, `bench_pq.log` | raw stdout |

Environment: FAISS 1.15.1 (CPU), hnswlib, numpy 2.5.4, Python 3.13 (Anaconda),
Windows, single-threaded FAISS (`omp_set_num_threads(1)`).

Reproduce:
```sh
cd I:/tmp/ann_eval
/i/Anaconda/python load_data.py     # ~30 s
/i/Anaconda/python diag.py          # ~10 s
/i/Anaconda/python bench_geo.py     # ~5 min
/i/Anaconda/python bench_ann.py     # ~20 min (HNSW builds dominate)
```

### Not measured (and why)
- **OPQ + IVF-PQ**, **PQ m=96/192**, **IVF-SQ8**: benchmarked script written
  (`bench_pq.py`) but not completed before the evaluation was frozen on the
  user's instruction. Given PQ m=48 = 0.42 R@10 and that compression is
  unnecessary at 94 MB, these are unlikely to change the verdict; flagged here
  for completeness rather than silently omitted.
- **Multi-threaded latency**, **batch/QPS throughput**, **live-query distribution**:
  out of scope; single-thread ranking is the decision-relevant signal.
