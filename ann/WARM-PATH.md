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

**Gate (fixed 2026-10-14).** The warm reorder only runs when it actually skips
work: `cov < budget`. `cov` is the number of entries the SHUT buckets cover, and
a single bucket covers 24–30% of the index (B1), so `budget = cov` could *raise*
the cap above `LZ_SEARCH_BUDGET` — a repeat of `geofs mdim` scored **47027 vs the
24000 cold cap** on the 59756-row index. The warm path must do **less** work,
never more. The bug was latent: D5 made 24000 a hard cutoff on 2026-10-11, while
this doc was written 2026-10-08 when the budget still auto-raised to N (so
`cov < nent` was always true then). When `cov >= budget` there is nothing to
skip, so the cold walk is left untouched.

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
