"""Nested cube (onion peel) test: 2-level 2x2x2 with 3-letter corner view + cross-level routing."""
import sys
sys.path.insert(0, '.')
from lettercube_py import LetterCube, grid, seed_rng, row_key
import random, hashlib, json

def corner_letters(sim, super_pos, corner_bits):
    """3-letter view at a corner of a super-cube.
    corner_bits = (bx,by,bz) in {0,1}^3 selects which sub-cube (octant).
    Returns the 3 row-assignment letters (one per axis) for that sub-cube."""
    # sub-cube position within super = corner_bits
    sub_pos = tuple(corner_bits)
    # find the cube object
    if sub_pos not in sim.cubes:
        return None
    c = sim.cubes[sub_pos]
    # 3 letters = frame letter of each axis row
    return tuple(rv[1] for rv in c["row_vals"])

def cross_level_route(seed, super_pos, corner_bits, level):
    """O(1) route: given super position + corner + level, compute sub-cube letters without
    reading all cubes. Uses deterministic seed."""
    sub_pos = tuple(corner_bits)
    # row key for each axis at sub_pos
    letters = []
    for k in range(3):
        rk = row_key(k, sub_pos)
        r = seed_rng(seed, "r" + str(rk))
        ls = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
        fr = r.choice("abcdefgh")
        letters.append(fr)
    return tuple(letters)

def test_nested_2level():
    print("=== B1: nested 2-level (8 sub-cubes in 2x2x2 super) ===")
    seed = "nested1"
    # Level 0: 8 sub-cubes
    sim0 = LetterCube(seed, deterministic=True)
    positions = [(x, y, z) for z in range(2) for y in range(2) for x in range(2)]
    sim0.build(positions)
    o, t = sim0.links()
    print(f"  level0 2x2x2: junction {o}/{t}, cubes={len(sim0.cubes)}, rows={len(sim0.rows)}")

    # 3-letter corner view for all 8 corners
    print("  3-letter corner views:")
    for bits in [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)]:
        cl = corner_letters(sim0, None, bits)
        cr = cross_level_route(seed, None, bits, 0)
        match = (cl == cr)
        print(f"    corner {bits}: sim={cl} route={cr} match={match}")

    # Level 1: super-cube as one node in a 2x2x2 grid of supers (4 supers)
    print("\n  Level1: 2x2x2 grid of super-cubes (each super = 8 sub)")
    sim1 = LetterCube(seed + "|L1", deterministic=True)
    super_positions = [(x, y, z) for z in range(2) for y in range(2) for x in range(2)]
    sim1.build(super_positions)
    o1, t1 = sim1.links()
    print(f"  level1: junction {o1}/{t1}, supers={len(sim1.cubes)}")

    # Cross-level: from super position + corner -> sub letters O(1)
    print("\n  Cross-level routing (super_pos, corner) -> sub letters:")
    all_match = True
    for sp in super_positions[:2]:
        for cb in [(0, 0, 0), (1, 1, 1)]:
            r = cross_level_route(seed, sp, cb, 0)
            # verify by direct sim lookup (sub-cube at cb within a super is same regardless of super pos in our model)
            direct = corner_letters(sim0, None, cb)
            if r != direct:
                all_match = False
            print(f"    super={sp} corner={cb}: routed={r} direct={direct} {'OK' if r==direct else 'MISMATCH'}")
    print(f"  All cross-level routes match direct lookup: {all_match}")


def test_scaling_vs_layering():
    print("\n=== B3: scaling (more cubes) vs layering (more levels) ===")
    seed = "scale1"
    # Scaling: 4x4x4 single level
    sim_s = LetterCube(seed, deterministic=True)
    sim_s.build(grid(4, 4, 4) if False else [(x, y, z) for z in range(4) for y in range(4) for x in range(4)])
    o, t = sim_s.links()
    print(f"  scaling 4x4x4 single level: junction {o}/{t}, cubes={len(sim_s.cubes)}, rows={len(sim_s.rows)}")

    # Layering: 2 levels, each 2x2x2
    sim_l0 = LetterCube(seed, deterministic=True)
    sim_l0.build([(x, y, z) for z in range(2) for y in range(2) for x in range(2)])
    sim_l1 = LetterCube(seed + "|L1", deterministic=True)
    sim_l1.build([(x, y, z) for z in range(2) for y in range(2) for x in range(2)])
    o0, t0 = sim_l0.links()
    o1, t1 = sim_l1.links()
    print(f"  layering 2x (2x2x2): L0 junction {o0}/{t0}, L1 junction {o1}/{t1}")
    print("  (scaling grows cube count in one level; layering adds levels — different mechanisms)")


def test_collision_report():
    print("\n=== C1: collision rate (row signatures) — corrected ===")
    for size in [5, 10, 20]:
        sim = LetterCube("D", deterministic=True)
        G = [(x, y, z) for z in range(size) for y in range(size) for x in range(size)]
        sim.build(G)
        for k in range(3):
            row_keys = set()
            row_sigs = {}
            for pos, c in sim.cubes.items():
                rk = row_key(k, pos)
                row_keys.add(rk)
                if rk not in row_sigs:
                    row_sigs[rk] = c["row_vals"][k]
            # collision = two different row_keys with same signature
            sig_to_keys = {}
            for rk, rv in row_sigs.items():
                sig_to_keys.setdefault(str(rv), []).append(rk)
            collisions = sum(1 for v in sig_to_keys.values() if len(v) > 1)
            print(f"  grid {size}^3 axis {k}: distinct row_keys={len(row_keys)}, distinct sigs={len(set(str(v) for v in row_sigs.values()))}, signature collisions={collisions}")


if __name__ == "__main__":
    test_nested_2level()
    test_scaling_vs_layering()
    test_collision_report()
    print("\ndone")