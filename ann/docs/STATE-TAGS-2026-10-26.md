# `GET /v1/state/tags` — zone discovery (2026-10-26)

An agent that wants to scope a search had two bad options: open
`tag_lut.json` itself (a client-side dependency on a bake artifact) or guess
zone ids. This endpoint answers "which zones exist and how big are they" from
the server, read-only.

## Contract

```
GET /v1/state/tags
200 {"status":"ok",
     "index":"…/ann_tags.tsv",       // the sidecar actually loaded
     "entries":63833,"tagged":63833,"unlabeled":0,
     "lut_zones":12,
     "zones":[{"zone":0,"tag":"architecture","rows":334,"retired":false}, …],
     "zones_n":12}
503 {"status":"error","error":"no_index"}   // nothing loaded yet
```

- `rows` is counted from `g_ent_tag` / `g_ent_zone` — the arrays
  `lz_scope_resolve()` walks — so it is the search path's own per-zone size,
  not a recount of the TSV text.
- `retired` is the frozen LUT's word: a retired tag has zero rows, so the
  sidecar cannot even name it, and the endpoint reports it with `rows:0`.
- `lut_zones` is LUT coverage. If the LUT is missing or its shape changed, the
  zones still come from the sidecar and `lut_zones` drops to 0 — visible, never
  silently claimed.
- Nothing is cached and nothing is written; the endpoint only reads what
  `/v1/state/search` already has resident.

## Live (:8096, 2026-10-26, 63,833 rows)

| zone | tag | rows |
|---|---|---|
| 0 | architecture | 334 |
| 1 | brainstorm | 9,918 |
| 2 | chatpool | 14,185 |
| 3 | config_values | 268 |
| 4 | constraints | 246 |
| 5 | freebuff | 999 |
| 6 | hermes | 12,206 |
| 7 | memo | 1 |
| 8 | naming | 59 |
| 9 | opencode | 4,165 |
| 10 | project_rules | 136 |
| 11 | v1-migrate | 21,316 |

Sum = 63,833 = `tagged` = `entries`; the same numbers as `tag_lut.json`'s zones
block and as a Python recount of `ann_tags.tsv`.

## Verification

`tests/test_state_tags.py` (in `run_tests.py`, 13 checks) builds a scratch
index + sidecar + LUT and asserts:

| check | what it proves |
|---|---|
| Z1 | every sidecar tag listed, with its frozen zone id |
| Z2 | rows == sidecar recount per tag **and** per zone |
| Z3 | rows == the LUT's zones block (one of the two is stale otherwise) |
| Z4 | a retired LUT tag is named, flagged retired, rows 0 |
| Z5 | live rows == tagged, tagged + unlabeled == entries |
| Z6 | each listed zone is walkable: `scope:"zone:<id>"` returns hits and every hit belongs to that tag |
| Z7 | read-only: `/health` untouched |

Z6 uses a real query vector: a zero vector scores 0 everywhere and the prune
floor drops every row, which looks like "the zone is empty" (measured while
writing the test).
