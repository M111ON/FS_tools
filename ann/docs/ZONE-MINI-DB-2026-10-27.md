# Per-zone mini vector DB — gate and decision (2026-10-27)

## Scope

`scope=zone:N` is a closed query. The zone must never widen to a global answer.
The mini-index is tree-side and derived from the frozen tag sidecar. Vector
payloads remain in the shared index; no vector is copied, rewritten, or
deleted.

## Baseline measurement

Artifact: `bench_zone_scratch_ab.py`, 40 real index-row queries, 63,872 rows,
384 dimensions, production budget and prune settings.

| zone | rows | p50, roster off | p50, roster on | scored |
|---|---:|---:|---:|---:|
| `zone:5` | 1,012 | 2.1 ms | 2.1 ms | 1,012 |
| `zone:11` | 21,316 | 11.1 ms | 10.8 ms | 21,316 |

`zone:7` has 1 row and is correctly refused for `topk=5`.

The row cost scales with zone size. A per-zone candidate index is therefore a
valid optimization target. The fixed HTTP/heap floor is not the target.

## Live crossover sweep

Follow-up receipt: live `:8096`, 20 real row-vector queries per zone, `topk=5`.
Every accepted zone scored exactly its row count; no zone widened or fell back.

| zone | rows | p50 ms | p90 ms | scored |
|---:|---:|---:|---:|---:|
| 8 | 59 | 1.52 | 4.94 | 59 |
| 10 | 136 | 1.54 | 3.01 | 136 |
| 4 | 246 | 1.93 | 6.71 | 246 |
| 3 | 268 | 1.63 | 3.59 | 268 |
| 0 | 334 | 1.86 | 8.64 | 334 |
| 5 | 1,012 | 4.66 | 15.58 | 1,012 |
| 9 | 4,169 | 6.92 | 33.46 | 4,169 |
| 1 | 9,918 | 6.30 | 17.63 | 9,918 |
| 6 | 12,206 | 5.69 | 10.35 | 12,206 |
| 2 | 14,207 | 8.79 | 12.02 | 14,207 |
| 11 | 21,316 | 11.19 | 20.46 | 21,316 |

The crossover is visible: small zones are already near the HTTP floor; larger
zones remain row-scan bound. A second float-vector matrix would not improve the
closed-scope scorer and is rejected by the same evidence.

## Production A/B receipt

`bench_zone_ab_live.py` ran both arms through the production `ann_keepalive`
environment, one process at a time, on the live 63,872-row index. The live
server was restored successfully afterward (`entries=63872`).

| arm | global top-5 p50 | zone 5 p50 | zone 11 p50 |
|---|---:|---:|---:|
| `ANN_ZONE_ROWS=0` | 19.5 ms | 13.4 ms | 34.7 ms |
| `ANN_ZONE_ROWS=1` | 15.7 ms | 4.3 ms | 19.6 ms |

Both arms returned `items_scored=1012` for zone 5 and `21316` for zone 11.
Both refused `zone:7` with one row as `scope_too_narrow`. The global route was
not changed by enabling the zone index; the global timing difference is normal
run-to-run process/cache variance, not a zone-index claim.

This closes the implementation and production verification gates.

## Implementation

The shipped slice is a packed zone-local row index:

- `ann/src/ann_search.c` builds `zone -> uint32 row indices` from the existing
  frozen sidecar.
- Row indices preserve the legacy ascending walk order.
- A zone query copies only that row span into the walk; shared `LZEntry.vec`
  payloads remain untouched.
- `ANN_ZONE_ROWS=0` restores the legacy two-pass filter for same-binary A/B.
- Allocation failure falls back to the legacy path; it never narrows results.

This is the mini vector DB boundary without a second vector store. A duplicated
per-zone float matrix would violate the closed-fruit rule and add no semantic
value because the scorer already owns the shared vectors.

## Correctness gate

`tests/test_zone_rows.py`: **8/8 PASS**.

- zone 1 and zone 3: identical hits, `walk_hash`, and `items_scored`
- narrow zone remains `scope_too_narrow`
- missing sidecar follows the legacy path
- `/health` reports the live roster

Full `ann/run_tests.py`: **ALL PASS**.

The gate compares against the same binary with `ANN_ZONE_ROWS=0`; it does not
compare against a stale executable.

## Decision

The packed row index is the accepted zone-local mini-index. Do not add a
duplicated vector matrix unless a future measurement proves that shared-vector
scoring, rather than row selection, is the bottleneck.
