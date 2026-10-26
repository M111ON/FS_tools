# Roster — door → rows, recorded on the append path

The owner's model (2026-10-26): *"การหามันไม่ต้องเดินอีกแล้ว เพราะเราเก็บ fingerprint ตอนมันเดินเข้ามา append ไปแล้ว"* — the search must **jump** to a door, not walk to find who is behind it.

## What was already there

| piece | where | what it holds |
|---|---|---|
| stamp | `ann_index.jsonl.stamp` | 8 B per row = `(anchor, anchor2)` int32 — the row's door pair, written by the regrow **on the write path** |
| totem | `lz_totem_id(bucket)` → `wl_id(cell,0)` | **1 bit per door**: walked / not walked. No membership. |
| order | `ann_index.order` | the bucket-major walk permutation |

So the fingerprint existed; the inverse did not. To find a door's members, the
walk **looked at every row once per routed bucket**:

```
examined = rnb × nent + scored          (measured 2026-10-26, 60 queries, live index 63,808)
         = 330,127 mean  vs  11,087 scored   →  29.8× the work actually needed
```

## What the roster adds

`g_roster[door]` = the rows behind that door, in **walk order**, built from the
same stamps the append path wrote. The walk then becomes:

```
examined = Σ|roster[door]| + scored     →  25,424 mean  (2.29× scored)
```

- **510 KB** at 63.8k rows (64 doors × ~1,993 rows × 4 B), rebuilt with the route cache
- append-only: a new row adds an entry; no vector is ever touched (closed-cluster rule, #7565)
- `ANN_ROSTER=0` disables it; failure to allocate falls back to the scan (never to a wrong answer)

## Gate — the roster must be invisible

`bench_rows_examined.py --ab` on the live 63.8k index, 60 queries, production budget:

| check | result |
|---|---|
| top-k identical | **60/60 PASS** |
| **walk fingerprint identical** (`route.walk_hash`) | **60/60 PASS** |
| items_scored identical | **60/60 PASS** |
| rows examined | 330,127 → 25,424 (**13.0×**) |
| examined / scored | 29.78× → 2.29× |

`tests/test_roster.py` repeats the same gate on a synthetic index **with a
budget cut** (12 rows): a reordered walk survives a top-k comparison but a
budget cut keeps a different prefix, so the cut is what proves order equality.

## The bug the fingerprint caught

The first version built the roster with `ord = NULL` (identity order) while the
walk ran in `g_ord_cache` order. Sets were identical, `items_scored` identical,
and **59/60 top-k identical** — a top-k diff alone reads as "one tie" and hides
the cause. The walk fingerprint showed `0/60` and the dump showed the ON walk
ascending (`9 92 95 348 …`) against the OFF walk permuted (`29392 20431 …`).

Cause: `lz_labels_derive()` installs its own bucket-major permutation into
`g_ord_cache` (`ann_search.c:1349`), so in derive mode the roster must be built
from `g_ord_cache`, never from `NULL`. Fixed 2026-10-26; the fingerprint then
reports 60/60.

## Latency — what it actually buys (measured 2026-10-26, after a PC restart)

`bench_roster_scaling.py`, live index 63,812, prune on (`anchor+p`), 100 queries,
fresh server per arm, same bucket count on both sides:

| buckets | roster OFF p50 | roster ON p50 | speedup | scored (both) |
|---|---|---|---|---|
| 5 (today's live) | 10.95 ms | **7.17 ms** | **1.53×** | 10,824 |
| 16 | 23.91 ms | **14.23 ms** | **1.68×** | 24,348 |

The gain grows with the routed-bucket count, exactly as the arithmetic says:
without a roster the gather is `rnb × n` looks, with one it is `Σ|members|`.

**Retraction.** An earlier arm (`bench_query_speed.py`, prune off, 2 reps,
default buckets) reported p50 11.91 → 11.91 ms and I read that as "the roster is
free but buys nothing". That arm was not the live configuration: with the prune
off the walk scores every routed row and the gather is a sequential sweep the
prefetcher hides. With the live settings (prune on, 5 buckets) the same change
is 1.53×. Same code, different regime — the number to quote is the table above.

The cold-page arm (`EmptyWorkingSet`) did **not** run — `OpenProcess`/
`EmptyWorkingSet` returned false, so those columns are zeros, not measurements.
A cold test still needs a real page-cache flush.

## Not in scope

- the 3-axis latch (144 = 4×6×6): a bijection 144→144 renames cells; it keeps
  1 bit per door and adds no membership
- `ANN_TOTEM_WARM` / `ANN_WARM_KEEP`: the cut that skips rows. The roster makes
  the *lookup* cheap without skipping anything — different question
