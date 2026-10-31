# Numeric multi-view benchmark contract

## Producer-owned views

Each row has explicit producer references, independent of ANN geometry:

- `value`: operands and result, including position (`lhs`, `rhs`, `result`)
- `shape`: operator/form, with numbers replaced by `N`
- `relation`: operation and magnitude class
- `context`: distractor metadata; it is never part of the default truth

## Query truth

Queries are generated independently from route references. The oracle ranks all
rows by the declared query intent and returns the true top-k. Route references
are only candidate generators. A junction is the derived union of the three
primary views; it is not a fourth semantic view.

## Failure model

Every path may have controlled errors, sampled independently per query:

- route miss: omit valid references
- confident false-positive: add irrelevant references with high route score
- boundary spill: add adjacent-form references
- duplicate references: deduplicate before scoring

## Paths

- `baseline`: one primary route plus its configured miss/spill model
- `union`: `value ∪ shape ∪ relation`
- `junction`: the same union with a derived shared-junction expansion

## Required report

For at least 500 queries per class and 2,000–10,000 rows, report mean and
p10/p50/p90 for R@5/R@10, wrong@k, items scored, route lookup, union/dedup,
and total time. Split interior and boundary queries.

Gate: multi-view gain >= 0.02, bounded candidate budget, boundary recall drop
no more than 2–5 percentage points, deterministic candidate order. No
production ANN change before this gate passes.
