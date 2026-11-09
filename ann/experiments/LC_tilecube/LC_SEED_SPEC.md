# LC Seed Generator — Spec v1.1 (corrected 2026-11-09)

## Sacred numbers (integer-only, no float, no math.h)
- 27 trits × 32 bits = 864 base seeds
- 864 × 24 = 20736 (Grand Field)
- 972 = Temporal Ring (rewind buffer slots)
- 3456 = Geomatrix

## Seed formula (Task 1)
```
trit = get_trit_from_angular_map(vec) % 27   // 3 ternary digits
bit  = get_bit_from_bitboard(vec) % 32        // 5 binary bits
base = (trit << 5) | bit                      // 0..863
```

## Multi-dim expansion (corrected)
**Handoff v1.0 formula `base * dim` is WRONG** — measured 47181/100000
cross-dim collisions (384D vs 128D) because ranges overlap.

**Canonical (v1.1):** `seed = base | (dim_id << 10)` where `dim_id` is a small
per-(model,dim) index. Zero collision guaranteed because 864 < 1024.
Implementation: `expand_tag()` in lc_seed_gen.c; Python equivalent in
test_lc_seed_multidim.py.

## Distance
XOR Hamming via `__builtin_popcount(a ^ b)` — 1-2 CPU cycles, preserves locality.
No SHA-256/avalanche hashing.

## Stability (Task 3)
LC seed address is deterministic (100/100 rounds identical) vs k-means
pca24 which changes every rebuild. Verified: test_lc_seed_stability.py.

## Snake/Fruit integration (Task 4)
Rewind buffer = 972 slots. `slot = seed % 972` + open addressing.
864 seeds → 0 collisions, fill 88.9%, full retrievability.
Implementation: lc_seed_snake_fruit.py.

## Hybrid (LC + IVF)
- lc_hybrid_router.py: HybridRouter class (fit/query/topk/rebuild_ivf/lc_identity_stable)
- LC seed = primary key (deterministic, never changes)
- IVF = derived cache (rebuildable, addresses change, data doesn't move)
- modes: lc (cheap/deterministic), ivf (recall), hybrid (union, best recall)
- Verified: hybrid recall 0.913 vs lc 0.270 vs ivf 0.887; LC identity stable across rebuild

## Multi-dim Hybrid + Rewind
- lc_multidim_hybrid.py: MultiDimHybridRouter (per-dim LC seeds with dim tag + per-dim IVF
  + shared face grid + rewind buffer + letter-embedding)
- dim tag: base | (dim_id << 10) -> zero cross-dim collisions (verified)
- rewind: seen(face) O(1) across dims; letter_embed(face) for cross-modal bridge
- Verified: 384D hybrid 0.800, 128D hybrid 0.905; cross-dim collisions 0

## Files
- lc_seed_gen.c / lc_seed_gen.exe (C, selftest/bench/multidim/gen/hamming)
- lc_hybrid_router.py (HybridRouter)
- lc_rewind_buffer.py (rewind buffer, seen() O(1))
- lc_letter_embed.py (deterministic letter-embedding, cross-modal bridge)
- test_lc_seed_multidim.py
- test_lc_seed_stability.py
- lc_seed_snake_fruit.py
- LC_SEED_SPEC.md (this file)
