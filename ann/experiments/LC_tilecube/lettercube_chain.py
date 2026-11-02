"""D2: chain mode cross-level consistency (simplified)."""
import sys
sys.path.insert(0, '.')
from lettercube_py import LetterCube, grid

def test_chain_crosslevel():
    print("=== D2: chain mode cross-level ===")
    seed = "chain1"
    # Level0: build a 2x2x2 in chain order (slide along X)
    sim0 = LetterCube(seed, deterministic=True)
    # chain order: (0,0,0)->(1,0,0)->(0,1,0)->... slide along X first
    order0 = [(x, y, z) for z in range(2) for y in range(2) for x in range(2)]
    sim0.build(order0)
    o0, t0 = sim0.links()
    print(f"  level0 chain build: junction {o0}/{t0}")

    # Level1: build supers in same chain order
    sim1 = LetterCube(seed + "|L1", deterministic=True)
    sim1.build(order0)
    o1, t1 = sim1.links()
    print(f"  level1 chain build: junction {o1}/{t1}")

    # Cross-level: for each super, its sub-cubes should have consistent row keys
    # along the slide axis (X). Check that row_key(0, sub) is same for adjacent supers.
    consistent = True
    for i in range(len(order0) - 1):
        a = order0[i]; b = order0[i+1]
        # adjacent along X?
        if b[0] == a[0] + 1 and b[1] == a[1] and b[2] == a[2]:
            rka = __import__('lettercube_py').row_key(0, a)
            rkb = __import__('lettercube_py').row_key(0, b)
            # in row mode these differ (different perpendicular coords? no—perp coords are y,z which are same)
            # so rka should == rkb for same row
            if rka != rkb:
                consistent = False
                print(f"  MISMATCH row key: {a}->{b}: {rka} vs {rkb}")
    print(f"  adjacent supers along X share row key: {consistent}")
    print(f"  chain mode cross-level: {'OK' if (o0==t0 and o1==t1 and consistent) else 'FAIL'}")

if __name__ == "__main__":
    test_chain_crosslevel()
    print("\ndone")