# LC TileCube Architecture

## Layer Stack

```
Geometry (v1.0) -> Incidence -> Address -> Generation -> Commit Journal -> Recovery
```

### Layer 1: Geometry
- Truncated octahedron (14 faces, 24 vertices, 36 edges)
- Frozen: no changes to geometry layer
- Independent oracle verification (2 construction methods agree)

### Layer 2: Incidence
- Bijective mapping: TO vertex <-> (minicube_a, minicube_b, outer_face)
- Scale invariant (fractal): rule preserved at every level
- Symmetry invariant: 48 cube symmetries + label permutations

### Layer 3: Address
- Hierarchical: L0 (24) -> L1 (72) -> L2 (216) = 312 total
- LCAddress(parent_id, hex_a, hex_b, square)
- Structurally derived (reconstructable from path)
- Globally unique via parent qualification

### Layer 4: Generation
- Generation manifest (gen_001/, gen_002/, ...)
- Atomic commit: write complete gen before manifest update
- GC: never delete referenced generations

### Layer 5: Commit Journal
- LC_COMMIT_RECORD (4 fields, hash chain)
- Journal is source of truth; manifest is index
- Head integrity + chain integrity (separate, both required)

### Layer 6: Recovery
- Crash at any point -> detectable + recoverable
- Fresh process -> exact equality
- Recovered State == Committed State

## Design Decisions

1. **Geometry first**: Prove geometry before persistence
2. **Persistence second**: Prove durability before integration
3. **Integration last**: Prove end-to-end before deployment
4. **Freeze, don't modify**: Each layer frozen before next layer built
5. **Logical design**: Physical durability is separate concern

## Extension Points

- C implementation: use canonical test vectors for byte-level contract
- Additional levels: L3, L4, ... (rule preserved at every scale)
- Additional geometries: truncated octahedron is one option
- HMAC/signatures: for production security (beyond hash chain)
"""