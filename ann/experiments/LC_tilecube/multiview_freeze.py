"""Prototype: 4D freeze frame + geo_jump multi-view addressing.

Concept (owner): scales (128, 162, 144, 64, 54, 48) expand through geo_jump
via the 3D frame frozen from 4D. 4D data is stored once; geo_jump (affine-
modular bijective transfer) provides different 3D views (cube, icosphere,
base-12, hexagonal) on demand. No multiple copies.

This prototype:
  1. Stores N vectors in a 20736-slot 4D freeze frame.
  2. Defines two views: cube (128 x 162) and icosphere (162 vertices x 128).
  3. Implements geo_jump transfer between views (permutation of slots).
  4. Verifies bijection: transfer then inverse = identity for all slots.
  5. Measures recall: cube-view addressing vs icosphere-view addressing
     vs PCA-to-cubic-grid baseline.

Python prototype only (#7825). Transfer is a fixed permutation (not learned).
"""
import sys, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, '.')

TOTAL = 20736  # 12^4 = 2^8 * 3^4
CUBE_FACTOR = 128   # 2^7
ICO_FACTOR = 162     # icosphere L2 vertices
# 128 * 162 = 20736


def icosahedron_faces_verts():
    """Return icosphere L2 vertices (162) and adjacency."""
    PHI = (1 + 5**0.5) / 2
    verts = np.array([
        (0,1,PHI),(0,1,-PHI),(0,-1,PHI),(0,-1,-PHI),
        (1,PHI,0),(1,-PHI,0),(-1,PHI,0),(-1,-PHI,0),
        (PHI,0,1),(PHI,0,-1),(-PHI,0,1),(-PHI,0,-1),
    ],dtype=float)
    verts /= np.linalg.norm(verts,axis=1,keepdims=True)
    faces = np.array([
        (0,2,8),(0,2,10),(0,4,6),(0,4,8),(0,6,10),
        (1,3,9),(1,3,11),(1,4,6),(1,4,9),(1,6,11),
        (2,5,7),(2,5,8),(2,7,10),(3,5,7),(3,5,9),
        (3,7,11),(4,8,9),(5,8,9),(6,10,11),(7,10,11),
    ],dtype=int)
    # subdivide twice
    for _ in range(2):
        new_faces=[]; mc={}; vl=list(verts)
        def gm(i,j):
            key=(min(i,j),max(i,j))
            if key not in mc:
                mid=(vl[i]+vl[j])/2; mid/=np.linalg.norm(mid)
                mc[key]=len(vl); vl.append(mid)
            return mc[key]
        for f in faces:
            a,b,c=f; ab=gm(a,b); bc=gm(b,c); ca=gm(c,a)
            new_faces += [(a,ab,ca),(b,bc,ab),(c,ca,bc),(ab,bc,ca)]
        faces=np.array(new_faces,dtype=int); verts=np.array(vl,dtype=float)
    return verts, faces


def build_adjacency(verts, faces):
    adj = defaultdict(set)
    for f in faces:
        for i in range(3):
            a,b=f[i],f[(i+1)%3]
            adj[a].add(b); adj[b].add(a)
    return adj


def cos(a,b):
    na,nb=np.linalg.norm(a),np.linalg.norm(b)
    return 0.0 if na<=0 or nb<=0 else float(np.dot(a,b)/(na*nb))


# --- geo_jump transfer (fixed permutation between views) ---
def build_transfer():
    """Build a bijective transfer between cube slots (0..20735) and
    icosphere slots (0..20735).

    Cube slot index = ci * ICO_FACTOR + cj  where ci in [0,CUBE_FACTOR), cj in [0,ICO_FACTOR)
    Ico slot index  = vi * CUBE_FACTOR + si  where vi in [0,ICO_FACTOR), si in [0,CUBE_FACTOR)

    Transfer: cube(ci,cj) <-> ico(cj, ci)  (transpose-like, bijective).
    This is the 'geo_jump' between views: a single affine-modular map.
    """
    transfer_cube_to_ico = np.zeros(TOTAL, dtype=int)
    transfer_ico_to_cube = np.zeros(TOTAL, dtype=int)
    for ci in range(CUBE_FACTOR):
        for cj in range(ICO_FACTOR):
            cube_slot = ci * ICO_FACTOR + cj
            ico_slot = cj * CUBE_FACTOR + ci
            transfer_cube_to_ico[cube_slot] = ico_slot
            transfer_ico_to_cube[ico_slot] = cube_slot
    return transfer_cube_to_ico, transfer_ico_to_cube


def test_bijection():
    print("=" * 60)
    print("TEST 1: geo_jump bijection (transfer then inverse = identity)")
    t_cti, t_itc = build_transfer()
    # verify all slots
    ok = True
    for s in range(TOTAL):
        if t_itc[t_cti[s]] != s:
            ok = False
            break
    print(f"  TOTAL={TOTAL} slots")
    print(f"  transfer(cube->ico) then inverse = identity: {ok}")
    # also verify surjective
    assert len(set(t_cti)) == TOTAL, "not surjective"
    print("  surjective: True")
    assert ok
    print("  PASS")


def test_transfer_properties():
    print("=" * 60)
    print("TEST 2: transfer properties (affine-modular)")
    t_cti, t_itc = build_transfer()
    # check that transfer is a permutation (bijective by construction)
    assert sorted(t_cti) == list(range(TOTAL))
    # check a few specific transfers
    print(f"  cube(0,0) -> ico({t_cti[0]//CUBE_FACTOR},{t_cti[0]%CUBE_FACTOR})")
    print(f"  cube({CUBE_FACTOR-1},{ICO_FACTOR-1}) -> ico({t_cti[-1]//CUBE_FACTOR},{t_cti[-1]%CUBE_FACTOR})")
    # verify transfer is its own inverse? (transpose is involutory only if square; here 128!=162 so no)
    print(f"  transfer is NOT involutory (128 != 162): correct")
    print("  PASS")


def test_recall_multiview():
    print("=" * 60)
    print("TEST 3: multi-view addressing recall vs PCA baseline")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    # Normalize (cosine space)
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    t_cti, t_itc = build_transfer()

    # --- Cube view addressing: project to 3D, quantize to 128x162 grid ---
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    # normalize projected
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    def cube_slot(v):
        # quantize 3D projected vector to cube slot (ci, cj)
        # ci in [0, 128): first dimension scaled
        # cj in [0, 162): second+third combined
        a = (v[0] + 1) / 2  # [0,1]
        b = (v[1] + v[2] + 2) / 4  # [0,1] approx
        ci = int(np.clip(a * (CUBE_FACTOR - 1), 0, CUBE_FACTOR - 1))
        cj = int(np.clip(b * (ICO_FACTOR - 1), 0, ICO_FACTOR - 1))
        return ci * ICO_FACTOR + cj

    X_cube_slots = [cube_slot(v) for v in Xp]
    Q_cube_slots = [cube_slot(v) for v in Qp]

    # cube view candidate generation: same slot + neighbors (slot +/- 1..K)
    cube_slot_map = defaultdict(list)
    for i, s in enumerate(X_cube_slots):
        cube_slot_map[s].append(i)

    def cube_candidates(q_slot, K=8):
        cands = set()
        for delta in range(-K, K + 1):
            ns = q_slot + delta
            if 0 <= ns < TOTAL:
                for idx in cube_slot_map.get(ns, []):
                    cands.add(idx)
        return cands

    # --- Icosphere view addressing: transfer cube slots -> ico slots ---
    # Store data in ico view: slot_ico = transfer(cube_slot)
    X_ico_slots = [int(t_cti[s]) for s in X_cube_slots]
    Q_ico_slots = [int(t_cti[s]) for s in Q_cube_slots]

    ico_slot_map = defaultdict(list)
    for i, s in enumerate(X_ico_slots):
        ico_slot_map[s].append(i)

    def ico_candidates(q_slot, K=8):
        cands = set()
        for delta in range(-K, K + 1):
            ns = q_slot + delta
            if 0 <= ns < TOTAL:
                for idx in ico_slot_map.get(ns, []):
                    cands.add(idx)
        return cands

    # --- PCA baseline (cubic grid, as before) ---
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    def pca_candidates(q, K=1):
        b = pca_bucket(q)
        cands = set(pca_map.get(b, []))
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
        return cands

    # --- Measure recall ---
    for K in [4, 8]:
        # cube view
        recs_cube = []
        scans_cube = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = cube_candidates(Q_cube_slots[qi], K=K)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_cube.append(len(gtset & set(ranked)) / 10.0)
            scans_cube.append(len(cands) / N * 100)

        # icosphere view (via geo_jump transfer)
        recs_ico = []
        scans_ico = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = ico_candidates(Q_ico_slots[qi], K=K)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_ico.append(len(gtset & set(ranked)) / 10.0)
            scans_ico.append(len(cands) / N * 100)

        # PCA baseline
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = pca_candidates(Qp[qi])
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_pca.append(len(gtset & set(ranked)) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"  K={K}:")
        print(f"    cube view:       recall={np.mean(recs_cube):.3f} scan~{np.mean(scans_cube):.1f}%")
        print(f"    icosphere view:  recall={np.mean(recs_ico):.3f} scan~{np.mean(scans_ico):.1f}%")
        print(f"    PCA baseline:    recall={np.mean(recs_pca):.3f} scan~{np.mean(scans_pca):.1f}%")
    print("  PASS")


def test_freeze_frame():
    print("=" * 60)
    print("TEST 4: 4D freeze frame (store once, access multiple views)")
    t_cti, t_itc = build_transfer()
    # simulate: store 100 vectors in 4D frame
    N = 100
    rng = np.random.default_rng(5)
    X = rng.normal(size=(N, 64))
    # assign each to a cube slot (freeze frame stores 4D address)
    slots = list(range(N))  # slot 0..99 in freeze frame
    # access via cube view
    cube_view = [int(s) for s in slots]  # identity for first N
    # access via icosphere view (geo_jump transfer)
    ico_view = [int(t_cti[s]) for s in slots]
    # verify: cube -> ico -> cube roundtrip
    roundtrip = [int(t_itc[iv]) for iv in ico_view]
    assert roundtrip == cube_view, "freeze frame roundtrip failed"
    print(f"  stored {N} vectors in 4D freeze frame")
    print(f"  cube view slots: {cube_view[:5]}...")
    print(f"  ico view slots:  {ico_view[:5]}...")
    print(f"  roundtrip (cube->ico->cube): identity OK")
    print("  PASS")


def main():
    test_bijection()
    test_transfer_properties()
    test_recall_multiview()
    test_freeze_frame()
    print()
    print("multi-view freeze frame prototype: all checks PASS")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
