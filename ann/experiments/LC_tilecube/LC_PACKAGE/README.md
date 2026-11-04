# LC TileCube — Portable Module Package

**Version:** 1.0 (Logical Design)
**Status:** Frozen — Geometry Layer v1.0 + LC Persistence Architecture v1.0

## What is LC TileCube?

LC TileCube is a geometric addressing and persistence system based on
truncated octahedron topology. It provides:

- **Geometry**: Truncated octahedron (14 faces: 8 hex + 6 square, 24 vertices)
- **Incidence**: Bijective mapping between geometry and data addresses
- **Address**: Hierarchical 3-level addressing (24 -> 72 -> 216 -> 312 addresses)
- **Persistence**: Generation-based commit with journal evidence
- **Recovery**: Crash-safe recovery with exact equality guarantee

## Module Structure

```
LC_PACKAGE/
  core/
    scale_incidence.py       — Incidence table (24 vertices, hex/square mapping)
    independent_oracle.py    — Independent geometry construction (verification)
    symmetry_invariance.py   — Symmetry + label permutation + recursive tests
    recursive_address.py     — Hierarchical addressing (LCAddress)
  persistence/
    persistence_reconstruction.py — Disk round-trip, order independence
    atomic_persistence.py         — Atomic write, checksum, recovery
    crash_point_test.py           — Crash at every lifecycle point
    gen_manifest.py               — Generation manifest backend
    manifest_self_recovery.py     — Dual-manifest rollback
    commit_fault_injection.py     — Fault injection at commit points
    commit_evidence.py            — Commit journal as source of truth
    journal_durability.py         — Journal fsync, torn records, corruption
    journal_head_integrity.py     — Last-record tampering, canonical serialization
    canonical_test_vectors.py     — Cross-implementation byte contract
  tests/
    end_to_end.py            — Full integration test (ALL PASS)
    test_ingest_fingerprint.py — Ingestion fingerprinting
    test_fruit_dim.py         — Per-fruit dimension + adapter
    test_ingest_dim.py        — Ingest + dimension integration
  docs/
    (see below)
```

## Quick Start

### Run all tests
```bash
cd LC_PACKAGE
python tests/end_to_end.py
```

### Run individual module tests
```bash
python core/scale_incidence.py
python persistence/journal_head_integrity.py
python persistence/canonical_test_vectors.py
```

### Requirements
- Python 3.8+
- No external dependencies (stdlib only)

## Core Concepts

### 1. Geometry (Truncated Octahedron)
- 14 faces: 8 hexagons + 6 squares
- 24 vertices, 36 edges
- Euler: V - E + F = 24 - 36 + 14 = 2
- 4 antipodal hex pairs (dual-rail checkpoints)

### 2. Incidence (Bijective Mapping)
- hex (8) <-> minicube (8): adjacency graph matches
- square (6) <-> outer face (6): 4 minicubes each
- vertex (2H+1S): 24 unique connections, no ambiguity
- round-trip: bijective, no loss, no collision

### 3. Address (3-Level Hierarchy)
- L0: 24 addresses (root)
- L1: 72 addresses (3 children per parent)
- L2: 216 addresses (3 children per parent)
- Total: 312 addresses
- LCAddress(parent_id, hex_a, hex_b, square) — globally unique

### 4. Persistence (Generation Manifest + Commit Journal)
- Generation: complete data + checksum written before commit
- Commit Journal: LC_COMMIT_RECORD with hash chain
- Recovery: journal is source of truth, manifest is index
- Crash-safe: detectable at every point, recoverable

### 5. Canonical Commit Record
```c
typedef struct {
    uint64_t generation_id;
    uint64_t sequence;
    uint8_t  data_checksum[32];        // 64 hex chars
    uint8_t  previous_record_hash[32];  // 64 hex chars
} LC_COMMIT_RECORD;
```
- record_hash = SHA256(canonical_serialize(all 4 fields))
- canonical = json.dumps(sort_keys=True, separators=(",",":"))
- encoding: UTF-8 (ASCII subset)
- hash: 64 lowercase hex chars

## Integration with Other Systems

### Using as a library
```python
import sys
sys.path.insert(0, 'LC_PACKAGE/core')
sys.path.insert(0, 'LC_PACKAGE/persistence')

from scale_incidence import REAL_VERTICES
from recursive_address import StructuralAddress, compose_structural
from gen_manifest import GenManifestStore

# Build addresses
level0 = [StructuralAddress((), h1, h2, sq) for h1, h2, sq in REAL_VERTICES]
level1 = compose_structural(level0)
level2 = compose_structural(level1)
all_addrs = level0 + level1 + level2  # 312 addresses

# Persist
store = GenManifestStore('./my_data')
store.commit([{"addr": a.key(), "value": 42} for a in all_addrs])
loaded, status = store.read()
```

### Using persistence only
```python
import sys
sys.path.insert(0, 'LC_PACKAGE/persistence')
from gen_manifest import GenManifestStore
from commit_evidence import GenStoreWithJournal

store = GenStoreWithJournal('./my_data')
store.commit(1, [{"key": "value"}])
loaded, status, gen = store.recover()
```

## Test Results Summary

| Module | Tests | Status |
|---|---|---|
| Geometry (scale_incidence) | 6 | PASS |
| Independent Oracle | 8 | PASS |
| Symmetry Invariance | 3 | PASS |
| Recursive Address | 6 | PASS |
| Persistence Reconstruction | 7 | PASS |
| Atomic Persistence | 7 | PASS |
| Crash-Point Exhaustive | 4 | PASS |
| Generation Manifest | 6 | PASS |
| Manifest Self-Recovery | 7 | PASS |
| Commit Fault Injection | 4 | PASS |
| Commit Evidence | 5 | PASS |
| Journal Durability | 7 | PASS |
| Journal Head Integrity | 7 | PASS |
| Canonical Test Vectors | 7 | PASS |
| End-to-End Integration | 4 stages | PASS |
| **Total** | **88+** | **ALL PASS** |

## Limitations

1. **Physical durability**: Tests use process-level crash simulation, not physical power-loss. Filesystem fsync behavior on real hardware may differ.

2. **Hash chain security**: Hash chain detects accidental corruption but not targeted attacks (attacker can recompute hashes). For production, consider HMAC or digital signatures.

3. **Performance**: Python prototype. C implementation recommended for hot-path traversal (per original design).

4. **Scale**: Tested up to 3 levels (312 addresses). Deeper levels need verification.

## Architecture

```
Geometry (v1.0) -> Incidence -> Address -> Generation -> Commit Journal -> Recovery
```

Frozen layers:
- Geometry Layer v1.0: FROZEN
- LC Persistence Architecture v1.0: FROZEN (Logical Design)
- LC_COMMIT_RECORD Canonical Form: FROZEN

## License

Part of FS_tools project (github.com/M111ON/FS_tools).
"""