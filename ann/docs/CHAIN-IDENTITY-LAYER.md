# ANN Chain Identity Layer — Architecture & Implementation

**Date:** 2026-11-01
**Status:** Implemented (seed identity wired into production)
**Repo:** I:\tools\ann

---

## 1. Overview

The ANN system uses a **hybrid architecture**:

- **Chain** provides identity (seed from vector → locate/dedup), counter-based random access, capo/offset storage, promotion, and composite identity.
- **Vector** provides semantic search (k-means anchors, proven 0.91 recall @ 10% scan).

Each layer does what it's good at. Chain does NOT replace k-means routing.

```
Data append
    ↓
Chain walks → seed (FNV from vector + space_id + Mix64)
    ↓
Seed jumps (counter-based: Mix64(seed ⊕ L·φ)) → capo/offset
    ↓
Vector bucket → k-means search (semantic)
```

---

## 2. Chain Headers (14 total)

### Core Identity
| Header | Purpose | Key Functions |
|---|---|---|
| `chain_seedgen.h` | 64-bit seed from vector | `seed_from_vector()`, `seed_from_vector_ex()`, `space_id_hash()`, `seed_probe()`, `seed_collision_estimate()` |
| `chain_counter.h` | Counter-based random access + capo/offset | `counter_desired_letter()`, `counter_actual_letter()`, `capo_store/remove/lookup`, `counter_walk/chain()` |
| `chain_exit.h` | 50-choice exit constraint | `chain_complement()`, `chain_exit_valid()`, `chain_skeleton_valid()` |

### Routing & Addressing
| Header | Purpose | Key Functions |
|---|---|---|
| `chain_addr.h` | ChainKey (letter sequence) | `chain_key()`, `chain_valid()`, `chain_resolve()`, `chain_extend()` |
| `chain_node.h` | 52 letters, 26 faces, major/minor | `chain_bucket_face()`, `chain_major_letter()` |
| `chain_headtail.h` | Head+tail addressing | `chain_ht_lookup()`, `chain_ht_any_depth()` |
| `chain_wsnap.h` | O(1) lookup by (letter, depth) | `wsnap_lookup/insert/delete/from_key()` |

### Promotion & Identity
| Header | Purpose | Key Functions |
|---|---|---|
| `chain_promote.h` | Deterministic 2-means split | `promote_split_2means()`, `promote_extend_address()`, `promote_needed()` |
| `chain_ident.h` | Composite identity | `ci_same_point()`, `ci_identical()`, `ci_status()` |
| `chain_bucket.h` | Bucket join (chain ↔ bucket) | `chain_bucket_face()` |

### Fixed-Frame & Multi-View (standalone, not wired to production)
| Header | Purpose | Key Functions |
|---|---|---|
| `chain_fixedanchor.h` | Fixed projection → face | `fa_anchor()`, `fa_gen_matrix()` |
| `chain_multiview.h` | Multi-view summon + consensus | `mv_summon()`, `mv_consensus()`, `mv_agreement_ratio()` |
| `chain_peano.h` | Gray code cycle (8 cubes) | `peano_next/prev/cycle`, `peano_is_hilbert()` (returns 0) |

---

## 3. Seed Identity Layer (wired into production)

### Seed Format (64-bit)
```
Field        Bits   Range      Description
major_a      5      0-23       minor letter index
level        16     0-65535    chain depth (structural)
cube_route   6      0-47      8 cubes × 6 faces
inner_ori    12     0-4095    12 twin pairs × 1 bit
outer_ori    12     0-4095    12 free faces × 1 bit
checksum     13     0-8191    collision verification
```

### Entropy
- ~47 bits effective (not 64): level(16) is structural
- Collision estimate (birthday bound, 2^47):
  - 63k rows → 1.4e-5 (≈0.0014%)
  - 1M rows → 0.36 (≈1 collision)
  - 10M rows → 355 (need open addressing)

### Space ID
- Hash of (model_name + version + dim) prevents cross-model collisions
- `space_id_hash("multilingual-e5-small", 1, 384)` = 0xdbec345f

### Collision Handling
- `seed_probe(seed, probe)`: deterministic open addressing (Mix64)
- `lz_seed_locate(seed)`: O(n) linear scan (production)

### Seed File Format
```
magic (8 bytes): "ANNSEED\0"
version (4 bytes): 1
n_rows (8 bytes): uint64
space_hash (4 bytes): uint32
seeds (n_rows × 8 bytes): uint64 array
```

---

## 4. Counter-Based Access

```
letter(L) = Mix64(seed ⊕ L·φ) mod 52
```

- O(1) access to letter at any level without walking from root
- φ = 0x9E3779B97F4A7C15 (golden ratio)
- Capo offset: stored only when displaced (offset 0 = not stored)
- `capo_store/remove/lookup`: sparse storage in CapoTable

---

## 5. Production Integration

### Build Pipeline
```
build_index.py:
  1. Read entries from SQLite
  2. Write JSONL (ann_index.jsonl)
  3. Generate seeds (ann_index.jsonl.seeds)  ← NEW
  4. Write f16 sidecar (ann_index.jsonl.f16)
```

### Server Load
```
ann_search.c:
  1. Load index (JSONL or sidecar)
  2. Load seeds (ann_index.jsonl.seeds)  ← NEW
  3. Load anchors (k-means centroids)
  4. Load order file
  5. Ready
```

### Search Path
```
Query vector
  → k-means route (anch_route) → top-5 buckets
  → scan rows in buckets (budget 24000)
  → return top-k by cosine similarity
  (seed identity available via lz_seed_of_row() for dedup)
```

---

## 6. Test Suite

- C tests: 16 suites (all PASS)
- Python tests: 19 (all PASS)
- Run: `python run_tests.py` → ALL PASS

### Test Files
| Test | Header | Coverage |
|---|---|---|
| test_chain_addr | chain_addr.h | round-trip, resolve, repair scan |
| test_chain_node | chain_node.h | 52 letters, 26 faces, major/minor |
| test_chain_bucket | chain_bucket.h | bucket join |
| test_chain_headtail | chain_headtail.h | head+tail addressing, depth |
| test_chain_ident | chain_ident.h | composite identity, merge rules |
| test_chain_exit | chain_exit.h | 50-choice, complement, skeleton |
| test_chain_wsnap | chain_wsnap.h | O(1) lookup, insert, delete |
| test_chain_promote | chain_promote.h | 2-means split, chain_extend |
| test_chain_fixedanchor | chain_fixedanchor.h | deterministic, multi-seed |
| test_chain_multiview | chain_multiview.h | summon, consensus, agreement |
| test_chain_peano | chain_peano.h | cycle, next/prev, shortcuts |
| test_chain_seedgen | chain_seedgen.h | pack/unpack, hash, space_id |
| test_chain_counter | chain_counter.h | counter access, capo store/remove |

---

## 7. Benchmark Results

### Routing (54k rows, 20 queries, brute-force oracle)
| Method | Recall@10 | Scan |
|---|---|---|
| k-means (K=56, top-5) | 0.91 | 10.6% |
| Chain 1 view (random proj) | 0.64 | 31.4% |
| Chain 2 views | 0.905 | 82.4% |
| Chain 4 views | 1.0 | 99.7% |

**Conclusion**: k-means wins for routing. Chain is identity layer, not router.

### Seed Identity
- 54,064 seeds generated, space_hash=0xdbec345f
- Loaded in production, log confirms
- Dedup: 372 exact duplicates found in 54k rows

---

## 8. What's NOT Implemented (Future Work)

1. **Counter-based access in search path** — seed → letter → bucket jump (currently uses k-means)
2. **Capo/offset persistence** — currently in-memory only (CapoTable)
3. **Multi-view in production** — currently single k-means probe
4. **Promotion in regrow path** — capacity check + 2-means + chain_extend (standalone module exists)
5. **LSH projection** — replace random projection with locality-preserving hash
6. **Recursive subdivision** — Hilbert curve (currently Gray code, 1 level)

---

## 9. Key Decisions

1. **Hybrid architecture**: chain = identity, vector = semantic search
2. **N=52**: natural mapping (26 pairs = A-Z), geometry (26 faces), digital root 7
3. **No antipodal jump**: tested, doesn't help (k-means centroids aren't antipodal)
4. **No migration to 1024-dim**: multilingual-e5-small (384) sufficient for 54k rows
5. **Seed identity**: FNV from vector + space_id + Mix64 (47-bit entropy)
6. **Capo/offset**: sparse storage (only non-zero offsets)
7. **Promotion**: deterministic 2-means, prefix-stable (chain_extend)
PY