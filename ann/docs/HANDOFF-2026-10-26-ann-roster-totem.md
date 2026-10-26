# HANDOFF — 2026-10-26 · totem rename, warm-keep gate, roster (door→rows)

From: `opencode-main-q9x` (OpenCode, ANN)
Live root: `I:\tools\ann` · live server `:8096` (keepalive-owned) · repo `I:\tools` (git)

## 1. What landed today (4 commits)

| commit | what |
|---|---|
| `277b031` | rename `tombstone` → **`totem`** everywhere in ann (179 replacements, 19 files, no leftovers) |
| `b307a77` | `ANN_WARM_KEEP=90` — the warm/totem cut may not scan less than 90 % of the cold walk |
| `a71d318` | **roster** — `door → rows`, the inverse of the append-time `.stamp` pair |
| `45f4cad` + `cca5e7d` | benches: `bench_query_speed.py`, `bench_roster_scaling.py`; retract the "free but buys nothing" arm |

Live binary = `build/ann_search.exe` copied to `build/ann_search_batch.exe`;
backups `ann_search_batch.pre-roster-25691026-094511.exe` and `…pre-totem-…`.
`/health` now carries `totem{...}` and `roster{on,doors,rows}`.

## 2. The one thing that was actually broken

The totem/warm path (shipped 2026-10-23) **lost recall on live**, not merely
"inert". `src/ann_search.c:2051` gates on `cov < budget`; that bound was
calibrated at K=8 where one bucket held 24–30 % of the index (cut 0.4 % of the
route). At K=64 a bucket is ~3 %, so the same bound cut 20–80 % of the route:

| pass (60q, live 63.8k, production budget) | items_scored | R@1 | R@5 | R@10 |
|---|---|---|---|---|
| cold | 11,218.8 | .9833 | **.9333** | .9250 |
| warm ON | 10,847.1 | .9667 | **.9100** | .9050 |

8/60 queries fired the cut; by kept-fraction: kept < 0.80 → R@5 .867→.733 (one
query 1.000→**0.000**), kept ≥ 0.90 → unchanged. `ANN_WARM_KEEP=90` puts a floor
under the scan, which makes the cut fire **0/200** — a safe no-op, not a lever.
`ANN_PAIR=own` A/B says the loss is not the centroid partner.

## 3. The thing that pays: the roster

The walk used to touch every row once **per routed bucket** to find that bucket's
members (`examined = rnb × n + scored`). The roster is the inverse of the 8 B
`.stamp` pair the regrow already writes on the append path: `door → rows` in walk
order, 4 B/row, rebuilt with the route cache, 510 KB at 63.8k rows.

Gate (60q, live index, production budget) — **all three must pass; the third is
the one that matters**:

| check | result |
|---|---|
| top-k identical | 60/60 |
| items_scored identical | 60/60 |
| **walk fingerprint identical** (`route.walk_hash`, `ANN_WALK_HASH=1`) | 60/60 |

The fingerprint caught the only real bug: the roster was built from **NULL**
order in derive mode while the walk ran in `g_ord_cache` order — same row set,
same scored count, 59/60 top-k (reads like one tie). `lz_labels_derive()`
installs its own bucket-major perm in `g_ord_cache` (`ann_search.c:1349`), so the
build must read `g_ord_cache`, never NULL. Walk dump before the fix:
ON `9 92 95 348…` ascending vs OFF `29392 20431 12013…` permuted.

### Latency (measured after a PC restart, 100q, fresh server per arm)

| buckets | roster OFF p50 | roster ON p50 | speedup | scored (both) |
|---|---|---|---|---|
| 5 (**today's live**) | 10.95 ms | **7.17 ms** | **1.53×** | 10,824 |
| 16 | 23.91 ms | 14.23 ms | 1.68× | 24,348 |
| 24 | 27.34 ms | 13.81 ms | 1.98× | 24,348 |
| 64 | 35.92 ms | 14.04 ms | 2.56× | 25,526 (budget-capped) |

**Retraction to carry forward:** an earlier arm (prune off, 2 reps, default
buckets) showed 11.91 → 11.91 and I wrote "free but buys nothing". Wrong regime —
with prune off the gather is a sequential sweep the prefetcher hides. Quote the
table above, never that arm. `ROSTER.md` holds both numbers plus the retraction.

**Honest scale answer:** vs a tuned numpy BLAS full scan (10.34 ms p50 on this
box) the routed walk is only **1.4×**; the index is 98 MB and fits in RAM, so
brute force is already fast. The roster's value is the *shape* of the cost
(`Σ|members|` instead of `rnb × n`), which is what makes more buckets affordable.
The cold-page arm did **not** give a measurement: `EmptyWorkingSet` reported
success but the file's pages stayed on the standby list, so the "cold" column
just re-read RAM. Verdict (owner, 2026-10-26): **warm measured, cold bounded by
arithmetic** — the roster payload is `rows x 4 B` = 0.49 MiB for 127,694
memberships against a 258 MB index jsonl, so it can never be the cold cost. The
roster is now file-backed (mmap, `src:built|mmap` in `/health`), so a cold start
faults pages instead of rebuilding; no reboot measurement is needed. Details and
the 9-check gate: `docs/ROSTER-COLD-2026-10-26.md`.

## 4. Correctness receipts

vs exact numpy over the whole index (30q): top-1 match **29/30**, top-5 overlap
**140/150 (93.3 %)**, mean top1–top5 score gap 0.069. That is the pre-existing
approximation of the router, **not** the roster (walk bit-identical proves it).
Production path exercised end-to-end: `cloud_ws/memcore/v2.py` →
`:8096/v1/state/search` returned the card written that same hour; `/v1/canary`
`n=194, trunc=0`.

## 5. Traps (do not repeat)

- **A counter can be right while the order is wrong.** Equal `items_scored` +
  equal top-k still hid a reordered walk. Keep `ANN_WALK_HASH=1` in any A/B.
- **Do not `grep` `trunc`** and read it as "cut short" — warm queries set it.
- **Never overwrite a running exe** on Windows: kill → copy → start. Header
  case in `ann_search_batch.exe` (lower case) is what keepalive launches.
- **A test that passes only because the ambient shell carries an env var is not
  a test.** `tests/test_loader_swap.py` L5b asserted the stamp overlay while the
  default label source is `derive` (overlay off by design) and only passed when
  `ANN_LABELS=stamp` happened to be exported; now pinned inside the test.
- **`bench_*` scripts spawn their own scratch servers** — pass the live index
  read-only, never point them at the live log/tags.
- **Do not reuse the frustum rescope work here.** That is a different layer
  (`task-td7fa1248` / `task-t5289298c`), owner session gone, spec says 6-direction
  frustum composite, not the 144² grid.

## 6. Remaining, in order

1. `task-ta90e5e78` — owner: Q1–Q3 answered by shipped code, only **Q4** open
   (phase-2 = geometry only?) + whether the `(pair<<1)|end` latch relayout for
   the centre crossing is still worth doing on its own.
2. ~~`task-t87ad8462`~~ **DONE 6a3134c** — act path hardened (a real NameError
   was waiting on the first H2 firing), gate visible in `/health` (`stamp.gate`),
   watcher ALERT + exit 1, 10 checks.
3. ~~`task-tabbd47c5`~~ **DONE 43ccb64** — bias = soft zone nudge; hard filter
   keeps 60.3 % of the global top-5, soft 0.05 keeps 74.3 % at 86.3 % in-zone.
4. ~~`task-t5ba6ae63`~~ **DONE 250ea5d** — `GET /v1/state/tags`, 13 checks.
5. `task-t4fbea5cf` — per-zone mini vector DB, **unblocked with a measured
   budget** (soft/hard both still score ~16.7k rows).  THIS IS NEXT.
6. `task-tb17d8541` — 7 old items; 1/7 verified done (`test_wang_latch` wired).
7. `task-t73aa3e3f` — 3 owner decisions (vec format, store bloat, daemon off).
8. `task-td7fa1248` / `task-t5289298c` — frustum rescope, unowned now.
9. No card yet: STRESS #9–#15 (stale cache on same-second rewrite, 2047-char
   truncation, topk coercion, telemetry).
10. ~~roster dedup + cold-page~~ **CLOSED b977d03** — dedup measured no gain
    (8.68 vs 8.82 ms, gate passed, reverted); cold is "warm measured, cold
    bounded by arithmetic" with the roster now file-backed/mmap
    (`docs/ROSTER-COLD-2026-10-26.md`, 9 checks).

## 7. Start here

```powershell
cd I:\tools\ann
git log --oneline -6
I:\python3.14.4\python.exe run_tests.py            # ALL PASS incl. tests/test_roster.py
(Invoke-RestMethod localhost:8096/health)  | ConvertTo-Json -Depth 4
(Invoke-RestMethod localhost:8096/v1/canary).queries
I:\python3.14.4\python.exe bench_roster_scaling.py --queries 40 --buckets 5,16
```
