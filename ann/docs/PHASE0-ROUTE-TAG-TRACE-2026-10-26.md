# Phase 0 route/tag trace

This is the code-verified baseline before changing the hot path.

## Current request path

1. `lz_search` parses `scope` and resolves tag/zone through the tag sidecar.
   A closed `tag:`/`zone:` scope skips anchor, Totem, and order routing by
   design; it walks the selected group only.
2. Without a closed scope, `lz_route_cached` supplies the anchor table/order.
   `anch_route` selects buckets from the query and centroid table.
3. `lz_totem_shut` reads the Totem latch for selected buckets. Totem is a
   route-used marker; the first OPEN→SHUT transition is persisted, repeats are
   no-ops.
4. `lz_roster` is a derived door→row-membership table. When loaded, the walk
   uses the roster to avoid scanning every row once per selected door, then
   deduplicates row indices with `pseen`.
5. The scorer still scores the resulting walk list. The route is only a real
   warp when `items_scored`/walk size is bounded by the selected roster/group,
   not by the global search budget.
6. Missing/dead route falls back to a broad exact walk. A dead route must not
   return a narrowed stamped subset.

## Important boundary

Tag and anchor are sibling chains:

```text
tag sidecar → closed zone scope → exact in-group walk
anchor      → Wang/latch buckets → Totem → roster → local walk
vector      → payload scored only after the walk is resolved
```

The current implementation confirms that explicit closed tag/zone scope
bypasses the route layer. This is correct semantics for scoped search, but it
is not evidence that a global query has warped to a bucket.

## Known gap to fix in Phase 1

The roster currently has two memberships per entry in the common path
(`anchor` and `anchor2`). It deduplicates before scoring, but the lookup work
and `examined` receipt can still reflect the duplicated door walks. The Phase 1
gate must compare the exact row walk list, hash, and scored count against the
legacy path, then measure whether roster lookup reduces row looks rather than
merely changing the order.

## Phase 1 implementation boundary

Use a scratch fixture and fresh process for A/B. Do not restart the supervised
`:8096` service. Add only the route-only receipt/gates needed to expose:

- selected bucket IDs and Totem state;
- roster span/row count;
- exact deduplicated walk hash;
- `items_total` and `items_scored`;
- fallback reason when route is absent or stale.

Do not add RDH, freeze a 5-byte format, create a mini-DB, or move vector bytes
until this evidence proves the entrance boundary is the bottleneck.
