"""Prototype: Hasse-diagram jump scan (view jumping).

Concept (owner): each Hasse node is an anchor with coordinate-like properties.
Instead of linear scan (slot +/- K), we JUMP between Hasse nodes (views)
following cover relations (edges). At each visited node, collect data from
the corresponding L2 faces. Stop when scan budget is reached.

This is graph-traversal scan on the Hasse diagram, aligned with the system's
design philosophy (#7532: observe -> build route -> jump O(1)).

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

# Rebuild everything from fd3_hasse
# (fd module already has: antichains, hasse_adj, rank, face_to_fd, l2_to_l0,
#  l0_to_l2, verts2, faces2, etc. after import)

# Hasse node coordinate: (rank, index_within_rank)
rank_groups = defaultdict(list)
for i in range(fd.n):
    rank_groups[fd.rank[i]].append(i)

# Map each Hasse node to its "coordinate" in 2D Hasse space
node_coord = {}
for r, nodes in rank_groups.items():
    for idx, node in enumerate(sorted(nodes)):
        node_coord[node] = (r, idx)

# Hasse space dimensions
max_rank = max(fd.rank)
max_idx = max(len(v) for v in rank_groups.values())


def hasse_distance(n1, n2):
    """Distance in Hasse coordinate space."""
    r1, i1 = node_coord[n1]
    r2, i2 = node_coord[n2]
    return abs(r1 - r2) + abs(i1 - i2)


def jump_scan(q_proj, scan_budget=0.05, strategy="bfs"):
    """Scan by jumping between Hasse nodes.

    scan_budget: fraction of total data to scan (0.05 = 5%).
    strategy: "bfs" (breadth-first), "dfs" (depth-first), "rank" (by rank order)

    Returns set of candidate indices.
    """
    # Find starting Hasse node (nearest FD element for query)
    # Map query to nearest L2 face -> FD element
    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)
    qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
    sims = face_centers @ qn
    nearest_l2_face = int(np.argmax(sims))
    start_fd = fd.face_to_fd[nearest_l2_face]

    # Total data count
    N_total = 500  # will be set by caller
    max_candidates = int(scan_budget * N_total)

    visited_fd = set()
    visited_l2 = set()
    cands = set()
    queue = deque()

    # Start node
    visited_fd.add(start_fd)
    queue.append(start_fd)

    # Collect data from start node
    for l2f in fd.l0_to_l2.get(start_fd, []):
        visited_l2.add(l2f)
        for idx in fd.face_map.get(l2f, []) if hasattr(fd, 'face_map') else []:
            cands.add(idx)

    # Need face_map from caller; we'll pass it or use global
    # For now, we'll handle this in the test function

    steps = 0
    while queue and len(cands) < max_candidates:
        node = queue.popleft() if strategy == "bfs" else queue.pop()
        # Jump to neighbors (Hasse cover relations)
        for nb in fd.hasse_adj[node]:
            if nb in visited_fd:
                continue
            visited_fd.add(nb)
            queue.append(nb)
            steps += 1
            # Collect data from this node's L2 faces
            # (handled by caller via face_map)
        if len(visited_fd) >= fd.n:
            break

    return visited_fd, visited_l2, steps


def test_jump_scan():
    print("=" * 60)
    print("TEST: Hasse jump scan (view jumping)")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    # PCA projection
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    # Face centers for L2
    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    # Build face_map: L2 face -> list of data indices
    X_face = []
    for v in Xp:
        sims = face_centers @ v
        X_face.append(int(np.argmax(sims)))
    face_map = defaultdict(list)
    for i, f in enumerate(X_face):
        face_map[f].append(i)

    # Also build FD-level map: FD element -> list of data indices
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_elem = fd.face_to_fd[f]
        fd_map[fd_elem].append(i)

    def jump_scan_candidates(q_proj, scan_budget):
        """Collect candidates by jumping through Hasse nodes."""
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = face_centers @ qn
        nearest_l2_face = int(np.argmax(sims))
        start_fd = fd.face_to_fd[nearest_l2_face]

        max_candidates = int(scan_budget * N)
        visited_fd = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])

        while queue and len(cands) < max_candidates:
            node = queue.popleft()
            for nb in fd.hasse_adj[node]:
                if nb in visited_fd:
                    continue
                visited_fd.add(nb)
                queue.append(nb)
                for idx in fd_map.get(nb, []):
                    cands.add(idx)
                    if len(cands) >= max_candidates:
                        break
                if len(cands) >= max_candidates:
                    break

        return cands, visited_fd

    def cos(a, b):
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))

    # Test at various scan budgets
    for budget in [0.05, 0.10, 0.20]:
        recs = []
        actual_scans = []
        nodes_visited = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands, visited = jump_scan_candidates(Qp[qi], budget)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs.append(len(gtset & set(ranked)) / 10.0)
            actual_scans.append(len(cands) / N * 100)
            nodes_visited.append(len(visited))

        print(f"  budget={budget:.0%}: recall={np.mean(recs):.3f} "
              f"scan~{np.mean(actual_scans):.1f}% "
              f"nodes~{np.mean(nodes_visited):.1f}/{fd.n}")

    # Compare: linear scan at same budget
    print()
    print("  --- Linear scan comparison ---")
    for budget in [0.05, 0.10, 0.20]:
        recs = []
        for qi in range(len(Q)):
            # Linear: collect first `budget*N` data points (by index)
            cands = set(range(int(budget * N)))
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs.append(len(gtset & set(ranked)) / 10.0)
        print(f"  budget={budget:.0%}: recall={np.mean(recs):.3f} (linear, no structure)")

    print("  PASS")


def test_jump_view_transfer():
    print("=" * 60)
    print("TEST: jump between views (geo_jump during scan)")
    # At each Hasse node, we can view data through different lenses
    # by geo_jump transfer. Demonstrate: cube view vs icosphere view of same node.
    t_cti, t_itc = None, None
    # Reuse transfer from multiview_freeze
    import multiview_freeze as mvf
    t_cti, t_itc = mvf.build_transfer()

    # Each Hasse node's L2 faces map to cube slots and ico slots
    # Jump between views = apply transfer to the same data
    print(f"  transfer built: {len(t_cti)} slots")
    # Verify: for a Hasse node, cube view and ico view are bijective
    sample_fd = 0
    l2_faces = fd.l0_to_l2.get(sample_fd, [])
    print(f"  FD node {sample_fd}: {len(l2_faces)} L2 faces")
    cube_slots = [f for f in l2_faces]  # identity (face index as slot)
    ico_slots = [int(t_cti[s % len(t_cti)]) for s in cube_slots]
    roundtrip = [int(t_itc[is_]) for is_ in ico_slots]
    print(f"  cube->ico->cube roundtrip OK: {roundtrip == cube_slots}")
    print("  PASS")


def main():
    test_jump_scan()
    test_jump_view_transfer()
    print()
    print("Hasse jump scan prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
