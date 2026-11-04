# LC TileCube — Integration Manual

## Overview

LC TileCube is a geometric addressing and persistence system.
This manual covers integration with external systems.

## Architecture

```
Geometry (v1.0) -> Incidence -> Address -> Generation -> Commit Journal -> Recovery
```

## Module Dependencies

```
core/scale_incidence.py          (no deps)
core/independent_oracle.py       (depends on scale_incidence)
core/symmetry_invariance.py     (depends on scale_incidence)
core/recursive_address.py        (depends on scale_incidence)
persistence/*                   (depends on core + persistence_reconstruction)
tests/end_to_end.py             (depends on all)
```

## Integration Patterns

### Pattern 1: Geometry + Address only (no persistence)
Use when you only need geometric addressing without persistence.

```python
import sys
sys.path.insert(0, 'LC_PACKAGE/core')
from scale_incidence import REAL_VERTICES
from recursive_address import StructuralAddress, compose_structural

level0 = [StructuralAddress((), h1, h2, sq) for h1, h2, sq in REAL_VERTICES]
level1 = compose_structural(level0)
level2 = compose_structural(level1)
all_addrs = level0 + level1 + level2  # 312 addresses

# Use addresses
for addr in all_addrs:
    print(addr.key(), addr.full_path())
```

### Pattern 2: With Persistence (Generation Manifest)
Use when you need crash-safe persistence.

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
all_addrs = level0 + level1 + level2

# Persist
store = GenManifestStore('./my_data')
data = [{"addr": a.key(), "value": 42} for a in all_addrs]
gen = store.commit(data)
print(f"committed gen{gen}")

# Recover (fresh process)
store2 = GenManifestStore('./my_data')
loaded, status = store2.read()
print(f"recovered {len(loaded)} items, status={status}")
```

### Pattern 3: With Commit Journal (full evidence)
Use when you need commit evidence and crash recovery.

```python
import sys
sys.path.insert(0, 'LC_PACKAGE/core')
sys.path.insert(0, 'LC_PACKAGE/persistence')
from scale_incidence import REAL_VERTICES
from recursive_address import StructuralAddress, compose_structural
from commit_evidence import GenStoreWithJournal

level0 = [StructuralAddress((), h1, h2, sq) for h1, h2, sq in REAL_VERTICES]
level1 = compose_structural(level0)
level2 = compose_structural(level1)
all_addrs = level0 + level1 + level2

store = GenStoreWithJournal('./my_data')
data = [{"addr": a.key(), "value": 42} for a in all_addrs]
store.commit(1, data)

# Recover (journal is source of truth)
loaded, status, gen = store.recover()
print(f"recovered gen{gen}, status={status}, items={len(loaded)}")
```

### Pattern 4: C Implementation (future)
The canonical test vectors provide byte-level contract for C implementation:

```c
// LC_COMMIT_RECORD
typedef struct {
    uint64_t generation_id;
    uint64_t sequence;
    uint8_t  data_checksum[32];        // 64 hex chars
    uint8_t  previous_record_hash[32];  // 64 hex chars
} LC_COMMIT_RECORD;

// canonical serialization: json.dumps(sort_keys=True, separators=(",",":"))
// record_hash = SHA256(canonical_bytes)
// hash: 64 lowercase hex chars
```

Test vectors for cross-implementation verification:
- Vector 1: hash = f8724e3939fc6e002c9f51dfc7ab960361f8c68a4d5179cd311b51c88b88c216
- Vector 2: hash = 150e5dc917359c66f6daa123357608a4168c358aa83be812ebdaab96d46dc34d
- Vector 3: hash = 1b3f106beb0c0977b8b90d322adb5a1e446b5eeb4ce6b934acd698840d293eca

## Testing

### Run all tests
```bash
cd LC_PACKAGE
python tests/end_to_end.py
```

### Run individual modules
```bash
python core/scale_incidence.py
python persistence/journal_head_integrity.py
python persistence/canonical_test_vectors.py
```

### Expected output
All tests should print "PASS" or "ALL TESTS: PASS".

## Troubleshooting

### Import errors
Ensure both core/ and persistence/ are in sys.path:
```python
import sys
sys.path.insert(0, 'LC_PACKAGE/core')
sys.path.insert(0, 'LC_PACKAGE/persistence')
```

### File not found
All modules use relative paths. Run from the directory containing LC_PACKAGE.

### Test failures
Check Python version (3.8+ required). No external dependencies.
"""