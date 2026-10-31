# ANN route sidecar contract v1

The sidecar is producer output. It contains routing references only; vectors
and payloads remain in the existing index.

## Record

One UTF-8 JSON object per line, keyed by `sid`:

```json
{
  "v": 1,
  "sid": "stable-id",
  "intent": "relation",
  "value_routes": [12, 19],
  "shape_routes": [4],
  "relation_edges": [
    {"to": "other-id", "kind": "equals", "dir": "out"}
  ],
  "junction_routes": [7],
  "producer_seq": 42
}
```

## Rules

- `sid` is unique and must match an existing index row.
- `intent` is one of `value`, `relation`, `shape`, `composite`, `unknown`.
- `relation` records use typed edges; graph traversal owns relation candidate
  expansion. They must not be sent through generic view UNION.
- `unknown` has no permission to open UNION; it uses the safe baseline.
- Route arrays contain non-negative integer IDs, are deduplicated and sorted.
- `relation_edges` are deterministic after sorting by `(dir, kind, to)`.
- Missing, stale, duplicate, malformed, or checksum-invalid sidecars are a
  safe-baseline condition, never a reason to discard the index.
- Sidecar references may duplicate a payload; payload/vector bytes are never
  duplicated by this artifact.

## Integrity

The producer writes a separate manifest containing row count, producer epoch,
and SHA-256 of the canonical sidecar bytes. Consumers verify the manifest
before enabling non-baseline routing.
