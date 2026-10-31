# Route producer integration boundary — 2026-10-27

## Verified insertion point

`ann/scripts/build_index.py` is the correct producer boundary. Its row loop
already has `entries.id`, `kind`, `term_hint`, `content`, and the embedding
while writing `ann_index.jsonl`.

## Current blocker

The source query has no producer-owned intent, value routes, shape routes, or
typed relation edges. `file`, `kind`, `term_hint`, vectors, and ANN anchors
cannot be used to infer those semantics. Therefore the live producer may only
emit `intent=unknown` until an upstream metadata source keyed by `entries.id`
exists.

## Required upstream source

Add or export a producer-owned relation/intent table keyed by stable `sid`.
The minimum record is the v1 route sidecar contract in
`ann/route_sidecar_schema.md`. Missing metadata must produce safe baseline,
not guessed routes.

## Atomic generation requirement

The index and sidecar must be generated as one epoch:

```text
ann_index.jsonl.tmp
ann_routes.jsonl.tmp
ann_routes.jsonl.manifest.json.tmp
```

The production manifest should additionally bind the sidecar to the index with
`index_rows` and `index_sha256`; row count and SID validation alone cannot
prove that same-SID records came from the same generation. Sidecar consumers
must reject mismatches and use the baseline route.

## Decision

Do not edit `build_index.py` to emit fabricated routes. The offline schema,
validator, numeric producer fixture, relation graph consumer, and intent safety
gate are complete. Production integration is blocked at the upstream metadata
source and remains intentionally unshipped.
