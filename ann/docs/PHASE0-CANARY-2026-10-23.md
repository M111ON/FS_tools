# Phase 0 — canary receipts (2026-10-23)

Purpose: turn the ANN upgrade plan's Option-A/Option-B choice and the
warm-path/phase-2 claims into *measurements* before code is restructured.
Everything below came from the instrumented binary, not from reasoning.

Instrumentation: `src/ann_search.c`, `ANN_CANARY=1` (+ `ANN_CANARY_RECOMPUTE=1`
for the one-shot boot sweep). One JSON line per query in
`<index>.canary.log`; aggregates at `GET /v1/canary`. Off by default.

Run: scratch instance on :8099 against the live index, route log redirected
to a temp copy so the live writer's log was never touched.

## Boot

| fact | value |
|---|---|
| entries | 62,851 |
| index parse (`index_ms`) | 37,508 – 38,084 ms |
| note replay (`fog_ms`) | 0.3 ms |
| note size | 227 B = 7 records |
| sidecar `.stamp` | 502,808 B |
| order `.order` | 251,424 B |

## Option-B gate — re-assign every row at boot

`ANN_CANARY_RECOMPUTE=1` runs the same `anch_route()` sweep the regrow uses,
over the whole index, once, before the loader thread starts.

```
option-b reassign: 62851 rows (62851 hits) in 316.4 ms (5.03 us/row) K=8 dim=384
```

**Gate: PASS.** Option B deletes 754 KB of derived state (sidecar + order) and
pays **+316 ms** on top of a **37.5 s** index parse = **0.84 %** boot overhead.
The existing boot cost is the index parse, not the derived files. The deleted
files were also the only place a label could disagree with the index, which is
what the H1/H2 hole is about.

Caveat recorded honestly: the 316 ms is arithmetic that still needs the
vectors, i.e. Option B removes *stored derived state*, not the parse. At 10×
rows it is ~3 s, still under the parse.

## Warm path — measured dead

35 queries, `ANN_TOMBSTONE_WARM=1`:

```
warm_taken  = 0
align       = no_fit 30, no_route 5
buckets     = visited 210, shut 180, skipped_rows 0
```

Every routed query saw shut buckets (`shut ≈ 6/7`) but `cov >= budget`, so
`ann_search.c` declines the swap by design — the warm set is never smaller
than the cold walk. Structural reason (`ann_search.c:588-591`):

```c
static inline uint32_t lz_fog_id(int bucket) {
    int c = bucket % 144;
    ...
    return wl_id((uint32_t)c, 0u);
}
```

`bucket` is an anchor id (0..7, `ANCHR_MAXK=8`), so the latch can only ever
hold **8 distinct cells** and saturates after the first walk. The walk then
always routes into shut cells and there is nothing to skip.

**Consequence for the plan:** `ANN_TOMBSTONE_WARM` is inert in steady state,
not merely conservative. Making the tombstone route anything requires a key
finer than `bucket % 144` (that is N4/N6 territory), and the 144-cell
geometry has no consumer today.

## Bucket skew — phase-2 gate

`/v1/canary` bucket counts (rows are counted in `anchor` AND `anchor2`):

```
counts = [11929, 6993, 21532, 7467, 26529, 10793, 15974, 24497, 0 ... 0]
min 0  p50 0  p90 0  max 26529  mean 245.5  cv 8.735
```

- Only **8 of 144** cells are used at all → the `144 = 4×6×6` latch geometry
  (phase 2) has no address to bind to yet.
- The 8 real buckets are badly skewed: max 26,529 rows (42 % of the index)
  vs min 6,993. `cv = 8.7` over the 144-cell frame.

**Phase-2 gate: FAIL / parked.** Skew exists, but at 8 buckets, not at the
144-cell frame the phase-2 geometry was designed for. Building the 3-axis
latch now would encode a distribution the route does not produce.

## Incidental finding: silent dead route on append (H1, live)

During the run the live server appended rows (62,851 → 62,857). The child's
route immediately flipped `anchor → brute` (`nb=0`, `align=no_route`) while
still reporting `cache=1`; requests kept answering, latency rose, nothing
alerted. That is exactly H1/H2 as built: the anchor table is count-bound
(`antr != nent` → refuse) and the fallback is a full scan.

Option B removes the *stale-pair* half of this (there is no sidecar to be
stale), but **not** the count-bound table half — regrow still has to refresh
labels. The cold-path watcher (H1 fix) stays on the list.

## Files

- `src/ann_search.c` — canary block, `cy_measure_reassign()`, `handle_canary()`,
  boot timers, `GET /v1/canary`, `ANN_CANARY[_RECOMPUTE]`.
- `data/memcore/ann_index.jsonl.canary.log` — raw per-query stream.
- scratch: `build/ann_search_canary.exe` (same source, separate output path).
