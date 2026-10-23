# HANDOFF 2026-10-23 — ANN: route truth comes from computation, not sidecars

Read this first, then `docs/ANN-UPGRADE-PLAN-2026-10-22.md` (plan) and
`I:/DWGLS-native-fs/docs/ANN-UPGRADE-2026-10-23-PHASE0.md` (today's receipts).
Board: http://127.0.0.1:8390/task/ui — cards `task-t5a6133d1` (plan),
`task-t3d81ef58` (phase 1, DONE), `task-tcce8f5f2` (phase 2, partial),
`task-t072a1c52` (watcher, DONE), `task-ta90e5e78` (4 owner decisions, blocked).

## 1. What changed in one sentence

The route no longer depends on any written sidecar: labels + walk order are
**derived at boot** from `ann_index.jsonl` + `ann_anchors.bin`, so a lost or
stale `.stamp`/`.order` is a cache miss, never a dead tree.

## 2. State at handoff (verified, not assumed)

```
live :8096  entries=62874+  k=24  labels=derive  routed=1  stale=0
            canary: trunc=0 warm=4 scored=52835 (13.2k/q) notes=6
            skew: max bucket 10725 (was 26533 at K=8), cv=0.53
binary      build/ann_search_batch.exe   (backups: ann_search_batch_v1.exe, _v2.exe)
commits     FS_tools 78d46e7 · DWGLS-native-fs 530a137 (both pushed)
tests       run_tests.py ALL PASS (re-run it before touching anything)
```

## 3. The five findings that produced this design

1. **The trio was inconsistent in the wild.** `.stamp` did not exist, `.order`
   was 5 rows stale, anchors table 4 rows short. `:8096` routed only because its
   in-memory cache predated the drift. A restart at that moment would have
   silently fallen back to a full scan.
2. **The index grows continuously** (ann-sync appends every few minutes; +8 rows
   were added while this session ran). Any file-bound route is stale within
   minutes. Do not "write sidecars more often" — do not depend on them.
3. **`K` was hardcoded to 8 for every corpus >= 64 rows** (`ann_search.c`, regrow
   path). At 62.8k rows the largest bucket (25.7k) exceeded `LZ_SEARCH_BUDGET`
   (24k), so every query was truncated mid-bucket (300/300 in the bench).
4. **`LZ_ANCH_MAXK` was 8**, and several route buffers are `int t2[LZ_ANCH_MAXK]`
   on the stack — the old binary cannot serve a K>8 table at all.
5. **The skew metric was wrong**: it divided by the 512-slot array instead of K,
   so `min` was always 0 and `cv` meaningless. `min`/`max`/`cv` now use K.

## 4. Measured results (300q vs numpy brute oracle, budget 24k)

| K  | max bucket | scored  | trunc   | R@5    | R@10   |
|----|-----------|---------|---------|--------|--------|
| 8  | 25,693    | 24,000  | 300/300 | .9533  | .9367  |
| 16 | 15,661    | 24,000  | 300/300 | .9767* | .9700* |
| 24 | 9,081     | 16,329  | 0       | .9680  | .9613  |
| 32 | 8,459     | 14,133  | 0       | .9567  | .9550  |

\*60-query run. Raising K does **not** reduce variance (`cv` .35-.47 flat) — it
shrinks the **largest** bucket, which is what the budget must cover. That is the
structural reason, not a tuned constant.

Shipped rule: `K = clamp(round_up4(ceil(n/3000)), 8, 48)`, env override
`ANN_ANCHOR_K`. The cap binds at ~144k rows — that is the next regime change.

## 5. What is where

| Concern | Location |
|---|---|
| label derive + boot order | `src/ann_search.c` `lz_labels_derive()` |
| route cache / append-only contract | `lz_route_cached()` — `n >= ntr` routes, `n < ntr` refuses |
| order self-heal (restamp) | `lz_restamp_worker()` — loader thread, 1/min, mtime-quiet |
| canary counters (always on) | `LZCanary g_cy`, `GET /v1/canary`, file stream needs `ANN_CANARY=1` |
| cold-path watcher | `ann_keepalive.py::_maybe_regrow`, `--plan`, `--force-regrow` |
| docs | `docs/HANDOFF-2026-10-23-ann-route-truth.md` (this file) |

## 6. Traps (do not repeat)

- **Do not `grep` for `trunc`** and assume it means "cut short": warm queries set
  `budget == cov` by construction, so only cold walks count (this was a self-
  inflicted false alarm on live at budget=6471).
- **`/v1/canary` right after a regrow can take ~56 s** (index reload happens in
  the request that finds the invalidated cache). Use a longer HTTP timeout.
- **Never overwrite a running exe** on Windows: copy it aside first, stop the
  process, then swap.
- **`ann_search_batch.exe` is the name keepalive launches.** Building only
  `ann_search_canary.exe` leaves the live service on the old binary — that is
  exactly what happened this morning (`/v1/canary` 404'd until the swap).
- Benchmark scripts spawn their own scratch servers: pass `--index/--anch/--order`
  and remember `ANN_WALK_PRUNE=1` (the live server sets it; without it the scored
  counts differ by ~2x and it looks like a routing difference).
- A **60-query** sample is enough to rank configs but not to certify a default;
  300q is the gate used above.

## 7. Next (in order)

1. **Let the watcher fire.** Trigger is a K breakpoint (72k rows) or `routed==0`.
   Confirm it heals and that `/health` tells the truth throughout — the act-path
   is wired and plan-checked but has never run on real growth.
2. **Note-as-authority is only half wired**: live reports `fog.replayed=0`. The
   P3 route log the service reads is empty while the note lives elsewhere. Either
   point `ANN_ROUTE_LOG` at the real note or make boot replay explicit.
3. **Latch 3-axis geometry** (phase 2 remainder): skew data now exists
   (`max/mean 4.1` at K=24) but no consumer is chosen. Do not start without
   picking the consumer.
4. **Owner decisions still open** (`task-ta90e5e78`): note-vs-sidecar conflict
   policy, note file shape, 32 B/record width, phase-2 scope. Do not assume.
5. `fog_live` / `replayed` / `heal` keys were printed by an older build —
   `/health` now carries `labels`, `k`, `order_stale`, `stamp_stale`. Update any
   script that scrapes the old keys.

## 8. Start here

```bash
cd I:/tools/ann
git log --oneline -3
I:/python3.14.4/python.exe run_tests.py          # ALL PASS expected
curl -s localhost:8096/health
curl -s localhost:8096/v1/canary                # real traffic counters
tasklist | grep ann_search                      # exactly ONE
```
