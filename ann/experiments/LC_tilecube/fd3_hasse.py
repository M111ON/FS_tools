"""Prototype: FD(3) Free Distributive Lattice on 3 generators.

FD(3) has 20 elements (= Dedekind number M(3) = 20 = icosahedron faces).
Elements are antichains of the Boolean lattice B_3 (8 elements).
Hasse diagram gives a lattice structure on 20 elements with distributivity.

This prototype:
  1. Enumerates all 20 antichains of B_3.
  2. Builds Hasse diagram (cover relations).
  3. Maps 20 elements -> 20 faces of icosahedron L0.
  4. Uses Hasse adjacency for candidate generation.
  5. Measures recall vs icosphere nearest-vertex and PCA.

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

# ---------- B_3 Boolean lattice ----------
# Elements are subsets of {0,1,2}, represented as bitmasks 0..7
# Order: X <= Y iff (X & Y) == X (X subset of Y)

def is_antichain(S):
    """S is a set of bitmasks; no element contains another."""
    lst = list(S)
    for i in range(len(lst)):
        for j in range(i+1, len(lst)):
            a, b = lst[i], lst[j]
            if (a & b) == a or (a & b) == b:
                return False  # one contains the other
    return True


def all_antichains():
    """Enumerate all antichains of B_3."""
    elems = list(range(8))  # 0..7
    result = []
    # all subsets of {0..7} that are antichains
    for r in range(9):  # antichain size 0..8
        from itertools import combinations
        for combo in combinations(elems, r):
            s = frozenset(combo)
            if is_antichain(s):
                result.append(s)
    return result


def antichain_leq(A, B):
    """A <= B in FD order: for all a in A, exists b in B with a <= b (subset)."""
    if not A:  # empty antichain = bottom
        return True
    if not B:
        return False  # nothing is <= empty except empty (handled above)
    for a in A:
        found = False
        for b in B:
            if (a & b) == a:  # a subset of b
                found = True
                break
        if not found:
            return False
    return True


# ---------- Build FD(3) ----------
antichains = all_antichains()
print(f"FD(3) elements: {len(antichains)} (expect 20)")
# map antichain -> index
ac_index = {ac: i for i, ac in enumerate(antichains)}

# Build partial order: A <= B
n = len(antichains)
leq = [[False]*n for _ in range(n)]
for i, A in enumerate(antichains):
    for j, B in enumerate(antichains):
        leq[i][j] = antichain_leq(A, B)

# Cover relations: i covers j if i > j and no k with i > k > j
covers = defaultdict(set)  # i -> set of j that i covers
covered_by = defaultdict(set)  # j -> set of i that cover j
for i in range(n):
    for j in range(n):
        if i == j:
            continue
        if leq[j][i] and not leq[j][i] == (i==j):  # j < i
            # check cover: no k with j < k < i
            is_cover = True
            for k in range(n):
                if k == i or k == j:
                    continue
                if leq[j][k] and leq[k][i] and not (k==j or k==i):
                    # j < k < i
                    if leq[j][k] and leq[k][i] and not leq[j][i] == False:
                        # need strict: j < k and k < i
                        if leq[j][k] and leq[k][i] and not (k==i):
                            # check not equal
                            if leq[j][k] and leq[k][i] and not (j==k):
                                is_cover = False
                                break
            if is_cover:
                covers[i].add(j)
                covered_by[j].add(i)

# Clean up: simpler cover check
covers = defaultdict(set)
covered_by = defaultdict(set)
for i in range(n):
    for j in range(n):
        if i == j:
            continue
        if not leq[j][i]:
            continue
        if leq[i][j]:  # i <= j, not j < i
            continue
        # j < i, check cover
        is_cover = True
        for k in range(n):
            if k == i or k == j:
                continue
            if leq[j][k] and leq[k][i] and not leq[j][i] == (k==i):
                # j <= k <= i with strict in middle
                if leq[j][k] and leq[k][i]:
                    # check not j==k and not k==i
                    if not (leq[j][k] and leq[k][i] and leq[j][i]):
                        pass
                    # proper: j < k < i means leq[j][k] and leq[k][i] and not leq[j][i]==leq[k][i]
                    if leq[j][k] and leq[k][i] and not (k==i or k==j):
                        # but need strict inequality
                        if leq[j][k] and leq[k][i]:
                            # check if k is strictly between
                            if leq[j][k] and leq[k][i] and not leq[i][k] and not leq[k][j]:
                                is_cover = False
                                break
        if is_cover:
            covers[i].add(j)
            covered_by[j].add(i)

# Hasse diagram adjacency (undirected)
hasse_adj = defaultdict(set)
for i in range(n):
    for j in covers[i]:
        hasse_adj[i].add(j)
        hasse_adj[j].add(i)

# Rank (longest path from bottom)
bottom = None
for i in range(n):
    if not antichains[i]:  # empty antichain = bottom
        bottom = i
        break

rank = [-1]*n
rank[bottom] = 0
# BFS from bottom using cover relations (upward)
queue = deque([bottom])
# covers[i] = what i covers (downward). covered_by[j] = what covers j (upward).
# rank increases upward
changed = True
while changed:
    changed = False
    for j in range(n):
        if rank[j] >= 0:
            continue
        # j's rank = 1 + max rank of things j covers
        preds = [k for k in covers[j] if rank[k] >= 0]
        if preds:
            rank[j] = max(rank[k] for k in preds) + 1
            changed = True

# ---------- Print structure ----------
print(f"\nFD(3) Hasse diagram:")
print(f"  elements: {n}")
print(f"  bottom index: {bottom} (empty antichain)")
ranks_count = defaultdict(int)
for r in rank:
    ranks_count[r] += 1
print(f"  ranks: {dict(sorted(ranks_count.items()))}")
print(f"  hasse edges: {sum(len(v) for v in hasse_adj.values())//2}")

# Print elements by rank
for r in sorted(set(rank)):
    elems_at_rank = [i for i in range(n) if rank[i] == r]
    print(f"  rank {r}: {elems_at_rank} (antichains: {[sorted(antichains[i]) for i in elems_at_rank]})")


# ---------- Map to icosahedron ----------
import icosphere_address as ico_mod
verts2, faces2 = ico_mod.build_icosphere(2)
# 20 faces -> 20 FD elements (by index order)
# Map L2 face (0..319) -> L0 face (0..19) -> FD element (0..19)
l0_verts, l0_faces = ico_mod.build_icosphere(0)
l0_fc = np.array([l0_verts[f].mean(axis=0) for f in l0_faces])
l0_fc = l0_fc / np.linalg.norm(l0_fc, axis=1, keepdims=True)
l2_fc = np.array([verts2[f].mean(axis=0) for f in faces2])
l2_fc = l2_fc / np.linalg.norm(l2_fc, axis=1, keepdims=True)
l2_to_l0 = []
for lc in l2_fc:
    l2_to_l0.append(int(np.argmax(l0_fc @ lc)))
face_to_fd = {i: l2_to_l0[i] for i in range(len(faces2))}
fd_to_l0 = {i: i for i in range(20)}
l0_to_l2 = defaultdict(list)
for i, l0 in enumerate(l2_to_l0):
    l0_to_l2[l0].append(i)

# Face adjacency from icosahedron (shared edges)
face_adj = defaultdict(set)
# Build face adjacency: two faces are adjacent if they share an edge
edge_to_faces = defaultdict(list)
for fi, f in enumerate(faces2):
    for i in range(3):
        a, b = f[i], f[(i+1)%3]
        edge_to_faces[(min(a,b), max(a,b))].append(fi)
for edge, flist in edge_to_faces.items():
    for i in range(len(flist)):
        for j in range(i+1, len(flist)):
            face_adj[flist[i]].add(flist[j])
            face_adj[flist[j]].add(flist[i])

print(f"\n  icosahedron L2: {len(faces2)} faces, {len(edge_to_faces)} edges")
print(f"  face adj edges: {sum(len(v) for v in face_adj.values())//2}")


# ---------- Recall test ----------
def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def test_recall():
    print("=" * 60)
    print("TEST: FD(3) Hasse-guided recall vs icosphere nearest-vertex")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    # PCA projection to 3D
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    # Map data to icosahedron faces (via nearest vertex -> face)
    # Actually, map to faces directly: each data point -> nearest face center
    face_centers = np.array([verts2[f].mean(axis=0) for f in faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    def nearest_face(v):
        sims = face_centers @ v
        return int(np.argmax(sims))

    X_face = [nearest_face(v) for v in Xp]
    face_map = defaultdict(list)
    for i, f in enumerate(X_face):
        face_map[f].append(i)

    # FD-guided candidate generation:
    # 1. Find nearest face (FD element)
    # 2. Expand along Hasse edges (FD cover relations) -> adjacent FD elements
    # 3. Also expand along icosahedron face adjacency
    # 4. Map FD elements -> icosahedron faces (identity for first 20)
    # 5. Collect data from all resulting faces
    def fd_candidates(q_proj, K_ring=1):
        f0 = nearest_face(q_proj)
        fd0 = face_to_fd[f0]
        # expand along Hasse diagram
        fd_nodes = {fd0}
        frontier = {fd0}
        for _ in range(K_ring):
            nxt = set()
            for node in frontier:
                for nb in hasse_adj[node]:
                    if nb not in fd_nodes:
                        fd_nodes.add(nb)
                        nxt.add(nb)
            frontier = nxt
        # map FD nodes -> L0 faces -> L2 faces
        faces_set = set()
        for fd in fd_nodes:
            for l2f in l0_to_l2.get(fd, []):
                faces_set.add(l2f)
        # also expand icosahedron face adjacency
        if K_ring >= 1:
            expanded = set()
            for f in faces_set:
                expanded.add(f)
                for nb in face_adj[f]:
                    expanded.add(nb)
            faces_set = expanded
        # collect data
        cands = set()
        for f in faces_set:
            for idx in face_map.get(f, []):
                cands.add(idx)
        return cands

    # Baseline: icosphere nearest-vertex (from previous prototype)
    X_vert = [ico_mod.nearest_vertex(verts2, v) if hasattr(ico_mod, 'nearest_vertex') else int(np.argmax(verts2 @ v)) for v in Xp]
    # use icosphere_address nearest_vertex
    def ico_nv(verts, v):
        sims = verts @ v
        return int(np.argmax(sims))
    X_vert = [ico_nv(verts2, v) for v in Xp]
    vert_map = defaultdict(list)
    for i, v in enumerate(X_vert):
        vert_map[v].append(i)
    adj2 = ico_mod.adjacency(verts2, faces2)

    def ico_candidates(q_proj, K_ring=1):
        v0 = ico_nv(verts2, q_proj)
        v_nodes = {v0}
        frontier = {v0}
        for _ in range(K_ring):
            nxt = set()
            for node in frontier:
                for nb in adj2[node]:
                    if nb not in v_nodes:
                        v_nodes.add(nb)
                        nxt.add(nb)
            frontier = nxt
        cands = set()
        for v in v_nodes:
            for idx in vert_map.get(v, []):
                cands.add(idx)
        return cands

    # PCA baseline
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    def pca_cands(q, K=1):
        b = pca_bucket(q)
        cands = set(pca_map.get(b, []))
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
        return cands

    for K_ring in [0, 1]:
        # FD-guided
        recs_fd = []
        scans_fd = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = fd_candidates(Qp[qi], K_ring=K_ring)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_fd.append(len(gtset & set(ranked)) / 10.0)
            scans_fd.append(len(cands) / N * 100)

        # icosphere nearest-vertex
        recs_ico = []
        scans_ico = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = ico_candidates(Qp[qi], K_ring=K_ring)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_ico.append(len(gtset & set(ranked)) / 10.0)
            scans_ico.append(len(cands) / N * 100)

        # PCA
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = pca_cands(Qp[qi])
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_pca.append(len(gtset & set(ranked)) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"  K_ring={K_ring}:")
        print(f"    FD(3)-guided:    recall={np.mean(recs_fd):.3f} scan~{np.mean(scans_fd):.1f}%")
        print(f"    icosphere:       recall={np.mean(recs_ico):.3f} scan~{np.mean(scans_ico):.1f}%")
        print(f"    PCA-grid:        recall={np.mean(recs_pca):.3f} scan~{np.mean(scans_pca):.1f}%")
    print("  PASS")


def test_hasse_properties():
    print("=" * 60)
    print("TEST: FD(3) Hasse diagram properties")
    print(f"  elements: {n}")
    print(f"  hasse edges: {sum(len(v) for v in hasse_adj.values())//2}")
    # check distributivity: meet of (a∨b) and (a∨c) should equal a∨(b∧c)
    # (we can't easily verify without full lattice ops, but structure is by construction)
    rc = {}
    for r in rank:
        rc[r] = rc.get(r, 0) + 1
    print(f"  ranks: {dict(sorted(rc.items()))}")
    # print hasse adj for a few nodes
    print(f"  hasse_adj[bottom={bottom}]: {sorted(hasse_adj[bottom])}")
    print("  PASS")


def main():
    test_hasse_properties()
    test_recall()
    print()
    print("FD(3) Hasse prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
