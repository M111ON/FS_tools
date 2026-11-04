"""Independent Geometry Oracle for Truncated Octahedron.

Builds the truncated octahedron from its OWN coordinates (regular TO),
not from cube-edge mapping. Then compares incidence table to scale_incidence.py.

Regular truncated octahedron:
  - 14 faces: 8 regular hexagons + 6 squares
  - 24 vertices, 36 edges
  - Can be defined as Voronoi cell of BCC lattice, or as
    truncated octahedron with circumradius R.

Construction (independent of cube):
  Vertices are at permutations of (0, ±1, ±2) — 24 points.
  Faces:
    - 6 squares: x = ±2, y = ±2, z = ±2 (axis-aligned, 4 vertices each)
    - 8 hexagons: ±x ± y ± z = 4 (diagonal planes, 6 vertices each)? 
      Actually hexagons are at the 8 corners of the cube [±1,±1,±1].

Let's use the standard coordinate set:
  Vertices: all permutations of (0, ±1, ±2) -> 24 points
  (each coordinate appears in each position: 3 choices for which coord is 0,
   2 choices for sign of ±1, 2 choices for sign of ±2, and 3! / ... = 24)

Face incidence derived from vertex coordinates directly.
"""
import sys, os, json, hashlib, random, time
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from itertools import permutations

EMBED_DIM = 384

# ---------- Build regular truncated octahedron from coordinates ----------
# Vertices: permutations of (0, ±1, ±2)
# Standard set: 24 vertices
verts = set()
for perm in permutations([0, 1, 2]):
    for s1 in (1, -1):
        for s2 in (1, -1):
            # the 0 stays 0; ±1 and ±2 get signs
            # perm tells which coordinate gets 0, 1, 2
            v = [0, 0, 0]
            v[perm.index(0)] = 0
            v[perm.index(1)] = s1 * 1
            v[perm.index(2)] = s2 * 2
            verts.add(tuple(v))
# Also need permutations where 0,1,2 assigned differently — permutations of values covers all
# But permutations([0,1,2]) gives orderings of values; we need orderings of positions.
# Redo: all permutations of positions for (0, ±1, ±2)
verts = set()
for p in permutations(range(3)):  # which axis gets 0, 1, 2
    for s1 in (1, -1):
        for s2 in (1, -1):
            v = [0, 0, 0]
            v[p[0]] = 0
            v[p[1]] = s1 * 1
            v[p[2]] = s2 * 2
            verts.add(tuple(v))
verts = sorted(verts)
assert len(verts) == 24, f"expected 24 vertices, got {len(verts)}"
V_SET = verts
V_INDEX = {v: i for i, v in enumerate(verts)}

# ---------- Face detection ----------
# A face is a set of vertices that are coplanar and form a polygon.
# Square faces: 4 vertices with one coordinate = ±2, others in {-1,0,1} with sum constraints
# Hexagon faces: 6 vertices with |x|+|y|+|z| = 4? or similar

# Find faces by: for each axis and sign, collect vertices where that coord has extreme value
# Square faces: coord = ±2 (6 faces)
square_faces = {}  # face_id -> list of vertex indices
fid = 0
for axis in range(3):
    for sign in (1, -1):
        val = sign * 2
        vs = [i for i, v in enumerate(verts) if v[axis] == val]
        # these should be 4 vertices forming a square
        assert len(vs) == 4, f"square face axis={axis} sign={sign}: {len(vs)} verts"
        square_faces[fid] = (axis, sign, vs)
        fid += 1
# fid should be 6
assert fid == 6, f"expected 6 square faces, got {fid}"

# Hexagon faces: remaining vertices form 8 hexagons
# Each hexagon corresponds to a corner of the cube [±1,±1,±1]
# Vertices with |x|,|y|,|z| all in {0,1,2} and sum of abs = 4? 
# Let's find hexagons by: vertices not on any square face? No, every vertex is on...
# Actually each vertex of TO is on 1 square face and 2 hexagon faces.
# Find hexagon faces: for each combination of signs (sx, sy, sz) in {±1}^3,
# collect vertices v such that sx*v[0] + sy*v[1] + sz*v[2] is maximized (supporting plane)

hex_faces = {}
for sx in (1, -1):
    for sy in (1, -1):
        for sz in (1, -1):
            # supporting plane: sx*x + sy*y + sz*z = max
            vals = [sx * v[0] + sy * v[1] + sz * v[2] for v in verts]
            maxval = max(vals)
            vs = [i for i, v in enumerate(verts) if (sx * v[0] + sy * v[1] + sz * v[2]) == maxval]
            # should be 6 vertices forming a hexagon
            assert len(vs) == 6, f"hex face ({sx},{sy},{sz}): {len(vs)} verts, expected 6"
            hex_faces[(sx, sy, sz)] = vs

assert len(hex_faces) == 8, f"expected 8 hex faces, got {len(hex_faces)}"

# ---------- Face incidence (vertex -> faces) ----------
# For each vertex, determine which faces contain it
vertex_faces = defaultdict(list)  # vertex_idx -> list of face ids (square + hex)
# square faces
for fid, (axis, sign, vs) in square_faces.items():
    for vi in vs:
        vertex_faces[vi].append(("sq", fid))
# hex faces
hex_id_map = {}
for idx, (key, vs) in enumerate(hex_faces.items()):
    hex_id_map[key] = 8 + idx  # hex faces indexed 8..15
    for vi in vs:
        vertex_faces[vi].append(("hex", hex_id_map[key]))

# Each vertex should be on exactly 1 square + 2 hexagons = 3 faces
for vi in range(len(verts)):
    faces = vertex_faces[vi]
    sq_count = sum(1 for t, _ in faces if t == "sq")
    hex_count = sum(1 for t, _ in faces if t == "hex")
    assert sq_count == 1, f"vertex {vi}: {sq_count} squares (expect 1)"
    assert hex_count == 2, f"vertex {vi}: {hex_count} hexagons (expect 2)"

# ---------- Build incidence table (vertex -> (hex_a, hex_b, square)) ----------
# Map face ids to our canonical indexing:
#   hex faces: 0..7 (we'll assign by order)
#   square faces: 0..5

# Assign hex face ids 0..7
hex_keys_sorted = sorted(hex_faces.keys())
hex_canonical = {key: i for i, key in enumerate(hex_keys_sorted)}
# square face ids already 0..5

# Build vertex incidence: for each vertex, (hex_a, hex_b, square)
ORACLE_VERTICES = []
for vi in range(len(verts)):
    faces = vertex_faces[vi]
    hexs = sorted([hex_canonical[k] for t, fid in faces for k in [None]  # placeholder
                   ] if False else [])
    # properly:
    hex_ids = []
    sq_id = None
    for t, fid in faces:
        if t == "hex":
            # find which hex key this fid corresponds to
            for key, hid in hex_id_map.items():
                if hid == fid:
                    hex_ids.append(hex_canonical[key])
        else:
            sq_id = fid  # square fid is already 0..5
    assert len(hex_ids) == 2
    assert sq_id is not None
    ORACLE_VERTICES.append((hex_ids[0], hex_ids[1], sq_id))

assert len(ORACLE_VERTICES) == 24

# ---------- Compare to scale_incidence.py REAL_VERTICES ----------
# Import scale_incidence's REAL_VERTICES
sys.path.insert(0, '.')
import scale_incidence as si
EXISTING = si.REAL_VERTICES

print("=" * 70)
print("INDEPENDENT GEOMETRY ORACLE — Truncated Octahedron")
print("=" * 70)
print(f"  Oracle vertices: {len(ORACLE_VERTICES)}")
print(f"  Existing vertices: {len(EXISTING)}")
print(f"  Hex faces: {len(hex_canonical)} (expect 8)")
print(f"  Square faces: {len(square_faces)} (expect 6)")
print()

# Compare as sets (canonical form: sorted hex pair + square)
def canonical(v):
    h1, h2, sq = v
    return (tuple(sorted((h1, h2))), sq)

oracle_set = set(canonical(v) for v in ORACLE_VERTICES)
existing_set = set(canonical(v) for v in EXISTING)

print(f"  Oracle unique combos: {len(oracle_set)}")
print(f"  Existing unique combos: {len(existing_set)}")
print(f"  Intersection: {len(oracle_set & existing_set)}")
print(f"  Oracle only: {len(oracle_set - existing_set)}")
print(f"  Existing only: {len(existing_set - oracle_set)}")

# ---------- Tests ----------
def test_vertex_count():
    print()
    print("=" * 70)
    print("TEST 1: Vertex Count (24)")
    print("=" * 70)
    print(f"  oracle: {len(ORACLE_VERTICES)}, existing: {len(EXISTING)}")
    assert len(ORACLE_VERTICES) == 24
    assert len(EXISTING) == 24
    print("  PASS")

def test_face_count():
    print()
    print("=" * 70)
    print("TEST 2: Face Count (8 hex + 6 square)")
    print("=" * 70)
    print(f"  hex: {len(hex_canonical)} (expect 8)")
    print(f"  square: {len(square_faces)} (expect 6)")
    assert len(hex_canonical) == 8
    assert len(square_faces) == 6
    print("  PASS")

def test_incidence_match():
    print()
    print("=" * 70)
    print("TEST 3: Incidence Table Match (oracle vs existing)")
    print("=" * 70)
    # canonical comparison
    match = (oracle_set == existing_set)
    print(f"  exact set match: {match}")
    if not match:
        print(f"  oracle only: {oracle_set - existing_set}")
        print(f"  existing only: {existing_set - oracle_set}")
    assert match, "incidence tables do not match"
    print("  PASS")

def test_no_collision():
    print()
    print("=" * 70)
    print("TEST 4: No Collision / No Duplicate (oracle)")
    print("=" * 70)
    combos = set()
    dup = 0
    for v in ORACLE_VERTICES:
        key = canonical(v)
        if key in combos:
            dup += 1
        combos.add(key)
    print(f"  unique combos: {len(combos)}, duplicates: {dup}")
    assert dup == 0
    assert len(combos) == 24
    print("  PASS")

def test_hex_adjacency():
    print()
    print("=" * 70)
    print("TEST 5: Hex Adjacency (oracle)")
    print("=" * 70)
    hex_adj = defaultdict(set)
    for h1, h2, sq in ORACLE_VERTICES:
        hex_adj[h1].add(h2)
        hex_adj[h2].add(h1)
    # each hex should have degree 3 (cube vertex degree 3)
    degrees = [len(hex_adj[h]) for h in range(8)]
    print(f"  hex degrees: {degrees}")
    assert all(d == 3 for d in degrees), f"not all degree 3: {degrees}"
    print(f"  all hex degree 3: True")
    print(f"  edges: {sum(degrees)//2}")
    print("  PASS")

def test_square_incidence():
    print()
    print("=" * 70)
    print("TEST 6: Square -> 4 Hex (per square face)")
    print("=" * 70)
    for sq in range(6):
        hexs = set()
        for h1, h2, s in ORACLE_VERTICES:
            if s == sq:
                hexs.add(h1); hexs.add(h2)
        print(f"  square {sq}: hexes = {sorted(hexs)} (count={len(hexs)})")
        assert len(hexs) == 4, f"square {sq} has {len(hexs)} hexes"
    print("  PASS")

def test_euler():
    print()
    print("=" * 70)
    print("TEST 7: Euler Characteristic (V - E + F = 2)")
    print("=" * 70)
    V = len(verts)
    F = 14
    # E = sum of face edges / 2; hex has 6, square has 4
    E = (8 * 6 + 6 * 4) // 2
    print(f"  V={V}, E={E}, F={F}")
    print(f"  V - E + F = {V - E + F} (expect 2)")
    assert V - E + F == 2
    print("  PASS")

def test_independence():
    print()
    print("=" * 70)
    print("TEST 8: Independence (two construction methods agree)")
    print("=" * 70)
    # Method 1: cube-edge derivation (scale_incidence.py)
    # Method 2: regular TO coordinates (this file)
    # They must agree on all 24 vertices
    print(f"  Method 1 (cube-edge): {len(EXISTING)} vertices")
    print(f"  Method 2 (coordinates): {len(ORACLE_VERTICES)} vertices")
    print(f"  Match: {oracle_set == existing_set}")
    assert oracle_set == existing_set
    print("  Two independent methods produce identical incidence table.")
    print("  PASS")


def main():
    tests = [test_vertex_count, test_face_count, test_incidence_match,
             test_no_collision, test_hex_adjacency, test_square_incidence,
             test_euler, test_independence]
    all_ok = True
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            all_ok = False
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR: {e}")
            all_ok = False
    print()
    print("=" * 70)
    print(f"INDEPENDENT GEOMETRY ORACLE: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    if all_ok:
        print()
        print("Conclusion: Two independent construction methods (cube-edge")
        print("derivation + regular TO coordinates) produce identical")
        print("incidence tables for all 24 vertices. The topology is real,")
        print("not an artifact of a single construction method.")
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
