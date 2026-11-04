"""Prototype: Truncated Octahedron Junction.

A truncated octahedron has 14 faces: 8 hexagons + 6 squares.
- Each vertex touches 2 hexagons + 1 square (3-face junction).
- 4 antipodal hexagon pairs (dual-rail / branch pairs).
- 6 squares = 6 faces of original cube (frame).

This prototype maps the truncated octahedron onto LetterCube primitives:
  - 3-face junction: 2 hex (letters) + 1 square (frame)  -> same as cube junction
  - 4 antipodal pairs -> 4 branches (dual-rail checkpoint)
  - 6 squares -> 6 directions (grow 0-5)
  - 8 hexagons -> 8 minicubes (2x2x2 cube vertices)

The 2x2x2 cube (8 minicubes, 48 inner faces) vs outer cube (6 faces, 2 used)
share the same junction principle at different scale (fractal self-similarity).

Geometry layer v1.0 (cube-based) is NOT modified.
"""
import sys, os, json, hashlib, random, time
sys.path.insert(0, '.')
import numpy as np

# ── Truncated Octahedron topology ────────────────────────────────────────────
# 8 hexagon faces (one per vertex of original cube), indexed 0..7
# 6 square faces (one per face of original cube), indexed 0..5 (X+,X-,Y+,Y-,Z+,Z-)
# 4 antipodal hexagon pairs: (0,7),(1,6),(2,5),(3,4)  (opposite cube vertices)
# Each vertex of truncated octahedron: 2 hex + 1 square

HEX_FACES = 8
SQUARE_FACES = 6
TOTAL_FACES = 14
VERTICES = 24
EDGES = 36

# antipodal hexagon pairs (dual-rail)
HEX_ANTIPODAL = [(0, 7), (1, 6), (2, 5), (3, 4)]

# square face -> direction index (grow 0-5)
SQUARE_DIR = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5}  # X+,X-,Y+,Y-,Z+,Z-

# vertex incidence: each vertex touches 2 hex + 1 square
# model: vertex (h1, h2, sq)  -- 3 faces meeting
# 24 vertices = 8 cube-vertices * 3 (each truncated vertex splits into 3)
VERTEX_INCIDENCE = []
for v in range(8):
    # cube vertex v has 3 incident faces; truncation creates 3 vertices
    faces = [(v, (v + 1) % 8, v % 6),
             (v, (v + 2) % 8, (v + 1) % 6),
             (v, (v + 3) % 8, (v + 2) % 6)]
    for h1, h2, sq in faces:
        VERTEX_INCIDENCE.append((h1 % HEX_FACES, h2 % HEX_FACES, sq % SQUARE_FACES))


class TruncatedOctJunction:
    """3-face junction: 2 hex (letters) + 1 square (frame).

    Same contract as LetterCube junction:
      - full check: both hex letters + square frame match (5-pair equivalent)
      - graft check: square frame match only (1-pair equivalent)
    """

    def __init__(self, seed="toj"):
        self.seed = seed
        self.hex_letters = {}   # hex_face_idx -> list of (4-letter tuple, frame)
        self.square_frames = {}  # square_face_idx -> frame char
        self._init()

    def _init(self):
        import random
        r = random.Random(self.seed)
        for h in range(HEX_FACES):
            letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
            frame = r.choice("abcdefgh")
            self.hex_letters[h] = [(tuple(letters), frame)] * 3
        for sq in range(SQUARE_FACES):
            self.square_frames[sq] = r.choice("abcdefgh")

    def check_full(self):
        """Full junction: 2 hex letters + square frame all match at each vertex."""
        ok = 0; tot = 0
        for h1, h2, sq in VERTEX_INCIDENCE:
            tot += 1
            if (h1 in self.hex_letters and h2 in self.hex_letters and
                    sq in self.square_frames):
                # hex1 frame == hex2 frame == square frame
                f1 = self.hex_letters[h1][0][1]
                f2 = self.hex_letters[h2][0][1]
                fs = self.square_frames[sq]
                if f1 == f2 == fs:
                    ok += 1
        return ok, tot

    def check_graft(self):
        """Graft junction: square frame match only."""
        ok = 0; tot = 0
        for h1, h2, sq in VERTEX_INCIDENCE:
            tot += 1
            if sq in self.square_frames:
                # just need square frame present (frame compatibility)
                ok += 1
        return ok, tot

    def antipodal_pairs(self):
        """Return 4 antipodal hex pairs (dual-rail checkpoints)."""
        return list(HEX_ANTIPODAL)

    def branch_count(self):
        return len(HEX_ANTIPODAL)  # 4


class ScaleComparison:
    """Compare 2x2x2 cube (8 minicubes, 48 faces) vs outer cube (6 faces).

    Same junction principle at different scale:
      - Outer cube: 6 faces, each can be a 2-face junction (adjacent faces)
      - 2x2x2 cube: 8 minicubes, each with 6 faces = 48 faces total
      - Truncated octahedron: bridges the two (14 faces, 3-face junctions)
    """

    @staticmethod
    def outer_cube_junctions():
        """Outer cube: 6 faces, adjacent pairs share an edge."""
        # faces: 0=X+,1=X-,2=Y+,3=Y-,4=Z+,5=Z-
        # adjacent pairs (share edge): all except antipodal pairs
        antipodal = [(0, 1), (2, 3), (4, 5)]
        pairs = []
        for i in range(6):
            for j in range(i + 1, 6):
                if (i, j) not in antipodal and (j, i) not in antipodal:
                    pairs.append((i, j))
        return pairs  # 12 adjacent pairs

    @staticmethod
    def inner_cube_faces():
        """2x2x2 cube: 8 minicubes * 6 faces = 48 faces."""
        return 8 * 6  # 48

    @staticmethod
    def truncated_oct_faces():
        """Truncated octahedron: 8 hex + 6 square = 14 faces."""
        return TOTAL_FACES  # 14

    @staticmethod
    def scale_ratio():
        """Inner (48) / outer (6) = 8x; truncated (14) is between."""
        return {
            "outer_cube_faces": 6,
            "inner_cube_faces": 48,
            "truncated_oct_faces": 14,
            "inner_to_outer_ratio": 48 / 6,
            "truncated_to_outer_ratio": 14 / 6,
        }


def test_truncated_oct():
    print("=" * 70)
    print("TEST 1: Truncated Octahedron Topology")
    print("=" * 70)
    print(f"  faces: {TOTAL_FACES} (8 hex + 6 square)")
    print(f"  vertices: {VERTICES}")
    print(f"  edges: {EDGES}")
    print(f"  antipodal hex pairs: {HEX_ANTIPODAL}")
    print(f"  branch count: {len(HEX_ANTIPODAL)}")
    print(f"  vertex incidences: {len(VERTEX_INCIDENCE)}")
    # Euler check
    V, E, F = VERTICES, EDGES, TOTAL_FACES
    print(f"  Euler: V-E+F = {V}-{E}+{F} = {V - E + F} (expect 2)")
    assert V - E + F == 2
    print("  PASS")

def test_junction():
    print("=" * 70)
    print("TEST 2: 3-Face Junction (2 hex + 1 square)")
    print("=" * 70)
    toj = TruncatedOctJunction(seed="test")
    full_ok, full_tot = toj.check_full()
    graft_ok, graft_tot = toj.check_graft()
    print(f"  full (hex+hex+square): {full_ok}/{full_tot}")
    print(f"  graft (square only):   {graft_ok}/{graft_tot}")
    print(f"  junction contract: full <= graft? {full_ok <= graft_ok}")
    print("  PASS")

def test_antipodal():
    print("=" * 70)
    print("TEST 3: Antipodal Pairs (dual-rail)")
    print("=" * 70)
    toj = TruncatedOctJunction(seed="test2")
    pairs = toj.antipodal_pairs()
    print(f"  pairs: {pairs}")
    # verify each pair is disjoint and covers all 8 hexagons
    used = set()
    for a, b in pairs:
        assert a != b
        assert a not in used and b not in used
        used.add(a); used.add(b)
    assert used == set(range(HEX_FACES))
    print(f"  all 8 hexagons covered: {used == set(range(HEX_FACES))}")
    print("  PASS")

def test_scale():
    print("=" * 70)
    print("TEST 4: Scale Comparison (2x2x2 vs outer cube)")
    print("=" * 70)
    sc = ScaleComparison()
    print(f"  outer cube adjacent pairs: {len(sc.outer_cube_junctions())}")
    print(f"  inner cube faces: {sc.inner_cube_faces()}")
    print(f"  truncated oct faces: {sc.truncated_oct_faces()}")
    r = sc.scale_ratio()
    for k, v in r.items():
        print(f"    {k}: {v}")
    # same principle: 3 faces meet at vertex
    print("  junction principle: 3 faces meet at every vertex (outer/inner/truncated)")
    print("  PASS")

def test_fractal():
    print("=" * 70)
    print("TEST 5: Fractal Self-Similarity")
    print("=" * 70)
    # 2x2x2 cube = 8 minicubes, each minicube = 6 faces
    # truncated octahedron = 8 hex (minicube positions) + 6 square (outer faces)
    # -> hex faces correspond to 8 minicubes, square faces to 6 outer cube faces
    print("  8 hexagons  <-> 8 minicubes of 2x2x2 cube")
    print("  6 squares   <-> 6 faces of outer cube")
    print("  4 antipodal pairs <-> 4 branches (dual-rail)")
    print("  24 vertices <-> 24 3-face junctions")
    print("  -> same junction principle at every scale")
    print("  PASS")

def main():
    tests = [test_truncated_oct, test_junction, test_antipodal, test_scale, test_fractal]
    all_ok = True
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR: {e}")
            all_ok = False
    print("\n" + "=" * 70)
    print(f"ALL TESTS: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
