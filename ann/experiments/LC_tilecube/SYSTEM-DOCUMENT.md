# Snake – Graft – Fruit – Grape Cluster
## Complete System Documentation

**Version:** 1.0 (Frozen Baseline)
**Date:** 2026-11-03
**Repository:** I:\tools\ann\experiments\LC_tilecube
**Language:** Python 3 (prototype), C (planned production)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Module Reference](#3-module-reference)
4. [Junction Contract](#4-junction-contract)
5. [Direction Transition Contract](#5-direction-transition-contract)
6. [Compact Identity](#6-compact-identity)
7. [Performance Characteristics](#7-performance-characteristics)
8. [Test Suite](#8-test-suite)
9. [Commit History](#9-commit-history)
10. [Glossary](#10-glossary)

---

## 1. System Overview

### 1.1 Purpose

A modular geometric growth system that can:
- **Grow** structures (Snake) in 6 directions in 3D
- **Branch** into multiple independent paths
- **Cut** segments and transform them into layered objects (Fruit/Onion)
- **Graft** segments between different structures
- **Preserve identity** through all operations
- **Save/Restore** entire networks losslessly
- **Reduce memory** via compact identity encoding

### 1.2 Core Concepts

| Concept | Description |
|---|---|
| **Cube** | Atomic unit. Has 3 axes, each with 4 letters + 1 frame (3 × (4L+1F)) |
| **Snake** | Linear chain of cubes growing in 6 directions (±X, ±Y, ±Z) |
| **Junction** | Adjacent cubes sharing a frame on the diff axis (connectivity) |
| **Branch** | Independent Snake starting from a position on another Snake |
| **Fruit** | Cut segment transformed into onion-layer growth mode |
| **Graft** | Cut segment from one Snake, connect to another (with interface check) |
| **Grape Cluster** | Network of Snakes + Fruits with independent identities |
| **Domain** | 3D grid boundary (domain_size³) |
| **Compact Identity** | String encoding of letters (9.3x/17.7x memory reduction) |

### 1.3 Design Principles

1. **One Parameter → One Hypothesis → One Test → One Decision**
2. **Modular:** Each module has separate tests and contracts
3. **No premature optimization:** Baseline first, then optimize
4. **Identity preservation:** No operation may destroy object identity
5. **Lossless persistence:** Save/Restore must produce identical structures
6. **Fail-loud:** Reject + rollback on any mismatch, never silent corruption

---

## 2. Architecture

### 2.1 Module Stack

```
┌─────────────────────────────────────────────────┐
│  grape_cluster (network of snakes + fruits)     │
├─────────────────────────────────────────────────┤
│  graft_engine (cut + reconnect)                 │
├─────────────────────────────────────────────────┤
│  fruit_core (cut → fruit) + onion_growth        │
├─────────────────────────────────────────────────┤
│  snake_branch (independent branches)            │
├─────────────────────────────────────────────────┤
│  snake_core (growth + junctions + identity)     │
├─────────────────────────────────────────────────┤
│  identity_manager (compact encoding)            │
├─────────────────────────────────────────────────┤
│  boundary_manager (domain enforcement)          │
├─────────────────────────────────────────────────┤
│  persistence (save/restore)                     │
├─────────────────────────────────────────────────┤
│  verification (2 validators)                    │
└─────────────────────────────────────────────────┘
```

### 2.2 Data Flow

```
Grow:     seed → _gen_cube() → positions[] + letters{} + visited{}
Cut:      positions[start:end] → Fruit (new identity, onion mode)
Graft:    cut src → check interface → shift → check connection → append to dst (or rollback)
Save:     state_dict() → JSON (positions, letters, visited, directions, domain_size, ...)
Restore:  JSON → positions, letters (decode), visited, directions → continue growth
Compact:  letters (tuples) → encode_string() → 15-char string → decode_string() → letters
```

### 2.3 Cube Structure

Each cube has 3 axes (X, Y, Z). Each axis has:
- 4 letters (A-Z) — inner data / identity
- 1 frame (a-h) — junction key / connectivity

Total per cube: 3 × (4 letters + 1 frame) = 15 characters

Example:
```
Axis 0 (X): (('X','Z','F','O'), 'e')
Axis 1 (Y): (('N','Y','S','F'), 'a')
Axis 2 (Z): (('Y','S','U','L'), 'f')
```

### 2.4 Letter Generation

Letters are generated deterministically:
```
seed_rng(seed, f"c{pos}|{axis}") → random.Random
letters = [rng.choice("A-Z") for _ in range(4)]
frame = rng.choice("abcdefgh")
```

Growth inheritance: when growing along axis K, the new cube inherits the K-axis letter from the previous cube. Other axes are independently generated.

---

## 3. Module Reference

### 3.1 snake_core (snake_sim.py)

**File:** `snake_sim.py`
**Class:** `Snake`

**Constructor:**
```python
Snake(seed, mode='chain', domain_size=20, start_pos=None)
```

**Public Methods:**

| Method | Signature | Description |
|---|---|---|
| `grow` | `grow(direction) → (bool, str)` | Grow one cube in direction (0-5). Returns (success, reason) |
| `check_junctions` | `check_junctions() → (ok, tot)` | Geometric junction (5-pair): full tuple equality |
| `check_graft_interface` | `check_graft_interface() → (ok, tot)` | Graft interface (1-pair): frame equality only |
| `state_dict` | `state_dict() → dict` | Serialize to JSON-compatible dict |
| `restore` | `restore(state)` | Deserialize from dict |

**Directions:**
| Index | Direction | Axis |
|---|---|---|
| 0 | +X | 0 |
| 1 | -X | 0 |
| 2 | +Y | 1 |
| 3 | -Y | 1 |
| 4 | +Z | 2 |
| 5 | -Z | 2 |

**Invariants:**
- `len(visited) == len(positions)` always
- `growth_count == len(positions)` always
- `current_domain` consistent with domain_size
- Letters generated deterministically from (seed, position, axis)

**Key Functions:**
- `domain_of(pos, size) → (x//size, y//size, z//size)`
- `row_key(axis, pos) → tuple` — row identifier for junction grouping
- `seed_rng(seed, salt) → random.Random` — deterministic RNG

### 3.2 fruit_core (fruit_graft.py — Fruit class)

**File:** `fruit_graft.py`
**Class:** `Fruit`

**Constructor:**
```python
Fruit(origin_snake_id, origin_positions, seed, domain_size)
```

**Public Methods:**

| Method | Signature | Description |
|---|---|---|
| `layer_grow` | `layer_grow(layer_num) → int` | Grow one onion layer. Returns new cube count |
| `identity` | `identity() → tuple` | Unique identity tuple |
| `check_junctions` | N/A | Not applicable (onion mode) |

**Attributes:**
- `fruit_id`: unique integer
- `origin_snake_id`: seed of origin snake
- `origin_positions`: list of positions cut from snake
- `seed`: deterministic seed
- `domain_size`: domain boundary
- `positions`: all positions (origin + layers)
- `visited`: set of all positions
- `layers`: dict (layer_num → set of positions)
- `current_layer`: highest layer grown
- `growth_mode`: always 'onion'
- `letters`: dict (pos → 3-axis letters)
- `merged`: always False (fruits cannot merge)

**Invariants:**
- `growth_mode == 'onion'` always
- Layers do not overlap (no position in multiple layers)
- Origin positions preserved
- Fruit cannot merge (no merge method)
- Fruit cannot be grafted into (type mismatch)

### 3.3 onion_growth (fruit_graft.py — FruitFormation)

**Class:** `FruitFormation` (static methods)

| Method | Signature | Description |
|---|---|---|
| `cut_and_remove` | `cut_and_remove(snake, start, end, seed) → (Fruit, bool)` | Cut segment from snake, create fruit, remove from snake |
| `cut_fruit` | `cut_fruit(snake, start, end, seed) → (Fruit, positions)` | Cut segment, create fruit, snake keeps positions |

**Rules:**
- Cut segment becomes fruit with new identity
- Snake's remaining positions preserved
- Origin reference stored in fruit
- Fruit letters generated from seed + position

### 3.4 graft_engine (fruit_graft.py — GraftEngine)

**Class:** `GraftEngine` (static methods)

| Method | Signature | Description |
|---|---|---|
| `graft` | `graft(src, cut_idx, dst, attach_pos, interface_check) → GraftResult` | Cut src at cut_idx, move right segment to dst |
| `cut_snake` | `cut_snake(snake, idx) → (left, right)` | Split snake at idx |
| `check_interface` | `check_interface(pos_a, snake_a, pos_b, snake_b) → (bool, str)` | Check frame compatibility |

**GraftResult:**
- `success`: bool
- `reason`: str ('ok', 'empty_segment', 'no_letter_match', 'domain_mismatch', 'collision_with_dst', 'connection_junction_mismatch', 'no_placement')
- `moved_positions`: list of moved positions

**Rules:**
- Interface must match (frame compatibility) before graft
- Connection junction must pass (frame match) or rollback
- On rejection: full state restoration (no partial changes)
- Moved segment letters preserved from source
- Collision detection prevents overlap

### 3.5 identity_manager (compact_identity.py)

**Encodings:**

| Scheme | Storage (B/cube) | Object (B/cube) | Encode (μs/cube) | Decode (μs/cube) |
|---|---|---|---|---|
| Baseline (tuples) | 265.2 | 265.2 | 0 | 0 |
| String | 15.0 | 28.6 | 0.48 | 1.02 |
| Bytes | 24.5 | 24.5 | 1.14 | 2.09 |
| Int | 18.4 | 18.4 | 1.52 | 2.26 |

**Functions:**
- `encode_string(letters) → str` — 15-char string (5 per axis: 4 letters + 1 frame)
- `decode_string(s) → list` — decode back to tuple format
- `encode_bytes(letters) → bytes` — 15 bytes
- `decode_bytes(b) → list` — decode back
- `encode_int(letters) → int` — single integer
- `decode_int(val) → list` — decode back

**Recommendation:** String encoding (9.3x object / 17.7x storage reduction, fastest encode/decode)

### 3.6 boundary_manager (snake_sim.py)

**Function:** `domain_of(pos, domain_size) → (x//size, y//size, z//size)`

**Rules:**
- Growth blocked when new position is outside current domain
- `boundary_blocks` counter tracks violations
- Domain is a 3D grid of `domain_size³` cubes

### 3.7 persistence (snake_sim.py)

**Format:** `state_dict()` returns JSON-serializable dict:
```json
{
  "seed": "string",
  "mode": "chain",
  "domain_size": 200,
  "positions": [[x,y,z], ...],
  "letters": {"x,y,z": [[[L,L,L,L], "f"], ...]},
  "visited": [[x,y,z], ...],
  "current_domain": [0,0,0],
  "growth_count": 21,
  "directions": [null, 0, 0, ...],
  "boundary_blocks": 0
}
```

**Rules:**
- `domain_size` must be preserved (added in baseline v1.0)
- Compact (string) or raw (tuple) letter encoding supported
- Restore must preserve all junctions
- Growth must continue after restore

### 3.8 verification (junction_contract.py)

**Two Validators:**

| Validator | Pairs Checked | Criterion | Use Case |
|---|---|---|---|
| `check_geometric_junction()` | 5 (4 mini-cube + 1 frame) | Full tuple equality (ra == rb) | Pure snake growth, branch body |
| `check_graft_interface()` | 1 (frame only) | Frame equality (ra[1] == rb[1]) | Graft boundaries, branch points |

**Key Insight:** Geometric is stricter (subset of graft). Frame-only is necessary for graft to succeed because inner letters at connection points come from different seeds.

---

## 4. Junction Contract

### 4.1 The Two Validators

```
check_geometric_junction(snake) → (ok, tot)
  - Checks: ra == rb (full tuple: 4 letters + frame)
  - Use for: pure snake growth, branch body, straight segments

check_graft_interface(snake) → (ok, tot)
  - Checks: ra[1] == rb[1] (frame only)
  - Use for: graft boundaries, branch points, turn points (if needed)
```

### 4.2 When to Use Which

| Scenario | Validator | Reason |
|---|---|---|
| Straight growth | Geometric | Same-axis inheritance ensures full match |
| Turn (direction change) | Geometric | New axis inherited from turn point |
| Branch body | Geometric | Same seed, same-axis inheritance |
| Branch point (connection) | Graft interface | Different seed, frame compatibility only |
| Graft (between objects) | Graft interface | Different seed, frame compatibility only |

### 4.3 Key Finding

**Direction and Connectivity are separate concerns.** Changing direction does not require a new validator because the inheritance mechanism handles axis transition. The new growth axis inherits from the turn point, ensuring geometric junction passes.

---

## 5. Direction Transition Contract

### 5.1 Three Scenarios

| Scenario | Geometric | Graft | Contract |
|---|---|---|---|
| STRAIGHT | 30/30 ✅ | 30/30 ✅ | Use geometric — all inherited |
| TURN | 30/30 ✅ | 30/30 ✅ | Use geometric — inheritance handles turn |
| BRANCH | 10/10 body ✅ | 10/10 body ✅ | Body: geometric. Point: graft interface |

### 5.2 Combined Test (STRAIGHT → TURN → BRANCH → GRAFT → FRUIT)

All 8 checks pass:
1. Straight: geo=20/20, graft=20/20 ✅
2. Turn: geo=35/35, graft=35/35 ✅
3. Branch body: geo=12/12 ✅
4. Branch point: frame_match=True ✅
5. Graft: graft=44/44 ✅
6. Fruit: onion mode, no overlap, identity ✅
7. Persistence: positions=True, letters=True ✅
8. Identity: 3 distinct objects ✅

---

## 6. Compact Identity

### 6.1 Problem

Letters use 265.2 B/cube (69% of total memory). This is the primary optimization target.

### 6.2 Solution

Encode 3 × (4 letters + 1 frame) into a 15-character string:
- Each axis: 4 letters + 1 frame = 5 chars
- Total: 15 chars per cube

### 6.3 Results

| Metric | Baseline | Compact (String) | Reduction |
|---|---|---|---|
| Storage (B/cube) | 265.2 | 15.0 | 17.7x |
| Object (B/cube) | 265.2 | 28.6 | 9.3x |
| Encode time | 0 | 0.48 μs/cube | — |
| Decode time | 0 | 1.02 μs/cube | — |
| Restore time | 5.51 ms (5K) | 6.92 ms (5K) | 1.25x slower |

### 6.4 Lossless Verification

- 1000 random test cases: all pass (string, bytes, int)
- Encode → Decode round-trip: identity preserved
- Integration test: Snake, Fruit, Graft, Save/Restore all pass

### 6.5 Trade-off

Compact identity reduces memory 9.3x (in-memory) / 17.7x (serialized) but increases restore time by 1.25x due to decode overhead. For production C implementation, overhead should be minimal.

---

## 7. Performance Characteristics

### 7.1 Growth

| Scale | Time | Per Cube | Junctions |
|---|---|---|---|
| 1K | 0.025s | 0.0447 ms | 549/549 |
| 10K | 0.235s | 0.0466 ms | 5,049/5,049 |
| 100K | 2.541s | 0.0508 ms | 50,049/50,049 |

Growth is O(1) per cube. Memory: 1,493 bytes/cube (Python).

### 7.2 Graft

| Segment Size | Time | Per Cube | Junctions |
|---|---|---|---|
| 5 | 0.049 ms | 0.0099 ms | 53/54 |
| 50 | 0.081 ms | 0.0016 ms | 98/99 |
| 500 | 0.556 ms | 0.0011 ms | 548/549 |

Graft is O(n) per segment. Per-cube cost decreases with scale (fixed overhead amortized).

### 7.3 Restore

| n | Replay | Letters | Junction | Index | Total |
|---|---|---|---|---|---|
| 100 | 0.02 | 0.17 | 0.06 | 0.01 | 0.27 ms |
| 1K | 0.07 | 0.99 | 0.40 | 0.08 | 1.54 ms |
| 5K | 0.37 | 4.34 | 1.87 | 0.47 | 7.06 ms |
| 10K | 1.31 | 12.92 | 3.82 | 0.71 | 18.76 ms |

Letter restoration dominates (69% of total). Optimization target for C implementation.

### 7.4 Memory (per cube)

| Component | B/cube | % of Total |
|---|---|---|
| Letters (identity) | 280.0 | 69.0% |
| Positions (geometry) | 40.8 | 10.0% |
| Domains | 40.8 | 10.0% |
| Visited (boundary) | 26.3 | 6.5% |
| Directions | 18.4 | 4.5% |
| Object overhead | 0.01 | 0.0% |
| **Total (exclusive)** | **406.3** | **100%** |

### 7.5 Long-run Stress

| Test | Scale | Result |
|---|---|---|
| Randomized growth | 2,283 cubes | ✅ |
| Heavy branching | 100 branches, 1,100 cubes | ✅ |
| Large grape network | 49 objects, 1,079 cubes | ✅ |
| Save/Restore | 0.02s for 49 objects | ✅ |
| Identity preservation | All objects | ✅ |

---

## 8. Test Suite

### 8.1 Test Files

| File | Tests | Status |
|---|---|---|
| `snake_perf.py` | Growth cost, junction cost, multi-snake, grape cluster | ✅ |
| `fruit_graft.py` | Fruit formation, graft, isolation, persistence | ✅ |
| `e2e_lifecycle.py` | Full lifecycle (snake→branch→cut→fruit→graft→save→restore→grow) | ✅ |
| `graft_verify_prof.py` | Graft scaling, restore profiling, memory profiling, junction verification | ✅ |
| `reconcile.py` | Memory reconciliation, restore timing, graft valid/invalid, identity lossless | ✅ |
| `compact_identity.py` | Lossless verification, memory comparison, encode/decode, restore | ✅ |
| `successful_graft.py` | Graft scaling (5→5000) | ✅ |
| `string_restore.py` | String restore benchmark (1K/5K/10K) | ✅ |
| `graft_audit.py` | Graft junction audit (5/50/250/500/2500) | ✅ |
| `junction_contract.py` | Geometric vs graft interface contract | ✅ |
| `compact_integration.py` | Compact identity in snake/fruit/graft/save/restore E2E | ✅ |
| `stress_test.py` | 6 structural stress tests | ✅ |
| `longrun_stress.py` | Randomized 10K, 100 branches, 49-object grape network | ✅ |
| `direction_transition.py` | Direction transition contract (straight/turn/branch) | ✅ |
| `regression_suite.py` | STRAIGHT→TURN→BRANCH→GRAFT→FRUIT regression | ✅ |

### 8.2 Test Counts

- Total test files: 15
- Total test cases: 50+
- All passing: Yes

---

## 9. Commit History

| # | Commit | Description |
|---|---|---|
| 1 | e5bebfc | snake_sim.py (snake growth + junctions) |
| 2 | 8008db4 | snake_scale_cost.py (growth cost, domain expansion, multi-snake) |
| 3 | 795a7c3 | fruit_graft.py (fruit formation + graft + grape + persistence) |
| 4 | 77124ce | snake_perf.py (performance benchmark) |
| 5 | 73a2ba2 | e2e_lifecycle.py (end-to-end lifecycle) |
| 6 | cf12798 | graft_verify_prof.py (graft junction verification + rollback) |
| 7 | 123ec9e | reconcile.py (benchmark reconciliation) |
| 8 | b527736 | compact_identity.py + successful_graft.py |
| 9 | f3a6334 | graft_audit.py + string_restore.py + frame-only junction check |
| 10 | f03020a | junction_contract.py + compact_integration.py |
| 11 | 9200c4a | stress_test.py + domain_size fix |
| 12 | 3879551 | longrun_stress.py (randomized 10K, 100 branches, 49-object grape) |
| 13 | 8500823 | direction_transition.py (direction transition contract) |
| 14 | 4f32f14 | regression_suite.py + baseline freeze |

---

## 10. Glossary

| Term | Definition |
|---|---|
| **Cube** | Atomic unit with 3 axes, each containing 4 letters + 1 frame |
| **Snake** | Linear chain of cubes growing in 6 directions |
| **Junction** | Connectivity between adjacent cubes via frame match on diff axis |
| **Geometric Junction** | Full tuple equality (5-pair): 4 letters + frame must match |
| **Graft Interface** | Frame equality only (1-pair): only frame must match |
| **Branch** | Independent Snake starting from a position on another Snake |
| **Fruit** | Cut segment transformed into onion-layer growth mode |
| **Onion Growth** | Layered growth radiating outward from origin |
| **Graft** | Cut segment from source Snake, connect to destination Snake |
| **Grape Cluster** | Network of Snakes + Fruits with independent identities |
| **Domain** | 3D grid boundary (domain_size³ cubes) |
| **Compact Identity** | String encoding of letters (9.3x/17.7x memory reduction) |
| **Identity** | Unique identifier for an object (seed, position, origin) |
| **Connectivity** | Junction-based linkage between cubes |
| **Rollback** | Full state restoration on graft rejection |
| **Frame** | Junction key (a-h) on each axis |
| **Inner Letters** | 4-letter data on each axis (A-Z) |
| **Inheritance** | Growth axis letter copied from previous cube |
| **Origin Reference** | Historical link from Fruit to source Snake |

---

## Appendix: File List

```
I:\tools\ann\experiments\LC_tilecube\
├── snake_sim.py                    # Snake core: growth, junctions, save/restore
├── fruit_graft.py                  # Fruit formation, graft engine, isolation
├── compact_identity.py             # Compact encoding (string/bytes/int)
├── junction_contract.py            # Two validators: geometric vs graft
├── direction_transition.py         # Direction transition contract
├── regression_suite.py             # Combined regression test
├── ARCHITECTURE-BASELINE.md        # Baseline freeze document
├── SYSTEM-DOCUMENT.md              # This document
├── snake_perf.py                   # Performance benchmarks
├── e2e_lifecycle.py                # End-to-end lifecycle test
├── graft_verify_prof.py            # Graft verification + profiling
├── reconcile.py                    # Benchmark reconciliation
├── successful_graft.py             # Graft scaling tests
├── string_restore.py               # String restore benchmark
├── graft_audit.py                  # Graft junction audit
├── compact_integration.py          # Compact identity E2E
├── stress_test.py                  # 6 structural stress tests
├── longrun_stress.py               # Long-run stress (randomized, branching, grape)
└── lettercube_rhombi.h             # C header (rhombi pattern)
```

---

*Document generated: 2026-11-03*
*Baseline version: 1.0 (Frozen)*
*Total commits: 14*
*Total test files: 15*
*Total test cases: 50+*
*All tests passing: Yes*
"