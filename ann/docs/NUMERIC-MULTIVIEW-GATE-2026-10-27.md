# Numeric multi-view routing gate — 2026-10-27

## Scope

Measurement only. The probe compares a single primary route with conditional
multi-view expansion over producer-owned `value`, `shape`, and `relation`
references. It does not change `ann_search.c`, the ANN index, or production
route artifacts.

## Fixture and failure model

- 2,000 generated arithmetic rows plus symbolic seed rows.
- 500 independently sampled queries, classified as value, shape, relation, or
  boundary intent.
- Controlled route miss, confident false-positive, boundary spill, and
  duplicate candidates.
- Brute-force truth is generated from producer intent; geometry only produces
  candidates.
- Conditional path starts at the primary route, expands only when sparse, and
  applies a shared candidate budget of 800.

## Measurements

| Class | Single-route R | Conditional R | Single scored | Conditional scored |
|---|---:|---:|---:|---:|
| value | 0.369 | **0.919** | 33.4 | 35.5 |
| shape | 0.347 | **0.619** | 634.6 | 680.9 |
| relation | **0.940** | 0.719 | 1,596.2 | **793.4** |
| boundary | 0.494 | **0.756** | 712.6 | **505.2** |

The conditional path reduced wrong candidates in value, shape, and boundary
probes, but relation queries regressed despite lower candidate cost. The first
unbalanced noise run was discarded; the equal-false-positive-budget rerun is
the authoritative result.

## Gate verdict

**FAIL — do not ship a route-reference sidecar or C implementation.**

Multi-view routing is useful for value and boundary intents, but it is not a
universal expansion policy. A query intent signal or a separately proven
relation route is required before revisiting this work. Current ANN artifacts
also have one tag and one route per SID, so a real-data multi-view test remains
blocked at the producer schema.

## Completed TODO

1. Fixture contract: complete.
2. Independent query/oracle generation: complete.
3. Controlled noise model: complete, with symmetric rerun.
4. Aggregate benchmark: complete (`2,000` rows, `500` queries).
5. Metrics and evaluation: complete.
6. Gate: failed honestly.
7. Production implementation: intentionally not started.
