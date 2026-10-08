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
# warm ON, topk=1, related queries
ANN_TOMBSTONE_WARM=1 ANN_ROUTE_LOG=data/memcore/lz_route.log \
  ./build/ann_search.exe --serve 8096
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"geofs mdim","topk":1}' http://127.0.0.1:8096/v1/state/search
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"geofs mdim multivolume","topk":1}' http://127.0.0.1:8096/v1/state/search
# second call reports route.mode="warm" and fewer items_scored
```

Verified ad-hoc 8/8 (scratch port, fresh route log): build, novel anchor mode,
two overlapping queries reduce, warm top-k == OFF top-k, OFF repeat identical.

## Relationship to the Confident Guideline

Two separate axes, both env-gated and composable:

- **structural** (`ANN_GRAFT`): which code a query is about — static, baked.
- **hot/cold** (`ANN_TOMBSTONE_WARM`): which routes were just walked — per query.

The tombstone is the amplifier: a route it keeps warm can later carry a stronger
cluster weight. Keep the two switches independent.
