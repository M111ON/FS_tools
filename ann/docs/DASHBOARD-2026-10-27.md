# ANN dashboard — read-only route/index/candidate view (2026-10-27)

Card: `task-tcf515cff` · owner `opencode-ann-k3v` · live root `I:\tools\ann`

## What was built

A separate, read-only dashboard on **:8391** (the taskboard map at :8390 is
untouched). It observes the live ANN data plane at :8096 and never writes to
ANN, never rejects or re-ranks a hit, and never infers a receipt.

```
dashboard/core.py                 pure derivations (no IO, deterministic)
dashboard/server.py               read-only HTTP layer
dashboard/static/index.html       single-page UI, no external assets
dashboard/tests/test_dashboard_core.py
dashboard/README.md
```

```powershell
cd I:\tools\ann
I:\python3.14.4\python.exe dashboard\server.py --port 8391
```

## Live receipts (2026-10-27, this session)

Health (`GET :8096/health` → normalized, `state=live`, warnings `[]`):

```
entries 63,915 · routed 1 · stamps_live 63,915 · route_dead 0 · labels derive
loader bg · stamp gate ok · stamp unplaced 0
totem replayed 108 / shut 62 / free 10,366 / pair 64
roster on · 64 doors · 127,830 rows · 511,320 B · src built
tags 63,915 tagged · 0 unlabeled · 12 kinds
```

Index view (read straight from the sidecars):

```
index rows 63,915 · stamp rows 63,915 · order rows 63,920 (delta +5, covers)
size 263,336,994 B · epoch binding match · anchors n_prev 63,915 · K 64
zones 12 · sum 63,915 = entries → coverage ok
occupancy 64 doors · 127,830 memberships · 0 negative rows
  top: door 13 = 5,377 (2,594 own + 2,783 pair)
```

Candidate view (index row 0, top-k 5, no scope):

```
route  mode anchor+p · buckets 20,48,0,42,56 · totem lit 5
       items_scored 12,950 / 63,915 · budget 24,000 · truncated 0 · fallback 0
       examined 30,722 · roster 1 · walk_hash 0
receipts  valid 0 · invalid 0 · stale 0 · missing 5 · coverage 0
```

Shadow view (`totem_shadow.jsonl`): 2 reports, 0 route violations, 10 missing
candidate receipts, coverage 0, last event `2026-10-27T11:18:36Z`.

## No-mutation proof

Same vector through both paths, back to back:

```
hits identical      : True
route keys identical: True
items_scored        : 12,950 (direct) == 12,950 (through dashboard)
totem lit           : 5 (direct) == 5 (through dashboard)
```

The only outbound calls are `GET /health`, `GET /v1/state/tags` and
`POST /v1/state/search` (a query). Local reads are `rb`/`r` on
`ann_index.jsonl{,.stamp,.order}`, `ann_anchors-state.json`, `tag_lut_report.json`
and the shadow report. There is no write endpoint; unsupported methods → 501.

## Verification

```
dashboard/tests/test_dashboard_core.py   42 tests OK
ann/run_tests.py                         ALL PASS (test wired into PY_TESTS)
py_compile core.py server.py             OK
```

Covered: request validation (including `topk` never coerced), health warnings,
stamp/order parsing, occupancy arithmetic, zone coverage, epoch binding,
receipt parsing, missing/valid/invalid/stale classification, unbound-sidecar
staleness, relation-intent rule, shadow totals and malformed rows, bounded
timeline, deterministic serialization, and the server's error paths
(unavailable health, 502 without ANN, 400 for bad top-k and past-end row, 404,
501 for write methods).

## Visual evidence

- `docs/dashboard-health-2026-10-27.png`
- `docs/dashboard-index-2026-10-27.png`
- `docs/dashboard-candidate-2026-10-27.png`
- `docs/dashboard-shadow-2026-10-27.png`

## Honest gaps

1. **Candidate receipts are missing on the live index** — the hit shape is
   only `sid, file, npast, score`. The dashboard shows `missing 5/5`, it does
   not guess. This is the same producer blocker as `task-t90845db6`.
2. **`/health` carries no `totem.lit`** — only per-query `route.totem` does, so
   the Health tab shows `—` for `lit` and the Route tile computes it from a
   query. Not a dashboard bug; a missing health field if it matters.
3. **`order` is 5 rows longer than the index** (63,920 vs 63,915). `/health`
   reports `order_stale 0`, so this is capacity padding, not a stale order.
   The dashboard reports the delta and only flags under-coverage.
4. **Occupancy is derived from the stamp sidecar**, not exposed by the server.
   It is the same 8 B/row the roster is built from, so it agrees with
   `roster.rows` (127,830).