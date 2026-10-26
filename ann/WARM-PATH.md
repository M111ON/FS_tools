# Tombstone warm path — what it does, and the honest numbers (2026-10-08)

## What was built

`ann_search.c`, inside `lz_mem_search`, gated by `ANN_TOMBSTONE_WARM` (**default 0**):

1. **Read the latch BEFORE walking.** Each routed bucket is checked with
   `wl_is_open(&g_fog, lz_fog_id(bucket))`. A bucket walked by an earlier query
   is SHUT; a never-walked one is OPEN.
2. **Score the SHUT buckets first, then stop.** The walk order is reordered so
   entries covered by SHUT buckets come first, `budget` is set to their count,
   and the scan stops after them (`route.mode = "warm"`).
3. The walk then shuts the latch as before (monotone) and appends to the P3 log.

**Gate (fixed 2026-10-14, bounded 2026-10-26).** The warm reorder only runs when
it actually skips work: `cov < budget`. `cov` is the number of entries the SHUT
buckets cover, and at K=8 a single bucket covered 24–30% of the index (B1), so
`budget = cov` could *raise* the cap above `LZ_SEARCH_BUDGET` — a repeat of
`geofs mdim` scored **47027 vs the 24000 cold cap** on the 59756-row index. The
warm path must do **less** work, never more. The bug was latent: D5 made 24000 a
hard cutoff on 2026-10-11, while this doc was written 2026-10-08 when the budget
still auto-raised to N (so `cov < nent` was always true then). When
`cov >= budget` there is nothing to skip, so the cold walk is left untouched.

`cov < budget` alone is **not enough** at K=64 — it stops the warm path doing
*more* work but permits doing far *less*, which is a recall loss. `ANN_WARM_KEEP`
(default 90) now bounds the cut: the warm path may never score less than 90% of
what the cold walk would score. See the 2026-10-26 section below — under that
bound it fired 0/200 at the live configuration and can only fire when the
still-open remainder of the route is ≤10%.

This is the **hot/cold axis** only. It never prunes a route that was not walked
before — a novel query has no SHUT bucket and keeps its full behaviour.

## Measured (57,076-entry index, dim 384, `topk=1`)

The reduction appears when a query's route **overlaps** previously-walked
buckets. Three related queries, warm ON vs OFF:

| query | ON `items_scored` | OFF `items_scored` | top-1 | mode |
|---|---|---|---|---|
| `geofs mdim` (novel) | 54750 | 54750 | `018b560d…` | anchor |
| `geofs mdim multivolume` (overlap) | **45852** | 48551 | `7c0cd648…` (same) | **warm** |
| `geofs multivolume geometry` (overlap) | **48322** | 48551 | `d58aa10f…` (same) | **warm** |

- **Gate met**: an overlapping query scores fewer items, and the **top-k is
  identical** to the OFF baseline (recall preserved).
- Novel query unchanged (54750 = OFF).

> ⚠️ **These 2026-10-08 numbers predate the D5 budget change** and the row
> `budget = cov` that produced them is the bug fixed 2026-10-14. At the current
> default (`LZ_SEARCH_BUDGET=24000`) the warm path is **inert** on this index:
> 5 buckets already cover >24000 entries, so `cov >= budget` and the cold walk
> is kept. Re-measured below with an explicit budget.

### Re-measured after the fix (2026-10-14, 59,756-entry index, 60 queries)

`bench_warm_recall.py` — raw index rows as queries, oracle = numpy brute top-K
over all N. `--budget 60000` (above N, so the warm set is a genuine subset):

| pass | mode | items_scored (mean) | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| OFF novel | anchor+p | 46265.6 | 1.0000 | 1.0000 | 1.0000 |
| ON novel (cold) | anchor+p / warm+p | 46002.7 | 1.0000 | 1.0000 | 1.0000 |
| ON repeat | warm+p | 46264.8 | 1.0000 | 1.0000 | 1.0000 |
| OFF overlap | anchor+p | 47812.0 | 0.9833 | 1.0000 | 0.9983 |
| ON overlap (warm) | warm+p | **47603.8** | 0.9833 | 1.0000 | 0.9983 |

- **C1 (novel recall unchanged)**: warm ON == OFF on recall@1/5/10 and top-k; the
  warm pass scores *fewer* items (46003 vs 46266), never more.
- **C2 (warm scores fewer)**: the overlapping query scores 47604 vs 47812 OFF,
  with recall **identical** and top-k == OFF.

At the production default (`LZ_SEARCH_BUDGET=24000`) both the OFF and ON runs
score exactly 24000 and every recall figure is identical — the warm path is a
correct no-op there.

### 2026-10-26 — the cut IS the recall loss at K=64 (bound added)

Re-measured at the production budget on the live 63,804-row index (K=64,
`LZ_ROUTE_BUCKETS=5`), scratch servers, numpy oracle, 60 queries:

| pass | items_scored | mode | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| OFF novel | 11,218.8 | anchor+p 60/60 | .9833 | .9333 | .9250 |
| ON novel | 10,847.1 | anchor+p 52, **warm+p 8** | **.9667** | **.9100** | **.9050** |
| ON exact repeat | 11,218.8 | anchor+p 60/60 | .9833 | .9333 | .9250 |
| ON overlap σ.05 | 11,107.0 | anchor+p 60/60 | .9000 | .8933 | .8833 |

C1 (novel recall unchanged) **FAILED**. Per-query kept fraction
(`warm_scored / cold_scored`) explains it:

| kept fraction | n | R@5 cold | R@5 warm | top-k identical |
|---|---|---|---|---|
| 0.00-0.20 | 1 | 1.000 | **0.000** | 0/1 |
| 0.60-0.80 | 3 | 0.867 | 0.733 | 1/3 |
| 0.80-0.90 | 4 | 0.900 | 0.900 | 3/4 |
| 0.90-0.95 | 2 | 1.000 | 1.000 | 2/2 |

Why it changed: `cov < budget` was calibrated when a bucket was 24-30% of the
index (K=8). The 2026-10-14 receipt above kept **99.6%** of the route, so the
cut was 0.4%. At K=64 a bucket is ~3%, so the same guard passes on cuts of
20-80% of the route, and `budget = cov` then scores ONLY the shut subset
(`src/ann_search.c:2051-2070`).

**Fix — `ANN_WARM_KEEP` (default 90):** the warm path may never score less than
90% of what the cold walk would score. Measured after the fix (200 queries):
C1 identical, C2 overlap identical, warm fired **0/200**.

That is arithmetic, not a regression: with `topb = 5` a *partial* shut set
covers at most 4/5 of the route when the buckets are even, so the bound is
reachable only when the one still-open bucket is tiny (live skew has buckets
from 104 to 5,327 rows); the all-shut case is refused (`ann_search.c:2030`)
because it has nothing to skip. Measured at the live configuration: **0 fires
in 200 queries**. Under a recall-safe bound the warm path can only fire when
the part it leaves unscored is ≤10% of the route — a bounded no-op, not a speed
lever.

**Retracted claim:** "the warm path saves work on related queries". It saved
work only while the saving was 0.4% (invisible) and lost recall the moment the
saving was real. What survives is the tombstone × graft amplifier, which
re-ranks inside the SAME scored set and never touches the cut.


## P3 route-log compaction (2026-10-14)

The tombstone's truth is the P3 log, and the latch is MONOTONE within an epoch,
so the log is append-only. Before the transition-only fix (2026-10-11) it
recorded the SAME slot on every walk: the live log held **5,661 records for 7
distinct slots (99% redundant)** and every boot replayed all of it. The fix
stopped new growth but left the historical bulk.

`scripts/p3_compact.py` compacts it **losslessly by construction** — replay is
last-event-wins per `(name_hash, slot)`, so superseded records carry no
information; `ANCHOR` (no state) and `CLEAR`-on-a-virgin-slot (a no-op) are
dropped, and everything kept is copied byte-exact so CRCs stay valid.

Measured on the live log, then deployed:

| | before | after |
|---|---|---|
| records | 5661 | **7** |
| bytes | 181,155 | **227** |
| shut ids | 72,144,216,288,360,432,504 | identical |
| live `fog.replayed` | 5661 | **7** |

`tests/p3_compact_verify.c` (the real `wl_replay_log_file` the server uses)
reports `LATCH IDENTICAL: YES`; `tests/p3_append_test.c` proves the compacted
file still accepts appends (7 → 8 records, 0 old ids lost). Safety: dry-run by
default, refuses on any CRC failure or state change, writes `.bak-<ts>` first.

`tests/test_p3_compact.py` is wired into `make test` (4th group), so the
canonical command covers this change: edge cases, refusals, shrink/idempotence,
and the C-replay round-trip.

```sh
python scripts/p3_compact.py --log data/memcore/lz_route.log            # dry-run
python scripts/p3_compact.py --log data/memcore/lz_route.log --apply    # compact
```
Stop the server first (it holds the log open for append).

## Honest limits (measured, do not overclaim)

1. **An EXACT repeat does not reduce.** A repeat routes to the same buckets the
   previous run just shut, so the SHUT set equals the walked set — the warm path
   has nothing to skip. The win is on **related** queries that re-enter
   already-walked buckets, not on a literal re-run. (The handoff's "repeat query
   scores fewer" assumed otherwise; measurement disagrees.)
2. **Inert at `topk ≥ 3` on this index.** There `topb = topk + 4 ≥ 7` buckets
   cover all 57k entries (B1 hard-bucket wall), so "SHUT buckets" = the whole
   index and there is no remainder to skip. The reduction only exists when the
   route is a genuine subset — measured at `topk=1` (5 buckets → 54750/57076)
   and `topk=2` (6 buckets → 56570/57076).
3. **Savings are modest here** (−5.6% and −0.5% of the scanned set) because a
   single warm bucket already covers ~24–30% of the index (the same coverage
   rule that causes B1). The lever helps most when the warm set is small
   relative to the index.
4. **Default OFF.** The bias is opt-in; the shipped behaviour is unchanged.

## Reproduce

```sh
# the full A/B measurement (novel vs repeat vs overlap, numpy oracle)
python bench_warm_recall.py --queries 60 --ks 1,5,10 --sigmas 0.05 --budget 60000

# or by hand: warm ON, topk=1, related queries
ANN_TOMBSTONE_WARM=1 ANN_ROUTE_LOG=data/memcore/lz_route.log \
  ./build/ann_search.exe --serve 8096
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"geofs mdim","topk":1}' http://127.0.0.1:8096/v1/state/search
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"geofs mdim multivolume","topk":1}' http://127.0.0.1:8096/v1/state/search
# second call reports route.mode="warm" and fewer items_scored (budget permitting)
```

Verified ad-hoc 8/8 (scratch port, fresh route log): build, novel anchor mode,
two overlapping queries reduce, warm top-k == OFF top-k, OFF repeat identical.

## Relationship to the Confident Guideline — the amplifier (wired 2026-10-08)

Two axes, both env-gated and composable:

- **structural** (`ANN_GRAFT`): which code a query is about — static, baked.
- **hot/cold** (`ANN_TOMBSTONE_WARM`): which routes were just walked — per query.

The tombstone is the amplifier, and the two are now **composed in code**: with
`ANN_AMP_BOOST=1`, an entry sitting in a bucket the tombstone kept warm has its
graft bias multiplied by `ANN_AMP_K` (default 2.0). The multiplier applies to the
bias only (never the cosine score), so it is a strict no-op unless *both* axes are
on. Response field `route.amp = {on, k, applied}` reports it.

## Tuning the warm path so it actually pays (2026-10-08)

The warm path is inert at `topk ≥ 3` because the default route walks
`topb = topk + 4` buckets — enough to cover the whole index (B1). Pin the bucket
count with the existing `LZ_ROUTE_BUCKETS` knob; at `LZ_ROUTE_BUCKETS=5` the route
is a genuine subset and the warm path (and the amplifier) are live at `topk=5`:

| config | mode | items_scored | amp.applied |
|---|---|---|---|
| `topk=5`, default topb=9→8 | warm (inert) | 56570 / 57076 | — |
| `topk=5`, `LZ_ROUTE_BUCKETS=5` | warm | 48210 / 57076 | **87** |

Measured amplifier effect (`ANN_GRAFT=1 ANN_TOMBSTONE_WARM=1 LZ_ROUTE_BUCKETS=5`,
fresh route log, warm pass) — identical scored set, re-ranked toward the warm
cluster:

| query | OFF top-5 | ON top-5 |
|---|---|---|
| `core/geo_wang_latch.h tombstone warm route` | ebf0bbc0bb, 97296c1abc, 131f34e983, 800a03cad6, 579f5156b0 | ebf0bbc0bb, 97296c1abc, 131f34e983, **cf764fd707**, **404ad7b548** |
| `core/geo_id_route_log.h p3 log crc` | 4c44d797bb, f162046894, 664262fa40, 00348f39d5, 7c0cd6480d | 4c44d797bb, f162046894, 664262fa40, **6f6b78a06f**, **fbc67f9a2a** |

Warm entries climb into the top-5 on **3/5** path-bearing queries; the warm
entries gain exactly `+λ` (0.05) — the multiplier on the structural weight, not on
the cosine score. Receipts: `I:\tmp\amp_test\amp_final.py` (A/B),
`I:\tmp\amp_test\amp_sanity.py` (raw `amp` field, OFF applied=0 / ON applied=87).

## Honest limits (amplifier)

- **Graft keys gate it.** The boost only fires for entries that carry file keys
  (`data/graft/keys.tsv`) and queries that name a repo path (`core/x.h`). A query
  with no path token has `g_qn=0`, so the bias is 0 and the amplifier is inert.
- **Two small flips, not a recall change.** It re-ranks within the scored set; it
  is a nudge, not a filter. Novel-query recall is untouched by construction.
- **Both switches required.** `ANN_AMP_BOOST` without `ANN_GRAFT=1` or without a
  live warm route does nothing (reported as `amp.applied=0`).
