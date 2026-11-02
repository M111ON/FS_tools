"""D1: rotation / capo shift on nested 2-level — does junction survive a uniform shift?"""
import sys
sys.path.insert(0, '.')
from lettercube_py import LetterCube, grid

def test_rotation():
    print("=== D1: capo shift / rotation on nested structure ===")
    seed = "rot1"
    # original 2x2x2
    sim0 = LetterCube(seed, deterministic=True)
    sim0.build([(x, y, z) for z in range(2) for y in range(2) for x in range(2)])
    o0, t0 = sim0.links()
    print(f"  original 2x2x2: junction {o0}/{t0}")

    # capo shift: translate all positions by +1 along X (wrap not needed for small)
    shifted = [(x+1, y, z) for (x, y, z) in sim0.cubes.keys()]
    sim1 = LetterCube(seed, deterministic=True)
    sim1.build(shifted)
    o1, t1 = sim1.links()
    print(f"  shifted +1 X:   junction {o1}/{t1}")

    # 90-degree rotation: (x,y,z) -> (y, -x, z) mapped to positive grid
    rot = []
    for (x, y, z) in sim0.cubes.keys():
        rot.append((y, (1 - x), z))  # map -1..0 to 0..1
    sim2 = LetterCube(seed, deterministic=True)
    sim2.build(rot)
    o2, t2 = sim2.links()
    print(f"  rotated 90 (mapped): junction {o2}/{t2}")

    # nested: shift the whole 2-level structure
    print("\n  nested 2-level shift:")
    simA = LetterCube(seed, deterministic=True)
    supers = [(x, y, z) for z in range(2) for y in range(2) for x in range(2)]
    simA.build(supers)
    shifted_supers = [(x+2, y+2, z+2) for (x, y, z) in supers]
    simB = LetterCube(seed, deterministic=True)
    simB.build(shifted_supers)
    oA, tA = simA.links()
    oB, tB = simB.links()
    print(f"    original supers: junction {oA}/{tA}")
    print(f"    shifted  supers: junction {oB}/{tB}")
    print(f"  capo shift preserves junctions: {o1==t1 and o2==t2 and oB==tB}")

if __name__ == "__main__":
    test_rotation()
    print("\ndone")