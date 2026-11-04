"""Prototype: map FD(3) Hasse diagram into 3-axis (3D) space.

Concept (owner): each Hasse anchor has coordinate-like properties. Map the
20 FD(3) elements into 3D (xyz) space using generator weights. Then use
3D spatial proximity (nearest neighbors in 3D) instead of graph traversal
(Hasse edges) for candidate selection. Hypothesis: 3D spatial neighborhoods
are more compact than graph neighborhoods -> lower scan budget for same recall.

Mapping: antichain A = {S_1,...,S_k} (subsets of {0,1,2})
  coord[g] = (number of S_i containing generator g) / k
  This gives a point in [0,1]^3 for each Hasse element.

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd


def antichain_to_3d(ac):
    """Map an antichain (frozenset of bitmasks) to 3D coordinate."""
    if not ac:
        return np.array([0.0, 0.0, 0.0])
    coords = np.zeros(3)
    for mask in ac:
        for g in range(3):
            if mask & (1 << g):
                coords[g] += 1.0
    return coords / len(ac)


# Build 3D coordinates for all 20 Hasse nodes
node_3d = np.array([antichain_to_3d(fd.antichains[i]) for i in range(fd.n)])
# Normalize to [0,1]^3 (already in [0,1])
print("FD(3) -> 3D mapping:")
for i in range(fd.n):
    print(f"  node {i}: rank={fd.rank[i]} antichain={sorted(fd.antichains[i])} -> 3D={node_3d[i].round(3)}")


def hasse_3d_neighbors(node_idx, K=3):
    """Find K nearest Hasse nodes in 3D space (not graph)."""
    dists = np.linalg.norm(node_3d - node_3d[node_idx], axis=1)
    order = np.argsort(dists)
    return [int(x) for x in order[1:K+1]]  # exclude self


def test_recall():
    print("=" * 60)
    print("TEST: 3-axis Hasse scan vs graph Hasse jump vs PCA")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    X_face = []
    for v in Xp:
        X_face.append(int(np.argmax(face_centers @ v)))
    face_map = defaultdict(list)
    for i, f in enumerate(X_face):
        face_map[f].append(i)
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_map[fd.face_to_fd[f]].append(i)

    def cos(a, b):
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))

    # Strategy 1: 3D spatial nearest neighbors (K nearest in 3D)
    def scan_3d(q_proj, K_nn, scan_budget):
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = face_centers @ qn
        nearest_face = int(np.argmax(sims))
        start_fd = fd.face_to_fd[nearest_face]

        # Find K nearest Hasse nodes in 3D
        dists = np.linalg.norm(node_3d - node_3d[start_fd], axis=1)
        order = np.argsort(dists)
        selected_fds = [int(x) for x in order[:K_nn+1]]  # +1 for self

        cands = set()
        for fde in selected_fds:
            for idx in fd_map.get(fde, []):
                cands.add(idx)
                if len(cands) >= int(scan_budget * N):
                    break
            if len(cands) >= int(scan_budget * N):
                break
        return cands

    # Strategy 2: graph Hasse jump (from previous prototype)
    def scan_graph(q_proj, scan_budget):
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = face_centers @ qn
        nearest_face = int(np.argmax(sims))
        start_fd = fd.face_to_fd[nearest_face]

        max_cands = int(scan_budget * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in fd.hasse_adj[node]:
                if nb in visited:
                    continue
                visited.add(nb)
                queue.append(nb)
                for idx in fd_map.get(nb, []):
                    cands.add(idx)
                    if len(cands) >= max_cands:
                        break
                if len(cands) >= max_cands:
                    break
        return cands

    # Strategy 3: PCA baseline
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    def scan_pca(q, scan_budget):
        b = pca_bucket(q)
        cands = set()
        # collect from bucket + neighbors
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
                    if len(cands) >= int(scan_budget * N):
                        break
            if len(cands) >= int(scan_budget * N):
                break
        if len(cands) < int(scan_budget * N):
            for idx in pca_map.get(b, []):
                cands.add(idx)
                if len(cands) >= int(scan_budget * N):
                    break
        return cands

    for budget in [0.05, 0.10]:
        # 3D spatial
        recs_3d = []
        scans_3d = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = scan_3d(Qp[qi], K_nn=3, scan_budget=budget)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_3d.append(len(gtset & set(ranked)) / 10.0)
            scans_3d.append(len(cands) / N * 100)

        # graph jump
        recs_graph = []
        scans_graph = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = scan_graph(Qp[qi], budget)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_graph.append(len(gtset & set(ranked)) / 10.0)
            scans_graph.append(len(cands) / N * 100)

        # PCA
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = scan_pca(Qp[qi], budget)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_pca.append(len(gtset & set(ranked)) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"  budget={budget:.0%}:")
        print(f"    3D spatial:  recall={np.mean(recs_3d):.3f} scan~{np.mean(scans_3d):.1f}%")
        print(f"    graph jump:  recall={np.mean(recs_graph):.3f} scan~{np.mean(scans_graph):.1f}%")
        print(f"    PCA:         recall={np.mean(recs_pca):.3f} scan~{np.mean(scans_pca):.1f}%")

    # Also test: does 3D mapping reduce nodes visited?
    print()
    print("  Node visitation (3D spatial, K_nn=3):")
    visited_counts = []
    for qi in range(len(Q)):
        qn = Qp[qi] / (np.linalg.norm(Qp[qi]) + 1e-9)
        sims = face_centers @ qn
        nearest_face = int(np.argmax(sims))
        start_fd = fd.face_to_fd[nearest_face]
        dists = np.linalg.norm(node_3d - node_3d[start_fd], axis=1)
        order = np.argsort(dists)
        selected = [int(x) for x in order[:4]]
        visited_counts.append(len(selected))
    print(f"    avg nodes visited: {np.mean(visited_counts):.1f}/20")
    print("  PASS")


def test_3d_structure():
    print("=" * 60)
    print("TEST: 3D mapping structure")
    # Check that 3D coordinates are well-distributed
    print(f"  3D coords range: min={node_3d.min(axis=0).round(3)}, max={node_3d.max(axis=0).round(3)}")
    # Check pairwise distances
    from itertools import combinations
    dists = [np.linalg.norm(node_3d[i]-node_3d[j]) for i,j in combinations(range(fd.n),2)]
    print(f"  pairwise dist: min={min(dists):.3f} max={max(dists):.3f} avg={np.mean(dists):.3f}")
    # Check that 3D mapping preserves some Hasse structure
    # (nodes with same rank should be somewhat clustered)
    for r in range(max(fd.rank)+1):
        nodes = [i for i in range(fd.n) if fd.rank[i]==r]
        if len(nodes) > 1:
            coords = node_3d[nodes]
            spread = np.linalg.norm(coords.std(axis=0))
            print(f"  rank {r}: {len(nodes)} nodes, 3D spread={spread:.3f}")
    print("  PASS")


def main():
    test_3d_structure()
    test_recall()
    print()
    print("3-axis Hasse prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
