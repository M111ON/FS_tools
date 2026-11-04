# Geometry Retrieval for LC / ANN — Full Report

**Date:** 2026-11-05
**Location:** `I:\tools\ann\experiments\LC_tilecube`
**Scope of this report:** candidate retrieval, granularity selection, and index
lifecycle for a geometric (icosphere) index. It does **not** claim anything
about LC addressing, memory layout, or storage.
**Status:** experimental architecture with measured props. Not a production
vector DB.

---

## 1. Question and what was actually tested

Starting question: does a **geometric topology** (icosphere) improve *semantic
candidate retrieval* compared with an unstructured graph, or does any apparent
gain come from the partition simply having bigger buckets?

The whole investigation is a sequence of ablations, each removing one competing
explanation. It ended with a **locked control** and a two-dataset replication.

Two roles are fixed everywhere in this work (see `fd3_hasse.py`,
`ico_cost_quality.py`):

| element | role |
|---|---|
| icosphere **vertex** | cell / address |
| icosphere **edge** | traversal relation |

Icosphere subdivision counts (`V_n = 10·4^n + 2`, `E_n = 30·4^n`, `F_n = 20·4^n`):

| level | vertex | edge | face |
|---|---|---|---|
| 0 | 12 | 30 | 20 |
| 1 | 42 | 120 | 80 |
| 2 | **162** | 480 | 320 |
| 3 | 642 | 1,920 | 1,280 |
| 4 | 2,562 | 7,680 | 5,120 |

"162 cells" therefore means **level-2 vertices**, not a fixed constant.

---

## 2. Experiment timeline (what each step ruled out)

| step | script | finding | what it eliminated |
|---|---|---|---|
| Hasse BFS vs geometries | `fd3_hasse.py`, `hasse_*.py` | Hasse / Pyrochlore / Random all within ~0.01 recall | graph *structure* as a lever (under a coarse partition) |
| Ablation A: random graph | `hasse_ablation.py` | random graph ≈ Hasse (−0.005) | the idea that Hasse's specific edges matter |
| Ablation B: shuffled neighbours | `hasse_ablation.py` | shuffled ≈ ordered (0.005) | edge *order* |
| Ablation C/D: rerank, PCA | `hasse_hybrid_readpath.py` | without rerank recall collapses (+0.250 from rerank) | "rerank is cosmetic" |
| XYZ as jump controller | `hasse_xyz_direction.py` | correlation(edge alignment, GT hit rate) = **−0.115** | XYZ direction as a retrieval guide |
| MemCore 3-layer metrics | `real_data_ablation.py` | start node supplies 94–100 % of recall at 20 FD cells | "traversal is doing the work" |
| Pyrochlore diagnosis | `pyrochlore_diagnostics.py` | candidate-pool Jaccard 0.888–0.901 between methods | "the collectors find different things" |
| SIFT1M uncapped | `sift1m_validation.py` | start recall 0.835, scan 12.8 % under a "5 %" budget | budget enforcement honesty |
| **Granularity fix** | `sift1m_granularity.py` | 162-cell partition: **Ico 0.973 vs ER 0.605** @20 % | the conclusion that geometry is useless |
| **Locked control** | `sift1m_locked_control.py` | Ico beats SizeRand (degree + neighbour-size matched) at every budget | "bigger neighbours explain it" |
| **Cross-dataset** | `memcore_granularity.py` | same result on 54k text embeddings | "SIFT-specific artifact" |
| Adaptive granularity | `ico_adaptive_granularity.py` | L1–L4 sweep, both datasets | "one fixed level is right" |
| Cost–quality | `ico_cost_quality.py` | rerank dominates cost, not traversal; pre/post/ceiling NDCG | "ranking stage is the bottleneck" |
| Order information | `ico_order_information.py` | Spearman +0.15, 3.1× enrichment in the first 10 % | "traversal order is meaningless" |
| Update cost | `ico_update_cost.py` | insert 24–29 µs, rebuild = 42k–408k inserts | "rebuild is the only update path" |
| Selector v2 | `ico_adaptive_selector.py` | feasibility gates × cost, p50/p95, update model | "one decision rule fits all" |
| Lifecycle | `ico_lifecycle.py` | tombstones cost quality directly; compaction restores it | "deletes are free" |
| Growth | `ico_growth_fixed.py`, `ico_growth_after_insert.py`, `ico_growth_adversarial.py` | max-cell criterion survives growth | "the selector is build-time only" |
| Level transition | `ico_level_transition.py` | downtime 1143 ms → 3.3 ms with shadow + swap | "transitions must stop the world" |
| Incremental remap | `ico_incremental_remap*.py` | exact remap at 1.25 % of the evaluations | "re-leveling needs a full rescan" |
| End-to-end | `ico_transition_e2e.py` | 34.5× compute → **19.7× wall-clock** | "compute savings equal time savings" |
| Scaling | `ico_scaling_remap.py` | remap strictly linear in N, 300k→1.2M | "fixed cost will dominate" |
| RCU stress | `ico_rcu_stress.py` | 5 invariants zero, 3 conditions pass | "reclamation cannot be made safe" |

---

## 3. Final measurements

### 3.1 The locked control (the strongest evidence)

`ico_cost_quality.py` partitions SIFT1M into **162 icosphere-vertex cells**
(identical for every graph), same start node, **exact L2**, 5 graph seeds ×
20 queries = 100 paired samples.

| Budget | Ico (geometric) | Config (same degree seq) | ER (same edge count) | SizeRand (degree + neighbour size matched) |
|---|---|---|---|---|
| 5 % | **0.648** | 0.530 | 0.526 | 0.542 |
| 10 % | **0.804** | 0.587 | 0.575 | 0.603 |
| 20 % | **0.973** | 0.638 | 0.634 | 0.689 |

Paired difference vs Ico (n = 100), 95 % CI:

| Budget | vs Config | vs ER | vs SizeRand |
|---|---|---|---|
| 5 % | +0.118 [+0.077, +0.159] | +0.122 [+0.081, +0.163] | +0.106 [+0.066, +0.146] |
| 10 % | +0.217 [+0.164, +0.270] | +0.229 [+0.174, +0.284] | +0.201 [+0.149, +0.253] |
| 20 % | +0.335 [+0.278, +0.392] | +0.339 [+0.285, +0.393] | +0.284 [+0.233, +0.335] |

All nine CIs exclude zero; t-p from 1.9e-07 to 9.9e-22; Wilcoxon p from 7.4e-08
to 6e-15. Ico wins 36/63/77–80 of 100 queries. **Verdict: PASS** under the
pre-registered rule ("Ico must still win after degree + neighbour-size
matching").

Neighbour-size control actually achieved: mean neighbour size Ico 6,231 vs
SizeRand 6,501, median 540 vs 549.

**Decomposition:** neighbour *size* alone explains a small part (SizeRand >
Config and ER), geometry explains the rest (+0.284 at 20 %).

### 3.2 Cross-dataset replication (MemCore, text embeddings)

`memcore_granularity.py`, 54,211 × 384 e5-small vectors, cosine, 162 cells.

| Budget | Ico | SizeRand | Config | paired diff vs Ico (SizeRand) | Ico wins |
|---|---|---|---|---|---|
| 5 % | **0.429** | 0.269 | 0.259 | +0.160 [+0.124, +0.196] | 63/100 |
| 10 % | **0.664** | 0.328 | 0.306 | +0.336 [+0.291, +0.381] | 90/100 |
| 20 % | **0.837** | 0.414 | 0.386 | +0.423 [+0.377, +0.469] | 94/100 |

The effect is **larger** on text embeddings than on SIFT and grows with budget in
both cases → the direction transfers.

### 3.3 Adaptive granularity (level sweep)

`ico_adaptive_granularity.py`. Pool recall at 5 / 10 / 20 %.

**SIFT1M (1,000,000 × 128)**

| level | V | 5 % | 10 % | 20 % | diff vs SizeRand @20 % | hops @20 % | cell build |
|---|---|---|---|---|---|---|---|
| 1 | 42 | 0.333 | 0.629 | 0.903 | +0.088 | 3.6 | 0.8 s |
| 2 | 162 | 0.648 | 0.804 | 0.973 | +0.284 | 11.9 | 1.1 s |
| 3 | 642 | 0.743 | 0.914 | 0.989 | +0.476 | 45.1 | 2.7 s |
| 4 | 2562 | 0.776 | 0.948 | 0.994 | +0.613 | 180.2 | 9.1 s |

**MemCore (54,211 × 384)**

| level | V | 5 % | 10 % | 20 % | diff @20 % | hops @20 % | build |
|---|---|---|---|---|---|---|---|
| 1 | 42 | 0.410 | 0.591 | 0.811 | +0.211 | 6.4 | 0.1 s |
| 2 | 162 | 0.432 | 0.659 | 0.836 | +0.413 | 22.6 | 0.1 s |
| 3 | 642 | 0.489 | 0.672 | 0.845 | +0.523 | 88.5 | 0.2 s |
| 4 | 2562 | 0.511 | 0.697 | 0.846 | +0.508 | 357.3 | 0.5 s |

Fine granularity makes the geometric advantage **larger**, not smaller: at level
4 / 5 % budget the random control collapses to 0.183 while Ico reaches 0.776.

### 3.4 Cost–quality

`ico_cost_quality.py`. Cost is dominated by **rerank**, not traversal.

| | SIFT1M | MemCore |
|---|---|---|
| traversal p50 | 0.00–0.50 ms | 0.02–0.78 ms |
| traversal p95 | 0.06–0.97 ms | 0.05–1.31 ms |
| rerank p50 | 46–204 ms (50k–200k candidates) | 2.5–10.6 ms |
| index build L1–L4 | 0.92 / 1.74 / 3.35 / 10.52 s | 0.12 / 0.13 / 0.31 / 0.59 s |
| graph build | ≤ 0.012 s | ≤ 0.012 s |

Because the candidate count is fixed by the budget, a finer level buys quality at
essentially the same query cost:

> SIFT target NDCG 0.80 → **L3 @ 5 % = 48 ms/query, 3.35 s build**
> vs L2 @ 10 % = 98 ms/query, 1.74 s build.

Marginal value collapses after L2 (SIFT @5 %): L1→L2 +0.284 NDCG for 0.82 s
(0.346/s); L2→L3 +0.075 for 1.61 s (0.047/s); L3→L4 +0.025 for 7.18 s (0.003/s).

### 3.5 Rerank quality, measured correctly

Pool recall and NDCG are different metrics. Measuring on the **same pool**:

- **SIFT1M**: loss (pool ceiling − achieved NDCG) ≈ **0.000** at every level and budget.
- **MemCore**: raw loss 0.11–0.19, but with duplicate tolerance (any pool item
  within 1e-4 cosine of a GT item counted as a hit) the loss drops to **0.035**.

MemCore diagnostic (`memcore_ranking_diagnostic.py`, L2, 20 %, 100 queries):
mean cosine gap between the 10th GT item and the best non-GT pool item is
**+0.00162**; 56/100 queries have that gap ≤ 1e-3; 10/100 GT top-10 sets contain
exact duplicate texts. **Conclusion: the cosine reranker is effectively at the
pool ceiling on both datasets; the apparent gap is near-duplicate noise.**

### 3.6 Traversal order is weakly informative

`ico_order_information.py` (L2, 20 %, 30 queries):

| metric | SIFT1M | MemCore | chance |
|---|---|---|---|
| Spearman(position, similarity rank) | +0.151 | +0.144 | 0 |
| preNDCG@10 | 0.0000 | 0.0105 | — |
| preNDCG@100 | 0.0022 | 0.0159 | — |
| pool top-10 within first pool/10 positions | **0.310** | **0.313** | 0.100 |

So preNDCG ≈ 0 means the order is uninformative **at k = 10**, not that it
carries no ordering information: early cells are 3.1× enriched. The collector
supplies recall; the reranker supplies ranking.

### 3.7 Update cost (four separate terms — never merged)

`ico_update_cost.py`:

| term | SIFT1M | MemCore |
|---|---|---|
| topology build (mesh) | ≤ 0.012 s, independent of N | same |
| index build (assign N rows) | 1.03 / 2.05 / 3.99 / 11.68 s (L1–L4) | 0.14 / 0.14 / 0.19 / 0.56 s |
| insert (project + argmax + append) | 24.1–28.6 µs + 1.5–1.7 µs | 23.6–28.9 µs + 1.5–1.7 µs |
| bulk insert | 1.2 / 2.8 / 2.6 / 7.6 µs per vector | 2.0–12.1 µs |
| rebuild = how many single inserts | 42k–408k | 5.8k–19.4k |

The icosphere **topology is fixed**, so an insert never re-derives the graph and
never re-clusters — only the touched cell's buffer grows.

### 3.8 Lifecycle

`ico_lifecycle.py` (N = 100k, L2, budget 5 % / 10 %):

| dead | NDCG@5 % | NDCG@10 % | dead in 5k candidates | compaction | after compaction |
|---|---|---|---|---|---|
| 0 % | 0.691 | 0.852 | 0 | — | — |
| 10 % | 0.646 | 0.790 | 500 | 0.18 s | **0.694 / 0.846** |
| 30 % | 0.526 | 0.643 | 1,495 | 0.14 s | **0.709 / 0.839** |
| 50 % | 0.442 | 0.547 | 2,504 | 0.12 s | **0.695 / 0.839** |

Tombstones waste the candidate budget in direct proportion; compaction restores
full quality. **Rule: compact before the dead fraction exceeds ~10–20 %.**

Insert:query ratio (N = 100k):

| budget | query p50 | +1 insert | +10 | +100 | +1000 |
|---|---|---|---|---|---|
| 5 % | 5.3 ms | 5.3 | 5.8 | 10.1 | **53.9** |
| 10 % | 10.0 | 10.1 | 10.5 | 14.9 | **58.6** |
| 20 % | 18.5 | 18.5 | 19.0 | 23.3 | **67.1** |

≤ 10 inserts/query costs < 10 %; 1,000 inserts/query dominates the query.
Write-heavy workloads must batch (bulk is ~20× cheaper than single insert).

### 3.9 Granularity criterion (the selector's gate)

Measured across N = 10k / 30k / 100k / 300k / 1M, four levels, both datasets:

> **`max_cell_size / N ≤ scan_budget` is SUFFICIENT for zero truncation.**

Every configuration satisfying it showed truncation 0.00. Violating it makes
truncation possible, and severity tracks the *share of queries that start in an
oversized cell*, not the max ratio itself (maxCell/N 17.1 % with a 10 % budget
gave truncation only 0.07 because 1 of 162 cells was oversized).

Empirical mapping: **L1 honours 20 %, L2 honours 10 %, L3 honours 5 %** at every
N tested.

**Rejected rule (do not reuse):** "mean cell size ≈ x % of N". It contradicts the
measured sweet spots (SIFT L3 mean = 0.156 % of N; MemCore L2 = 0.618 %;
MemCore L3 = 0.155 %).

**Stability under growth** from N = 100k to 500k (`ico_growth_after_insert.py`):

| insert stream | maxCell/N at 100k / 200k / 300k / 500k |
|---|---|
| uniform | 6.85 / 6.92 / 6.90 / 6.85 % |
| contiguous (clustered) | 7.59 / 6.92 / 6.88 / 7.34 % |
| adversarial (all into the 5 largest cells) | 5.60 / 13.02 / 17.14 / 18.80 % |

The criterion still predicted correctly in the adversarial case; only the margin
eroded (3.4×). Compaction moves the ratio by < 0.35 pp — it restores dead space,
not balance. Monitoring cost: **O(1) counter 0.33 µs, max-heap O(log V) 0.76–0.96 µs,
np.max O(V) 1.21–2.10 µs.** The heap is **not** O(1); the counter is.

### 3.10 Level transition

`ico_level_transition.py` (N = 300k, L2 → L3, trigger: max cell 7.03 % > 5 %):

| strategy | downtime | shadow build | queries served during |
|---|---|---|---|
| stop-the-world | **1,143 ms** | — | 0 |
| chunked + atomic swap | **3.30 ms** | 798 ms | 15/15 |
| | | | | 

Query latency during the transition p50 15.0 ms (baseline 14.5), p95 18.0 ms.
After swap: L3 max cell 2.48 % → criterion satisfied. Rows that changed cell:
**75.4 %** (226,236 / 300,000). Structural memory peak **2.3 MiB** when cells hold
row ids; 73 MiB / 879 MiB only if cells duplicate the vector payload (uint8 128-d
/ f32 384-d).

`ico_transition_e2e.py` — end-to-end with precompute, remap, buffer build, swap,
reclaim and interleaved queries:

| stage | full argmax | incremental | ratio |
|---|---|---|---|
| precompute (per-cell order) | 0 ms | 10 ms | (new cost) |
| remap (15 chunks, queries interleaved) | 10,292 ms | 479 ms | 21.5× |
| cell buffer build | 23 ms | 24 ms | 1.0× |
| atomic swap | 3 ms | 3 ms | 1.0× |
| reclaim old | 6 ms | 7 ms | 0.9× |
| **TOTAL** | **10,325 ms** | **524 ms** | **19.7×** |

Mapping identical (0 mismatches); post-transition recall@10 0.7750, NDCG@10
0.8457. **34.5× compute → 19.7× wall-clock**; the fixed ~33 ms tail and the
~225 ms of query interleaving are what dilute it (interleaving is 47 % of the
incremental remap and only 2 % of the full one).

### 3.11 Exact incremental remapping

`ico_incremental_remap_prefix.py`. Exactness rests on one bound: a row `p` in
cell `v` can only be nearest to a level-3 vertex `u` with
`||v−u|| ≤ ||v−p|| + ||p−u|| ≤ 2·||v−p||`.

| method | distance evaluations | % of full | exact |
|---|---|---|---|
| full argmax | 192,600,000 | 100 % | baseline |
| B: cell-level superset (`2·θ_v`) | 4,813,723 | 2.50 % | ✅ 0 mismatch |
| C: `v` + midpoints of edges incident to `v` | 1,599,664 | 0.83 % | ❌ **15,816 wrong** |
| **D: per-row radius + sorted prefix** | **2,406,671** | **1.25 %** | ✅ **0 mismatch** |

D: 34.5× faster including a 12 ms one-off precompute, mean 8.02 candidates per
row, 19.2 % of rows skipped with no evaluation at all.

**Method C is wrong and must not be used**: an L2 cell is the Voronoi region of
`v` and is much larger than the union of its 6 incident triangles, so rows can be
stolen by midpoints of non-incident edges. This is exactly the "fast but wrong"
case the acceptance rule forbids.

Two implementation requirements for exactness:
1. apply the bound with the **row's own** distance, not the cell maximum;
2. **sort each candidate prefix by index** before argmin so tie-breaking matches
   full argmax (first minimum) — SIFT contains duplicate descriptors.

### 3.12 Scaling of the remap

`ico_scaling_remap.py`, precompute cached (10.9 ms one-off, **1.59 MiB** for
162×642, reusable at every N):

| N | remap | total | structural peak | q p95 during | q p95 baseline |
|---|---|---|---|---|---|
| 300,000 | 0.53 s | 0.57 s | 3.4 MiB | 15.9 ms | 29.3 ms |
| 600,000 | 1.05 s | 1.11 s | 6.9 MiB | 32.6 ms | 31.9 ms |
| 1,200,000 | 2.10 s | 2.23 s | 13.7 MiB | 64.7 ms | 92.6 ms |

Remap is **strictly linear** (1.00× / 1.97× / 3.95× for N ratios 1× / 2× / 4×).
The fixed-cost hypothesis is **not** confirmed at these sizes: remap stays
93–95 % of the total, and the tail is 5–7 % of it, because the cell buffer build
is itself O(N) (23/50/112 ms) while only swap (3–6 ms) and reclaim (6–9 ms) are
genuinely O(1) — a fixed ~10 ms against a 2.2 s transition. Structural peak memory
is exactly **12N bytes** (old ids 4N + new assignment 4N + new cell arrays 4N).
Process-RSS deltas were too noisy to quote (−6 to −24 MB, allocator reuse).

### 3.13 RCU / epoch reclamation

`ico_rcu_stress.py` — 2-thread stress (1 writer publishing 10 alternating L2/L3
versions rebuilt by the exact remap at N = 50k; 1 reader serving queries; 89
reads; 2 deferred reclaims; max 2 versions live; 7 deliberately slow reads with
120 ms holds).

| invariant | violations |
|---|---|
| I1 snapshot mutated while live | **0** |
| I2 acquired object ≠ released object | **0** |
| I3 use-after-reclaim | **0** |
| I4 reclaimed while held (object identity) | **0** |
| I5 reported a generation older than a preceding publish | **0** |

Conditions: (1) every generation 1–10 received reads — PASS; (2) the newest
generation was read — PASS; (3) reclamation was deferred while a reader held an
older version (max live 2) — PASS.

**Two harness lessons, both required in production:**
1. the epoch bump and the publication must be **one atomic step under the same
   lock** — doing them separately created a window where the epoch did not
   identify the published version, producing 7 false violations;
2. the reclamation safety check must be **independent of the epoch arithmetic
   used to make the decision** — here the decision uses epochs and the check uses
   object identity.

---

## 4. Corrections and retractions (honesty log)

Every one of these was found by re-checking a suspicious number rather than
accepting it.

| # | claim made | correction | how it was caught |
|---|---|---|---|
| 1 | "geometry does not matter for retrieval" | **wrong** — artifact of the 20-cell partition | granularity control (`sift1m_granularity.py`) |
| 2 | Hasse/Pyrochlore/XYZ add retrieval value | **not supported** (corr −0.115; random ≈ Hasse) | ablations |
| 3 | "mean cell size ≈ 0.5–1.5 % of N" as a level rule | **retracted** — contradicts measured sweet spots | the owner's cross-check |
| 4 | "ranking stage is the bottleneck on text" | **retracted** — loss is near-duplicate tie noise (0.185 → 0.035 tolerant) | `memcore_ranking_diagnostic.py` |
| 5 | level "does not affect query cost" | **narrowed** — it holds at equal budget in this set only; level acts indirectly via candidate distribution and hops | the owner's correction |
| 6 | `preNDCG ≈ 0` ⇒ traversal order uninformative | **narrowed** — uninformative at k = 10 only; Spearman +0.15, 3.1× enrichment | `ico_order_information.py` |
| 7 | "swap is O(1), heap is fine" | **corrected** — the counter is O(1), the heap is O(log V), 2.3–2.7× the counter | the owner's correction |
| 8 | first RCU run FAIL (I3 = 7, I4 = 7) | **my harness was wrong**, not the protocol | re-derivation + rewrite |
| 9 | "midpoint-only affected region is enough" | **wrong** — 15,816 wrong rows | exactness assertion in the test itself |

**Harness bugs found and fixed** (each would have produced a plausible but wrong
number):

- `.fvecs` bodies are **float32**, not int32; reading them as int32 gives
  all-zero vectors and silently collapses every row into one cell
  (`ico_gt_check.py`, `ico_tie_check.py`).
- SIFT ground truth agreement vs the official `.ivecs`: **20/20 exact match**.
- When the query is itself a corpus member it sits at distance 0 and takes rank
  1; excluding it from GT while keeping it in the pool caps NDCG@10 at exactly
  **0.780** for every query (`ico_growth_fixed.py`).
- A budget cannot be honoured when the start cell alone exceeds it: reported as
  `trunc`, never silently truncated (`trunc` column everywhere).

---

## 5. What is proven, and what is not

### Proven (with receipts)

1. **Geometric topology improves candidate retrieval** at equal scan budget,
   after controlling degree and neighbour-size distribution — replicated on
   SIFT1M (L2) and MemCore (text embeddings), CIs excluding zero, Ico winning
   63–94 of 100 queries.
2. **Granularity is the binding constraint** at 1M scale: a 20-cell partition
   cannot honour a 5–10 % budget.
3. **The level criterion is `max_cell/N ≤ budget`** (sufficient), stable under
   insert and compaction, monitorable in O(1) + O(log V) per insert.
4. **Level transition is zero-downtime in prototype**: 1,143 ms → 3.3 ms.
5. **Exact incremental remapping** at 1.25 % of the evaluations, identical
   mapping and identical quality.
6. **Rerank is at the pool ceiling** on both datasets.
7. **End-to-end transition scales linearly** to 1.2M rows, 19.7× faster than a
   full remap, with no measurable query degradation.
8. **RCU/epoch reclamation logic is correct** in a 2-thread prototype: 5
   invariants at zero, 3 conditions satisfied.

### Not proven / out of scope

- **Production atomicity.** Python reference assignment is atomic under the GIL;
  there are no fences, no reordering. The swap must be re-proven in C/Rust with
  real atomics and an epoch/grace-period mechanism.
- **More than one concurrent reader.** This box has **2 logical CPUs**; > 2
  readers requires a bigger machine (colab).
- **Preemption and GC pauses** during a reader's critical section.
- **LC addressing, memory layout, storage.** Nothing here speaks to them; the
  evidence is about *semantic candidate retrieval* only.
- **Latency as production numbers.** All timings are Python prototypes with exact
  brute-force rerank (no SIMD/GPU). Use them for structural comparisons, not for
  SLO promises.
- **Real workloads.** Growth, deletes and insert:query ratios are synthetic.

---

## 6. Architecture: adaptive granularity lifecycle

```
Monitor  →  Trigger  →  Shadow Build  →  Exact Incremental Remap  →  Atomic Swap  →  Reclaim
```

| stage | mechanism | measured |
|---|---|---|
| Monitor | cell counter (O(1)) + max-heap (O(log V)) | 0.33 µs / 0.76–0.96 µs |
| Trigger | `max_cell/N > scan_budget` | L1→20 %, L2→10 %, L3→5 % |
| Shadow Build | build the new assignment while the old index serves | 798 ms @300k, peak 12N bytes |
| Remap | exact bound, per-row radius, sorted prefix | 1.25 % of full argmax, 0 mismatches |
| Atomic Swap | publish under one lock together with the epoch bump | 3.3 ms downtime |
| Reclaim | epoch-based, identity-verified | 5 invariants at zero |

Cost terms kept **separate** (never merged into one "build cost"):
`topology_build` · `index_build` · `insert` · `compaction` · `query`.

Selector decision order:

1. **Feasibility** — NDCG target, truncation limit, hop limit, p95 SLO (hard gates).
2. **Cost** — build + query p50/p95 against the query count.
3. **Update model** — insert / append / rebuild / compaction.

Example (SIFT, p95 SLO 250 ms): every 20 % budget is rejected (p95 272–333 ms);
high quality → **L3 @ 10 %** (NDCG 0.943, p95 149 ms); targets up to 0.80 →
**L3 @ 5 %** (0.820, p95 73 ms). MemCore cannot reach 0.80 at any level
(ceiling ≈ 0.706) → reported infeasible rather than "least bad".

---

## 7. Remaining work (prioritised)

1. **Multi-reader RCU on a bigger machine (colab)** — 4/8/16 readers, contention,
   grace-period latency, starvation of the writer.
2. **Production atomic swap in C/Rust** — real atomics + fences + epoch
   reclamation; prove no dangling reference under load.
3. **Preemption/GC-pause cases** — a reader paused mid-critical-section for
   seconds must still not block reclamation forever (grace-period bound).
4. **Level criterion under adversarial continued insert** — the margin eroded
   3.4× in the synthetic hot-cell stream; needs a real tenant-per-cell workload.
5. **Precompute cache as product asset** — the 1.59 MiB per (level-from,
   level-to) pair table should be built once and shipped with the index.
6. **Compaction policy** — trigger on dead fraction (10–20 %) vs on quality drop;
   measure both.
7. **Rerank cost** — it is 95 %+ of query time; SIMD/GPU or a cheaper metric is
   where the next order of magnitude lives, not in the collector.
8. **MemCore quality ceiling** — deduplicate before embedding; the corpus has
   exact duplicate texts inside GT top-10 sets.
9. **Write-path optimisation (LC)** — the O(N²) fruit_store save noted earlier;
   not addressed here.

---

## 8. Reproduction

```powershell
cd I:\tools\ann\experiments\LC_tilecube

# dataset (external, ~168 MB download -> 541 MB unpacked, gitignored)
python -c "import urllib.request;urllib.request.urlretrieve('ftp://ftp.irisa.fr/local/texmex/corpus/sift.tar.gz','sift.tar.gz')"
python -c "import tarfile;tarfile.open('sift.tar.gz').extractall('.')"

# the evidence chain, in order
python ico_gt_check.py                  # GT agrees with the official ground truth (20/20)
python sift1m_granularity.py            # granularity is the binding constraint
python sift1m_locked_control.py         # degree + neighbour-size matched control  <- key result
python memcore_granularity.py           # cross-dataset replication
python ico_adaptive_granularity.py      # L1-L4 sweep
python ico_cost_quality.py              # pre/post/ceiling NDCG + latency
python ico_lifecycle.py a               # growth (add b / c for compaction / insert ratio)
python ico_incremental_remap_prefix.py  # exact remap at 1.25 % of evaluations
python ico_transition_e2e.py            # end-to-end transition
python ico_scaling_remap.py             # scaling to 1.2M
python ico_rcu_stress.py                # RCU/epoch invariants
```

MemCore experiments read `I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3`
(read-only).

## 9. Evidence files

`.log` files next to each script are the raw console output of the runs quoted
above; they are the receipts and are committed alongside the scripts.

| log | produced by |
|---|---|
| `sift1m_gran.log`, `sift1m_locked.log` | granularity control, locked control |
| `memcore_gran.log` | text-embedding replication |
| `ico_cost_quality.log`, `ico_adaptive*.log` | cost–quality, level sweep |
| `ico_lifecycle*.log`, `ico_growth_*.log` | lifecycle, growth |
| `ico_update.log` | update cost |

---

*Scope reminder: semantic candidate retrieval only. Nothing in this report
validates LC addressing, memory layout, or storage, and no number here is a
production latency promise.*
