# Architecture Baseline — Snake / Graft / Fruit / Grape Cluster

**Version:** 1.0 (Frozen)
**Date:** 2026-11-03
**Status:** BASELINE — do not modify without version bump and regression test

## 1. Proven Properties (DO NOT BREAK)

These properties have been verified by tests. Any change must preserve all of them.

### P1: Growth is O(1) per cube
- `Snake.grow(direction)` cost is constant per cube
- Measured: 0.045-0.051 ms/cube (straight line, 100K cubes)
- Test: `snake_perf.py` BENCH 1

### P2: Junction continuity in pure snake growth
- Consecutive cubes on the same axis share identical (letters, frame) on that axis
- `check_geometric_junction()` returns (n-1)/(n-1) for any snake grown from a single seed
- Test: `snake_sim.py` (all growth tests)

### P3: Fruit changes growth mode (linear → onion)
- `Fruit` uses `growth_mode='onion'` (layered growth)
- Onion layers do not overlap
- Fruit has independent identity from origin snake
- Test: `fruit_graft.py` B1-B4

### P4: Graft preserves identity
- Cut segment moves to destination with original letters preserved
- Source snake identity preserved (remaining segment unchanged)
- Destination snake identity preserved (original cubes unchanged)
- Test: `fruit_graft.py` C1, C4

### P5: Graft rejects on interface mismatch
- `check_interface()` returns False when frames don't match
- Graft returns `GraftResult(False, reason)` without modifying state
- Test: `fruit_graft.py` C2

### P6: Graft rejects on connection junction mismatch
- Connection point frames must match (`la[diff_axis][1] == lb[diff_axis][1]`)
- On rejection: full rollback (positions, visited, letters, growth_count restored)
- Test: `graft_verify_prof.py` TEST 4, `graft_audit.py`

### P7: Fruit isolation (no merge, no graft into, no overlap)
- Fruit cannot be grafted into (type mismatch)
- Fruit cannot merge (no merge method)
- Fruit layers do not overlap
- Test: `fruit_graft.py` D1-D4

### P8: Persistence is lossless
- `state_dict()` → `restore()` preserves positions, letters, junctions
- Compact (string) encoding preserves identity
- Growth continues after restore with valid junctions
- Test: `fruit_graft.py` F1-F2, `compact_integration.py`

### P9: Boundary enforcement
- Growth blocked when new position is outside current domain
- `boundary_blocks` counter tracks violations
- Test: `snake_sim.py`, `e2e_lifecycle.py`

### P10: Collision detection
- Growth blocked when new position is already visited
- Self-collision and inter-snake collision detected
- Test: `snake_sim.py`

### P11: Two validators are separated
- `check_geometric_junction()` (5-pair): full tuple equality — for pure snake growth
- `check_graft_interface()` (1-pair): frame equality only — for graft boundaries
- Geometric ≤ Graft (geometric is stricter)
- Test: `junction_contract.py`, `stress_test.py` TEST 6

### P12: Compact identity is lossless
- String encoding (9.3x object / 17.7x storage) preserves all letters
- Encode/decode round-trip passes 1000 random cases
- Test: `compact_identity.py`, `compact_integration.py`

### P13: Grape Cluster independence
- Multiple fruits in one network have distinct identities
- Fruits grow independently
- Snake continues growing after fruit creation
- Test: `fruit_graft.py` E1-E2, `stress_test.py` TEST 3

## 2. Module Contracts

### 2.1 snake_core (snake_sim.py)

**Responsibility:** Cube generation, growth, junction verification

**Public API:**
- `Snake(seed, mode, domain_size, start_pos)` — constructor
- `grow(direction) → (bool, str)` — grow one cube in direction (0-5)
- `check_junctions() → (ok, tot)` — geometric junction (5-pair)
- `check_graft_interface() → (ok, tot)` — graft interface (1-pair)
- `state_dict() → dict` — serialize
- `restore(state)` — deserialize

**Invariants:**
- `len(visited) == len(positions)` always
- `growth_count == len(positions)` always
- `current_domain` consistent with domain_size
- Letters generated deterministically from (seed, position, axis)

**Directions:** 0=+X, 1=-X, 2=+Y, 3=-Y, 4=+Z, 5=-Z

### 2.2 snake_branch (implicit in snake_core)

**Responsibility:** Branching (multiple snakes from different positions)

**Rules:**
- Each branch is an independent Snake instance
- Branches share no state with parent
- Branch can start from any position (not just origin)

### 2.3 fruit_core (fruit_graft.py — Fruit class)

**Responsibility:** Fruit creation, onion-layer growth, identity

**Public API:**
- `Fruit(origin_snake_id, origin_positions, seed, domain_size)`
- `layer_grow(layer_num) → int` — grow one onion layer, returns new cube count
- `identity() → tuple` — unique identity
- `check_junctions()` — not applicable (fruit uses onion mode)

**Invariants:**
- `growth_mode == 'onion'` always
- Layers do not overlap (no position in multiple layers)
- Origin positions preserved
- Fruit cannot merge (no merge method)

### 2.4 onion_growth (fruit_graft.py — FruitFormation)

**Responsibility:** Cut snake segment, create fruit

**Public API:**
- `cut_and_remove(snake, start, end, seed) → (Fruit, bool)`
- `cut_fruit(snake, start, end, seed) → (Fruit, positions)`

**Rules:**
- Cut segment becomes fruit with new identity
- Snake's remaining positions preserved
- Origin reference stored in fruit

### 2.5 graft_engine (fruit_graft.py — GraftEngine)

**Responsibility:** Cut and reconnect snake segments

**Public API:**
- `graft(src, cut_idx, dst, attach_pos, interface_check) → GraftResult`
- `cut_snake(snake, idx) → (left, right)`
- `check_interface(pos_a, snake_a, pos_b, snake_b) → (bool, str)`

**Rules:**
- Interface must match (frame compatibility) before graft
- Connection junction must pass (frame match) or rollback
- On rejection: full state restoration (no partial changes)
- Moved segment letters preserved from source

### 2.6 grape_cluster (implicit)

**Responsibility:** Multiple snakes + fruits in one network

**Rules:**
- Each object has independent identity
- No global used-set or collision rejection across objects
- Network topology is emergent, not prescribed

### 2.7 identity_manager (compact_identity.py)

**Responsibility:** Compact encoding of letters

**Encodings:**
- String: 28.6 B/cube (object), 15.0 B/cube (storage), 9.3x/17.7x reduction
- Encode: 2.39 ms/5000 cubes
- Decode: 5.12 ms/5000 cubes
- Lossless: verified 1000 random cases

### 2.8 boundary_manager (snake_sim.py — domain_of)

**Responsibility:** Domain boundary enforcement

**Rules:**
- `domain_of(pos, size)` = (x//size, y//size, z//size)
- Growth blocked when domain changes
- `boundary_blocks` counter tracks violations

### 2.9 persistence (snake_sim.py — state_dict/restore)

**Responsibility:** Save/Restore of snake state

**Format:** JSON-serializable dict with positions, letters, visited, directions, etc.

**Rules:**
- Compact (string) or raw (tuple) letter encoding
- domain_size must be preserved
- Restore must preserve all junctions
- Growth must continue after restore

### 2.10 verification (junction_contract.py)

**Responsibility:** Two validators

**Validators:**
- `check_geometric_junction()`: 5-pair (full tuple) — pure snake
- `check_graft_interface()`: 1-pair (frame) — graft boundaries

## 3. Test Coverage

| Test File | Coverage |
|---|---|
| snake_perf.py | Growth cost, junction cost, multi-snake, grape cluster |
| fruit_graft.py | Fruit formation, graft, isolation, persistence |
| e2e_lifecycle.py | Full lifecycle (snake→branch→cut→fruit→graft→save→restore→grow) |
| graft_verify_prof.py | Graft scaling, restore profiling, memory profiling, junction verification |
| reconcile.py | Memory reconciliation, restore timing, graft valid/invalid, identity lossless |
| compact_identity.py | Lossless verification, memory comparison, encode/decode, restore |
| successful_graft.py | Graft scaling (5→5000) |
| string_restore.py | String restore benchmark (1K/5K/10K) |
| graft_audit.py | Graft junction audit (5/50/250/500/2500) |
| junction_contract.py | Geometric vs graft interface contract |
| compact_integration.py | Compact identity in snake/fruit/graft/save/restore E2E |
| stress_test.py | 6 structural stress tests |

## 4. Commit History

| Commit | Description |
|---|---|
| e5bebfc | snake_sim.py (snake growth + junctions) |
| 8008db4 | snake_scale_cost.py (growth cost, domain expansion, multi-snake) |
| 795a7c3 | fruit_graft.py (fruit formation + graft + grape + persistence) |
| 77124ce | snake_perf.py (performance benchmark) |
| 73a2ba2 | e2e_lifecycle.py (end-to-end lifecycle) |
| cf12798 | graft_verify_prof.py (graft junction verification + rollback) |
| 123ec9e | reconcile.py (benchmark reconciliation) |
| b527736 | compact_identity.py + successful_graft.py |
| f3a6334 | graft_audit.py + string_restore.py + frame-only junction check |
| f03020a | junction_contract.py + compact_integration.py |
| 9200c4a | stress_test.py + domain_size fix |

## 5. Direction Transition Contract (Locked)

| Event | Validator | Rationale |
|---|---|---|
| Snake walks straight | Geometric (5-pair) | Same-axis inheritance |
| Snake turns | Geometric (5-pair) | New axis inherited from turn point |
| Branch body grows | Geometric (5-pair) | Same seed, same-axis inheritance |
| Branch connects to parent | Graft interface (1-pair) | Different seed, frame compatibility |
| Graft between objects | Graft interface (1-pair) | Different seed, frame compatibility |

**Rule: Direction and Connectivity are separate concerns.**
**Rule: Geometry defines continuity within an object. Graft defines compatibility between objects.**

## 6. Regression Suite (regression_suite.py)

Single test covering: STRAIGHT → TURN → BRANCH → GRAFT → FRUIT → PERSISTENCE → IDENTITY

All 8 checks pass. This is the baseline regression test. Any change must keep this passing.

## 7. Test Coverage (Final)

| Test File | Coverage |
|---|---|
| snake_perf.py | Growth cost, junction cost, multi-snake, grape cluster |
| fruit_graft.py | Fruit formation, graft, isolation, persistence |
| e2e_lifecycle.py | Full lifecycle (snake→branch→cut→fruit→graft→save→restore→grow) |
| graft_verify_prof.py | Graft scaling, restore profiling, memory profiling, junction verification |
| reconcile.py | Memory reconciliation, restore timing, graft valid/invalid, identity lossless |
| compact_identity.py | Lossless verification, memory comparison, encode/decode, restore |
| successful_graft.py | Graft scaling (5→5000) |
| string_restore.py | String restore benchmark (1K/5K/10K) |
| graft_audit.py | Graft junction audit (5/50/250/500/2500) |
| junction_contract.py | Geometric vs graft interface contract |
| compact_integration.py | Compact identity in snake/fruit/graft/save/restore E2E |
| stress_test.py | 6 structural stress tests |
| longrun_stress.py | Randomized 10K, 100 branches, 49-object grape network |
| direction_transition.py | Direction transition contract (straight/turn/branch) |
| regression_suite.py | STRAIGHT→TURN→BRANCH→GRAFT→FRUIT regression |

## 8. Commit History (Final)

| Commit | Description |
|---|---|
| e5bebfc | snake_sim.py (snake growth + junctions) |
| 8008db4 | snake_scale_cost.py (growth cost, domain expansion, multi-snake) |
| 795a7c3 | fruit_graft.py (fruit formation + graft + grape + persistence) |
| 77124ce | snake_perf.py (performance benchmark) |
| 73a2ba2 | e2e_lifecycle.py (end-to-end lifecycle) |
| cf12798 | graft_verify_prof.py (graft junction verification + rollback) |
| 123ec9e | reconcile.py (benchmark reconciliation) |
| b527736 | compact_identity.py + successful_graft.py |
| f3a6334 | graft_audit.py + string_restore.py + frame-only junction check |
| f03020a | junction_contract.py + compact_integration.py |
| 9200c4a | stress_test.py + domain_size fix |
| 3879551 | longrun_stress.py (randomized 10K, 100 branches, 49-object grape) |
| 8500823 | direction_transition.py (direction transition contract) |
| (this) | regression_suite.py + baseline freeze |

## 9. Next Steps (not yet implemented)

- C implementation of compact identity
- Performance optimization of letter restoration (69% of restore time)
- Large-scale grape network (1000+ objects)
- 4D/5D extension (deferred per original spec)
"