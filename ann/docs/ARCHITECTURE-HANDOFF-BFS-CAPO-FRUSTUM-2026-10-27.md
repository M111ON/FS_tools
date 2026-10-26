# BFS / Capo / Frustum Architecture Handoff

**Status:** design record and next-session handoff  
**Date:** 2026-10-27

## Core thesis

The system is a structural field, not a semantic compressor. Data bytes keep
their identity. The system observes them, builds a route, and stores the route
so later lookup can jump instead of walking the whole field.

```text
first append: observe -> route -> record receipt
later search: receipt/route -> warp to entrance -> local search
```

## BFS and focus

The whole world is laid on BFS. Only the node/frustum reached by the current
pointer or focus is materialized at full size. Everything else is compact:

```text
out of focus = pointer + rule + receipt + delta
in focus     = expanded anchor/frustum/roster/vector view
```

Focus movement is expand/contract, not a copy of the world. Required invariant:
`payload bytes moved during focus change = 0`.

## Climate change / capo

Climate change shifts the pattern of the whole world. A capo changes the frame
used to resolve the pattern; it does not move or rewrite fruits/vectors.
Equivalent content appended by different users can later appear at the same
current level without being the same identity.

```text
fruit identity = payload/origin identity
view identity  = user/core/capo frame
current pos    = derived and movable
```

Search selects a view/capo explicitly. Cross-view search is explicit, never an
implicit merge.

## Apex / center core

The cube-like composite in the supplied drawings is the apex/center core, not
a frustum and not a vector container. It is the central junction for the six
KIS+Hyper axes:

```text
apex/center core -> six-axis KIS + Hyper topology -> major branches
                 -> closed frustum containers -> vector fruits
```

The square/triangle material is a structural tessellation primitive, not a
request to build a 3-D renderer or move payload bytes.

## Major, minor, frustum, Totem

The quadtree grows by four major directions per level. Each major has six minor
sides/directions. The apparently reverse direction that is another dimension
is not backtracking.

1. Major branches may grow.
2. A frustum is a closed container and cannot grow further.
3. A closed frustum cannot backtrack.
4. The four apex points of a frustum are its four Totems.
5. Totems are entrance/neighbor identity, not payload and not tags.
6. Neighbor traversal may use frustums that share Totems.

Example: a CAT FAMILY frustum may have four Totems (lion, tiger, panther,
cat), with vector fruits inside. Totems mediate traversal; they are not the
vectors. This resembles HNSW at traversal level, but edges are mediated by
Totem/frustum structure rather than direct vector-to-vector edges.

## Component boundaries

```text
tag sidecar -> metadata/group scope
anchor      -> bucket/entrance selection
Totem       -> entrance-used / neighbor receipt
roster      -> local row membership/span
vector      -> payload fruit, scored after route resolution
```

Tags remain separate from vectors. Totem remains separate from the RDH/anchor
fingerprint. Totem sits at the bucket entrance; it is not the payload
fingerprint.

## Verified ANN prototype

Current route:

```text
anchor -> selected buckets -> Totem state -> mmap roster -> deduplicated walk -> scorer
```

Verified gates:

- zone/roster fixture: 8 checks pass;
- full suite passes;
- receipt exposes buckets, Totem, roster readiness, route K, and roster rows;
- with `ANN_WALK_PRUNE=1`, `roster_ready=1` implies `roster_used=1`;
- walk hash/hits equality remains gated against the legacy path;
- stale routes fail loud or use broad exact fallback;
- roster is mmap-backed and cold-safe by arithmetic bound; cold-page reboot
  measurement is closed as “warm measured, cold bounded by arithmetic”.

The earlier false `roster=0` was caused by a fixture with `ANN_WALK_PRUNE`
disabled, not by a bad roster.

The first correct scratch A/B showed `ANN_ROSTER=0/1`, but only one arm order
and identical `items_scored=12,173`; its latency difference is not an accepted
speed claim. A proper paired benchmark must alternate arm order and record
hits, walk hash, examined, scored, roster used, and latency per query.

## Ownership and ranges

The owner holds the core distance/range for each core type. The universe may be
144 or 20736. The sub-unit region `0 <= x < 1` is owner-only and is never
allocated to another user.

Keep these separate:

```text
owner core range
allocation state/cursor
user/view offset
capo/climate delta
```

The next-user increment is intentionally not frozen; it may be +1, +144, or a
future algorithmic step. Tensor and KV do not share a cursor or address
universe.

## Tensor chain

```text
tensor bytes -> KIS + Hyper -> apex/capo -> major/frustum -> 4 Totems -> fruits
```

Climate shifts coordinate bindings; tensor bytes remain unchanged. Tensor owns
its coordinate universe, fingerprint, receipt, and route rules.

## KV chain

KV is separate and lighter:

```text
KV state -> fixed-width chunks -> ANN append -> Totem receipt -> recall
```

KV does not need full KIS+Hyper geometry. Cold bytes remain in storage; ANN
holds a light receipt containing chunk id, width, sequence/timeline,
Totem/bucket, and route epoch. Fixed width permits direct offset calculation.
A new width is a new format version, never a reinterpretation of old receipts.

Tensor and KV communicate only through explicit interfaces. They do not share
topology, ownership cursor, climate delta, or fingerprint semantics.

## Delta alignment

Different capo views may normalize into a canonical frame:

```text
canonical = current_position - user_offset - capo_delta
```

Structural rules that normalize to the same cell may be deduplicated. Fruits
must not be merged solely because current or normalized positions match. The
climate delta may eventually be one affine offset per axis or a per-level
delta chain; do not freeze the algorithm yet.

## Non-goals

- Do not make vector payloads anchor metadata.
- Do not use tags as vector identity.
- Do not treat Totem as tombstone or payload hash.
- Do not create permanent per-zone mini-DBs before proving the focus boundary.
- Do not freeze a 5-byte format without provenance.
- Do not build a visual 3-D component from the drawings.
- Do not merge Tensor and KV chains.
- Do not restart supervised `:8096` during scratch benchmarks.

## Next-session plan

### A. Receipt benchmark

Alternate roster OFF/ON for the same query vectors and fresh processes. Record
per query: arm, hits, walk hash, buckets, Totem, roster ready/used, examined,
items scored, and latency. Fail on hit or walk-hash differences. Run both arm
orders before reporting a delta.

### B. Focus fixture

Build a small BFS fixture with two focus targets, a closed frustum, four Totems,
and immutable payload bytes. Prove focus A -> B -> A reconstructs from compact
state and moves zero payload bytes.

### C. Two-view capo gate

Append equivalent content at different levels in two views, shift capo, and
prove equal current coordinates do not merge identity.

### D. Structural delta

Apply offsets, deduplicate structural rules only, retain fruit/view identity,
and verify round-trip restoration.

### E. Interfaces

After A-D pass, specify separate Tensor KIS+Hyper and KV fixed-width receipt
interfaces. Do not implement KV here yet.

## Acceptance bar

```text
payload bytes moved by capo/focus shift = 0
closed frustum does not grow/backtrack
four Totems remain stable as entrance identity
view identity survives equal current coordinates
structural dedup does not merge fruits
receipt replay reconstructs the same route
roster ON preserves hits + walk hash
KV receipt recalls exact fixed-width bytes
Tensor and KV remain independently testable
```
