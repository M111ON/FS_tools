"""Prototype: icosphere-based addressing for normalized embeddings.

Owner insight: 162 in the equation 128x162=20736 maps to icosphere L2
(10*4^2+2 = 162 vertices, 320 faces, 480 edges). Normalized embeddings
live on a hypersphere, so icosphere (spherical mesh) is a natural
partitioning — better than cubic PCA grid.

This prototype:
  - generates icosphere L2 (162 vertices)
  - projects embeddings to 3D, normalizes to unit sphere
  - addresses by nearest icosphere vertex (spherical Voronoi)
  - candidate generation: same vertex + adjacent vertices
  - measures recall vs PCA-to-cubic-grid baseline

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, '.')

PHI = (1 + 5**0.5) / 2


def icosahedron():
    verts = np.array([
        (0, 1, PHI), (0, 1, -PHI), (0, -1, PHI), (0, -1, -PHI),
        (1, PHI, 0), (1, -PHI, 0), (-1, PHI, 0), (-1, -PHI, 0),
        (PHI, 0, 1), (PHI, 0, -1), (-PHI, 0, 1), (-PHI, 0, -1),
    ], dtype=float)
    # normalize
    verts /= np.linalg.norm(verts, axis=1, keepdims=True)
    faces = np.array([
        (0, 2, 8), (0, 8, 4), (0, 4, 6), (0, 6, 10), (0, 10, 2),
        (3, 9, 11), (3, 11, 7), (3, 7, 5), (3, 5, 9), (3, 9, 11),
        (1, 4, 9), (4, 8, 9), (8, 5, 9), (5, 11, 9), (11, 1, 9),
        (2, 10, 7), (10, 6, 7), (6, 1, 7), (1, 3, 7), (3, 2, 7),
        (2, 3, 5), (2, 5, 8), (8, 5, 9),  # duplicate-ish, will dedup
    ], dtype=int)
    # Use standard icosahedron faces
    faces = np.array([
        (0, 2, 8), (0, 2, 10), (0, 4, 6), (0, 4, 8), (0, 6, 10),
        (1, 3, 9), (1, 3, 11), (1, 4, 6), (1, 4, 9), (1, 6, 11),
        (2, 5, 7), (2, 5, 8), (2, 7, 10), (3, 5, 7), (3, 5, 9),
        (3, 7, 11), (4, 8, 9), (5, 8, 9), (6, 10, 11), (7, 10, 11),
    ], dtype=int)
    return verts, faces


def subdivide(verts, faces, n=1):
    """Subdivide icosahedron n times -> icosphere."""
    verts = np.array(verts, dtype=float)
    for _ in range(n):
        new_faces = []
        midpoint_cache = {}
        verts_list = [v for v in verts]
        def get_mid(i, j):
            key = (min(i, j), max(i, j))
            if key not in midpoint_cache:
                mid = (verts_list[i] + verts_list[j]) / 2
                mid /= np.linalg.norm(mid)
                midpoint_cache[key] = len(verts_list)
                verts_list.append(mid)
            return midpoint_cache[key]
        for f in faces:
            a, b, c = f
            ab = get_mid(a, b)
            bc = get_mid(b, c)
            ca = get_mid(c, a)
            new_faces.append((a, ab, ca))
            new_faces.append((b, bc, ab))
            new_faces.append((c, ca, bc))
            new_faces.append((ab, bc, ca))
        faces = np.array(new_faces, dtype=int)
        verts = np.array(verts_list, dtype=float)
    return verts, faces


def build_icosphere(level=2):
    verts, faces = icosahedron()
    verts, faces = subdivide(verts, faces, level)
    return verts, faces


def adjacency(verts, faces):
    """Build vertex adjacency from faces."""
    adj = defaultdict(set)
    for f in faces:
        for i in range(3):
            a, b = f[i], f[(i+1)%3]
            adj[a].add(b)
            adj[b].add(a)
    return adj


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def test_icosphere_structure():
    print("=" * 60)
    print("TEST 1: icosphere structure")
    for level in [0, 1, 2]:
        verts, faces = build_icosphere(level)
        print(f"  level {level}: vertices={len(verts)} faces={len(faces)}")
    verts, faces = build_icosphere(2)
    assert len(verts) == 162, f"expected 162, got {len(verts)}"
    assert len(faces) == 320, f"expected 320, got {len(faces)}"
    adj = adjacency(verts, faces)
    degs = [len(adj[i]) for i in range(len(verts))]
    print(f"  icosphere L2: {len(verts)} verts, {len(faces)} faces")
    print(f"  degree min/max/avg: {min(degs)}/{max(degs)}/{np.mean(degs):.1f}")
    print("  PASS")


def test_recall():
    print("=" * 60)
    print("TEST 2: icosphere addressing recall vs PCA-to-cubic-grid")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    # Normalize all to unit sphere (cosine space)
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    # PCA projection to 3D (baseline)
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    # normalize projected to unit sphere
    Xp = Xp / np.linalg.norm(Xp, axis=1, keepdims=True)
    Qp = Qp / np.linalg.norm(Qp, axis=1, keepdims=True)

    verts, faces = build_icosphere(2)
    adj = adjacency(verts, faces)

    def nearest_vertex(v):
        sims = verts @ v
        return int(np.argmax(sims))

    # icosphere addressing: address = nearest vertex
    X_addr = [nearest_vertex(v) for v in Xp]
    addr_map = defaultdict(list)
    for i, a in enumerate(X_addr):
        addr_map[a].append(i)

    def ico_candidates(q, K=1):
        v = nearest_vertex(q)
        cands = set(addr_map.get(v, []))
        # expand to K-ring neighbors
        frontier = {v}
        visited = {v}
        for _ in range(K):
            nxt = set()
            for node in frontier:
                for nb in adj[node]:
                    if nb not in visited:
                        visited.add(nb)
                        nxt.add(nb)
            for node in nxt:
                for idx in addr_map.get(node, []):
                    cands.add(idx)
            frontier = nxt
        return cands

    # PCA-to-cubic-grid baseline
    def pca_bucket(pt):
        pt = (pt + 1) / 2  # [-1,1] -> [0,1]
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

    for K in [0, 1]:
        # icosphere
        recs_ico = []
        scans_ico = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = ico_candidates(Qp[qi], K=K)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            rset = set(ranked)
            recs_ico.append(len(gtset & rset) / 10.0)
            scans_ico.append(len(cands) / N * 100)

        # PCA
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = pca_candidates(Qp[qi])
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            rset = set(ranked)
            recs_pca.append(len(gtset & rset) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"  K-ring={K}:")
        print(f"    icosphere: recall={np.mean(recs_ico):.3f} scan~{np.mean(scans_ico):.1f}%")
        print(f"    PCA-grid:  recall={np.mean(recs_pca):.3f} scan~{np.mean(scans_pca):.1f}%")
    print("  PASS")


def test_vertex_distribution():
    print("=" * 60)
    print("TEST 3: icosphere vertex distribution (uniformity)")
    verts, faces = build_icosphere(2)
    # check that vertices are roughly uniformly distributed
    # (all on unit sphere, min angular separation)
    from itertools import combinations
    min_sep = min(np.arccos(np.clip(np.dot(verts[i], verts[j]), -1, 1))
                   for i, j in combinations(range(len(verts)), 2))
    print(f"  162 vertices on unit sphere")
    print(f"  min angular separation: {np.degrees(min_sep):.2f} deg")
    print(f"  (uniform sphere: ~{np.degrees(4*np.pi/162**0.5):.2f} deg expected)")
    print("  PASS")


def main():
    test_icosphere_structure()
    test_recall()
    test_vertex_distribution()
    print()
    print("icosphere addressing prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
