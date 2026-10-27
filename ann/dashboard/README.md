# ANN dashboard — read-only

A separate, read-only view of the live ANN data plane at `:8096`. It never
writes to ANN, never rejects or re-ranks a candidate, and never infers a
receipt from vector values, buckets, `term_hint`, file names or geometry.

It is **not** the taskboard map at `:8390`; that stays untouched.

## Run

```powershell
cd I:\tools\ann
I:\python3.14.4\python.exe dashboard\server.py --port 8391
# open http://127.0.0.1:8391
```

Optional flags:

| flag | default | meaning |
|---|---|---|
| `--ann` | `http://127.0.0.1:8096` | ANN data plane to observe |
| `--index` | `I:\tools\ann\data\memcore\ann_index.jsonl` | index whose sidecars are read |
| `--report` | `...\totem_shadow.jsonl` | append-only shadow report |
| `--receipts` | *(none)* | optional `sid -> totem_receipt` sidecar |

Tests:

```powershell
I:\python3.14.4\python.exe dashboard\tests\test_dashboard_core.py   # 42 tests
I:\python3.14.4\python.exe run_tests.py                            # full suite (wired)
```

## Views

| view | source | what it shows |
|---|---|---|
| **Health** | `GET :8096/health` | state (`live`/`degraded`/`unavailable`), entries, routed, route_dead, loader, stamp gate, totem, roster, and the warning list behind `degraded` |
| **Index** | `.stamp` + `.order` + `ann_anchors-state.json` + `tag_lut_report.json` + `/v1/state/tags` | epoch binding, per-door occupancy (`own` + `pair` memberships), zone split, tag-bake receipts |
| **Candidate** | `POST :8096/v1/state/search` | route receipt and top-k, with per-candidate receipt status |
| **Shadow** | `totem_shadow.jsonl` | route violations, receipt coverage, missing receipts, malformed rows, append-only timeline |

## Data contracts (read from the live server, not assumed)

`GET /health` (observed 2026-10-27):

```json
{"status":"ok","service":"ann_search","entries":63913,"routed":1,"stamps_live":63913,
 "route_dead":0,"maxk":32,"k":64,"labels":"derive","order_stale":0,
 "loader":{"mode":"bg","index_age_s":5170.4},
 "tags":{"kinds":12,"tagged":63913,"unlabeled":0},
 "stamp":{"live":63913,"unplaced":0,"side_rows":63913,"gate":"ok","gate_code":0},
 "totem":{"replayed":108,"shut":62,"free":10366,"pair":64},
 "roster":{"on":1,"doors":64,"rows":127826,"bytes":511304,"src":"built"}}
```

`POST /v1/state/search` takes `{"vec": [...], "topk": N, "scope": ""}` and returns
`{"status","hits":[{"sid","file","npast","score"}],"route":{...}}`. The route
receipt carries `mode`, `buckets`, `walk_hash`, `items_scored`, `items_total`,
`budget`, `truncated`, `fallback`, `examined`, `roster`, `totem{lit,open,shut,
replayed,free,pair}` and `amp{on,k,applied}`.

`GET /v1/state/tags` returns `zones[{zone,tag,rows,retired}]` plus
`entries/tagged/unlabeled/lut_zones`.

Sidecars read (read-only, 8 B / 4 B per row):

- `ann_index.jsonl.stamp` — `(anchor, anchor2)` int32 pair per row; the
  append-time door pair. The dashboard counts **both** memberships because
  both are real walk work.
- `ann_index.order` — bucket-major walk permutation (row count only).
- `ann_anchors-state.json` — `n_prev`, `index_size`, `index_mtime`, `K`,
  `anchors_cksum`. Compared with the live `index` stat to expose a stale
  sidecar (the "index frozen for days" bug class).

## Candidate receipts — honest by construction

The live memcore index carries only `sid, file, npast, score`. There is no
producer-owned `totem_receipt` yet, so the dashboard shows:

```
valid 0 · invalid 0 · stale 0 · missing = topk
```

A receipt is only ever shown as `valid` when all of these hold:

1. a receipts sidecar is configured,
2. its sibling `*.manifest.json` exists and the epoch is bound,
3. `validate_receipt()` (the canonical `totem_shadow_validate.py`) returns no
   violations against the manifest's `generation`, `totems`, `frustum`, `edges`
   and `intent`.

`stale_generation` renders as **stale**; other violations render as **invalid**;
a differing epoch binding renders every present receipt as **stale**
(`index_binding_mismatch`). SIFT1M receipts, when used, are labelled
"structural test data only — not semantic truth".

**Remaining blocker:** the producer/index must emit
`sid -> totem_receipt` (see `docs/ROUTE-PRODUCER-INTEGRATION-2026-10-27.md`,
board card `task-t90845db6`). Until then missing receipts stay an observation;
hard reject is disabled and this dashboard has no path to enable it.

## Constraints (enforced, not documented-only)

- Read-only: `do_GET` serves views, `do_POST` only forwards a search query.
  There is no write endpoint; unsupported methods return 501.
- No mutation of search or index state: no re-rank, no filter, no hard gate.
- No model/JEV call in the query path.
- No credentials or tokens anywhere in source, logs or fixtures.
- Bounded polling: manual refresh, plus an explicit 10 s toggle (default off).
- Malformed data fails visibly: bad JSON in a sidecar or a report row is listed
  with its line number, never silently dropped.
- `topk` is validated (1..64, integer), never coerced.

## Files

```
dashboard/core.py                 pure derivations (no IO, deterministic)
dashboard/server.py               read-only HTTP layer
dashboard/static/index.html       single-page UI (no external assets)
dashboard/tests/test_dashboard_core.py
```