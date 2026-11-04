# LC TileCube Specification v1.0

## 1. Geometry

### Truncated Octahedron
- 14 faces: 8 regular hexagons + 6 squares
- 24 vertices, 36 edges
- Euler: V - E + F = 24 - 36 + 14 = 2
- 4 antipodal hexagon pairs: (0,7), (1,6), (2,5), (3,4)

### Vertex Incidence
Each vertex touches 2 hexagons + 1 square (3-face junction).
24 vertices = 8 cube vertices x 3 (truncation splits each vertex).

### Mapping
- hex (8) <-> minicube (8): adjacency graph matches 2x2x2 cube
- square (6) <-> outer face (6): 4 minicubes per face
- vertex (2H+1S): 24 unique connections, bijective

## 2. Incidence

### Properties
- Bijective: TO vertex <-> (minicube_a, minicube_b, outer_face)
- No collision: 24 unique combos
- No duplicate: 24 unique connections
- Round-trip identity: vertex -> connection -> vertex

### Scale Invariance
- Rule preserved at every scale (fractal)
- 3 junctions per sub-cube (constant ratio)
- hex-to-minicube: 8->8, square-to-face: 6->6, 2H+1S: 24 per scale

## 3. Address

### LCAddress Structure
```python
class LCAddress:
    parent_id: int       # structural (path-derived, not insertion-order)
    hex_a: int           # 0-7
    hex_b: int           # 0-7
    square: int          # 0-5
```

### Hierarchy
- L0: 24 addresses (root, parent_id=-1)
- L1: 72 addresses (3 children per L0 parent)
- L2: 216 addresses (3 children per L1 parent)
- Total: 312 addresses

### Properties
- Globally unique via parent qualification
- Structurally derived (reconstructable from path)
- Ingest order independent
- Process restart: reverse lookup complete
- Parent isolation: add/remove one set doesn't affect others

## 4. Generation

### Generation Manifest
```
LC_STORE/
  generations/
    gen_001/
      data
      checksum
    gen_002/
      data
      checksum
  manifest        (current_gen + generation metadata)
  manifest.prev   (rollback point)
```

### Commit Protocol
1. Write new generation completely (data + checksum) to temp
2. fsync temp files
3. Backup current manifest -> manifest.prev
4. Write new manifest to temp
5. fsync manifest temp
6. Atomic rename manifest temp -> manifest
7. fsync directory

### Recovery
- Journal is source of truth; manifest is index
- If manifest corrupt -> recover from manifest.prev
- If both corrupt -> full history scan (scan all gens, pick latest valid)
- Orphan generations (written but not committed) -> not selected
- Never auto-select highest gen ID (committed only)

### Garbage Collection
- Never delete current generation
- Never delete manifest-referenced generations
- Keep last N generations (configurable)
- Protected: current + last N + all manifest-referenced

## 5. Commit Journal

### LC_COMMIT_RECORD
```c
typedef struct {
    uint64_t generation_id;
    uint64_t sequence;
    uint8_t  data_checksum[32];        // 64 hex chars
    uint8_t  previous_record_hash[32];  // 64 hex chars
} LC_COMMIT_RECORD;
```

### record_hash
```
record_hash = SHA256(canonical_serialize(all 4 fields))
canonical = json.dumps({"data_checksum":..., "generation_id":..., "previous_record_hash":..., "sequence":...}, sort_keys=True, separators=(",",":"))
encoding: UTF-8 (ASCII subset)
```

### Chain Integrity
- record[n].previous_record_hash == record[n-1].record_hash
- Chain break -> stop (don't skip)

### Head Integrity
- record[last].data_checksum == actual_generation_checksum
- Direct comparison (no Record N+1 needed)

### Commit Authority
- Journal is source of truth
- Manifest is index (rebuildable from journal)
- If journal and manifest disagree -> journal wins

## 6. Recovery

### Properties
- Crash at any point -> detectable
- Crash before journal fsync -> old generation
- Crash after journal fsync -> new generation
- Fresh process -> reload from disk -> exact equality
- Recovered State == Committed State

### Failure Injection
- Case A: crash before journal fsync -> old generation
- Case B: crash after journal fsync -> new generation

## 7. Canonical Test Vectors

### Vector 1 (Genesis)
- Input: gen=1, seq=1, cs=0*64, prev=0*64
- Serialized: {"data_checksum":"000...0","generation_id":1,"previous_record_hash":"000...0","sequence":1}
- Hash: f8724e3939fc6e002c9f51dfc7ab960361f8c68a4d5179cd311b51c88b88c216

### Vector 2 (Chained)
- Input: gen=2, seq=2, cs=a*64, prev=v1_hash
- Serialized: {"data_checksum":"aaa...a","generation_id":2,"previous_record_hash":"f872...216","sequence":2}
- Hash: 150e5dc917359c66f6daa123357608a4168c358aa83be812ebdaab96d46dc34d

### Vector 3 (High Values)
- Input: gen=999, seq=42, cs=f*64, prev=e*64
- Serialized: {"data_checksum":"fff...f","generation_id":999,"previous_record_hash":"eee...e","sequence":42}
- Hash: 1b3f106beb0c0977b8b90d322adb5a1e446b5eeb4ce6b934acd698840d293eca

## 8. Limitations

1. Physical durability: process-level crash simulation, not power-loss
2. Hash chain security: detects accidental corruption, not targeted attacks
3. Performance: Python prototype, C recommended for hot-path
4. Scale: tested up to 3 levels (312 addresses)
5. uint64: Python handles natively; C must use uint64_t carefully
"""