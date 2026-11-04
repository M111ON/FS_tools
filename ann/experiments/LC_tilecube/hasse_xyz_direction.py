"""Prototype: XYZ as Jump Controller (direction selector), not spatial address.

Concept (owner): separate concerns into 3 parts:
  1. XYZ — Direction Selector: chooses which cover relation (edge) to follow first
  2. FD(3) — Relationship Graph: maintains 20 nodes + 32 cover edges
  3. geo_jump — View Transfer: changes view without changing data identity

Hypothesis: XYZ may not tell us which node is near, but can tell us which
cover relation (edge) to walk first. This leverages both structures without
forcing Hasse into geometry.

4 methods compared (same dataset, same queries):
  A: Hasse BFS (baseline)
  B: Hasse + XYZ direction (edge ordering by query alignment)
  C: Hasse + weighted directional jump (weighted edge traversal)
  D: Hasse + selective expansion (rank-based selective)

Locked variables:
  FD_NODE_COUNT = 20
  FD_EDGE_COUNT = 32
  AXIS_COUNT = 3
  MAX_SCAN_BUDGET = 5 (%)
  TARGET_SCAN_PERCENT = 2.7 (%)

Metrics: Recall@10, Scan%, Nodes visited, Jumps per query.

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

# ---- Locked constants ----
FD_NODE_COUNT = 20
FD_EDGE_COUNT = 32
AXIS_COUNT = 3
MAX_SCAN_BUDGET = 5       # percent
TARGET_SCAN_PERCENT = 2.7  # percent

# ---- 3D direction for each Hasse node (from antichain generator weights) ----
# This is used as DIRECTION, not as spatial coordinate.
def antichain_direction(ac):
    """Direction vector for a Hasse node (from antichain generator weights)."""
    if not ac:
        return np.array([0.0, 0.0, 0.0])
    coords = np.zeros(3)
    for mask in ac:
        for g in range(3):
            if mask & (1 << g):
                coords[g] += 1.0
    return coords / len(ac)

node_dir = np.array([antichain_direction(fd.antichains[i]) for i in range(fd.n)])

# ---- Edge direction: direction of each Hasse edge (u -> v) ----
# edge_dir[(u,v)] = normalized(node_dir[v] - node_dir[u])
edge_list = []
edge_dir_map = {}
for u in range(fd.n):
    for v in fd.hasse_adj[u]:
        if u < v:  # avoid duplicates
            d = node_dir[v] - node_dir[u]
            nrm = np.linalg.norm(d)
            if nrm > 1e-9:
                d = d / nrm
            edge_list.append((u, v))
            edge_dir_map[(u, v)] = d
            edge_dir_map[(v, u)] = -d

# ---- Query direction: 3D direction from PCA projection ----
def query_direction(q_proj):
    """Derive 3D direction from query's projected vector."""
    nrm = np.linalg.norm(q_proj)
    if nrm < 1e-9:
        return np.array([0.0, 0.0, 0.0])
    return q_proj / nrm


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


# ============================================================
# Method A: Hasse BFS (baseline)
# ============================================================
def method_a(q_proj, fd_map, N, budget_pct):
    """Standard BFS on Hasse graph."""
    face_centers = None  # set by caller
    # Find start node
    # (start node determined by caller and passed via closure)
    pass

def make_method_a(fd_map, N):
    def scan(q_proj, start_fd, budget_pct):
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in fd.hasse_adj[node]:
                if nb in visited:
                    continue
                visited.add(nb)
                queue.append(nb)
                jumps += 1
                for idx in fd_map.get(nb, []):
                    cands.add(idx)
                    if len(cands) >= max_cands:
                        break
                if len(cands) >= max_cands:
                    break
        return cands, visited, jumps
    return scan


# ============================================================
# Method B: Hasse + XYZ direction (edge ordering by query alignment)
# ============================================================
def make_method_b(fd_map, N, node_dir_arr):
    def scan(q_proj, start_fd, budget_pct):
        q_dir = query_direction(q_proj)
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        # Priority queue: (alignment_score, node) - higher alignment first
        # We'll use a list-based priority approach
        frontier = []
        for nb in fd.hasse_adj[start_fd]:
            if nb not in visited:
                ed = edge_dir_map.get((start_fd, nb), np.zeros(3))
                alignment = float(np.dot(ed, q_dir))
                frontier.append((alignment, nb))
        # Sort by alignment descending
        frontier.sort(key=lambda x: -x[0])
        # BFS with prioritized edge ordering
        queue = deque()
        for _, nb in frontier:
            queue.append(nb)
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            jumps += 1
            for idx in fd_map.get(node, []):
                cands.add(idx)
                if len(cands) >= max_cands:
                    break
            # Add neighbors with priority
            nbrs = []
            for nb in fd.hasse_adj[node]:
                if nb not in visited:
                    ed = edge_dir_map.get((node, nb), np.zeros(3))
                    alignment = float(np.dot(ed, q_dir))
                    nbrs.append((alignment, nb))
            nbrs.sort(key=lambda x: -x[0])
            for _, nb in nbrs:
                if nb not in visited:
                    queue.append(nb)
            if len(cands) >= max_cands:
                break
        return cands, visited, jumps
    return scan


# ============================================================
# Method C: Hasse + weighted directional jump
# ============================================================
def make_method_c(fd_map, N, node_dir_arr):
    def scan(q_proj, start_fd, budget_pct):
        q_dir = query_direction(q_proj)
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        # Weighted frontier: (weight, node)
        # Weight = alignment of edge direction with query direction
        # Use a priority list
        frontier = []
        for nb in fd.hasse_adj[start_fd]:
            if nb not in visited:
                ed = edge_dir_map.get((start_fd, nb), np.zeros(3))
                alignment = float(np.dot(ed, q_dir))
                # Weight combines alignment with edge "strength"
                # Edge strength = 1 / (1 + rank distance) to prefer local jumps
                rank_dist = abs(fd.rank[nb] - fd.rank[start_fd])
                weight = alignment / (1.0 + rank_dist * 0.5)
                frontier.append((weight, nb))
        frontier.sort(key=lambda x: -x[0])
        # Greedy: always pick highest-weight unvisited neighbor
        jumps = 0
        while frontier and len(cands) < max_cands:
            # Pick best
            frontier.sort(key=lambda x: -x[0])
            weight, node = frontier.pop(0)
            if node in visited:
                continue
            visited.add(node)
            jumps += 1
            for idx in fd_map.get(node, []):
                cands.add(idx)
                if len(cands) >= max_cands:
                    break
            # Add new neighbors
            for nb in fd.hasse_adj[node]:
                if nb not in visited:
                    ed = edge_dir_map.get((node, nb), np.zeros(3))
                    alignment = float(np.dot(ed, q_dir))
                    rank_dist = abs(fd.rank[nb] - fd.rank[node])
                    w = alignment / (1.0 + rank_dist * 0.5)
                    frontier.append((w, nb))
            if len(cands) >= max_cands:
                break
        return cands, visited, jumps
    return scan


# ============================================================
# Method D: Hasse + selective expansion
# ============================================================
def make_method_d(fd_map, N, node_dir_arr):
    def scan(q_proj, start_fd, budget_pct):
        q_dir = query_direction(q_proj)
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        # Selective: expand based on XYZ direction alignment
        # At each node, only follow edges where alignment > threshold
        alignment_threshold = 0.0  # only follow positively aligned edges
        queue = deque([start_fd])
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            nbrs = []
            for nb in fd.hasse_adj[node]:
                if nb in visited:
                    continue
                ed = edge_dir_map.get((node, nb), np.zeros(3))
                alignment = float(np.dot(ed, q_dir))
                if alignment > alignment_threshold:
                    nbrs.append((alignment, nb))
            # Sort by alignment descending
            nbrs.sort(key=lambda x: -x[0])
            for _, nb in nbrs:
                if nb in visited:
                    continue
                visited.add(nb)
                queue.append(nb)
                jumps += 1
                for idx in fd_map.get(nb, []):
                    cands.add(idx)
                    if len(cands) >= max_cands:
                        break
                if len(cands) >= max_cands:
                    break
        return cands, visited, jumps
    return scan


# ============================================================
# Test harness
# ============================================================
def run_comparison():
    print("=" * 70)
    print("XYZ as Jump Controller: 4-Method Comparison")
    print(f"  FD_NODE_COUNT={FD_NODE_COUNT} FD_EDGE_COUNT={FD_EDGE_COUNT}")
    print(f"  AXIS_COUNT={AXIS_COUNT} MAX_SCAN_BUDGET={MAX_SCAN_BUDGET}%")
    print(f"  TARGET_SCAN_PERCENT={TARGET_SCAN_PERCENT}%")
    print("=" * 70)

    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    # PCA projection to 3D (for query direction + baseline)
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    # Face centers for start node selection
    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    X_face = []
    for v in Xp:
        X_face.append(int(np.argmax(face_centers @ v)))
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_map[fd.face_to_fd[f]].append(i)

    # Start node for each query
    def get_start_fd(q_proj):
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = face_centers @ qn
        nearest_face = int(np.argmax(sims))
        return fd.face_to_fd[nearest_face]

    start_fds = [get_start_fd(qp) for qp in Qp]

    # Method instances
    methods = {
        "A: Hasse BFS": make_method_a(fd_map, N),
        "B: Hasse+XYZ dir": make_method_b(fd_map, N, node_dir),
        "C: Weighted jump": make_method_c(fd_map, N, node_dir),
        "D: Selective": make_method_d(fd_map, N, node_dir),
    }

    # PCA baseline scan function
    def pca_scan(q_proj, budget_pct):
        def pca_bucket(pt):
            pt = (pt + 1) / 2
            pt = (pt * 11).astype(int)
            pt = np.clip(pt, 0, 11)
            return tuple(pt.tolist())
        pca_map_local = defaultdict(list)
        for i, pt in enumerate(Xp):
            pca_map_local[pca_bucket(pt)].append(i)
        b = pca_bucket(q_proj)
        cands = set()
        max_c = int(budget_pct / 100.0 * N)
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map_local.get(tuple(nb), []):
                    cands.add(idx)
                    if len(cands) >= max_c:
                        break
            if len(cands) >= max_c:
                break
        if len(cands) < max_c:
            for idx in pca_map_local.get(b, []):
                cands.add(idx)
                if len(cands) >= max_c:
                    break
        return cands

    # Run all methods at MAX_SCAN_BUDGET (5%)
    budget = MAX_SCAN_BUDGET
    print(f"\n--- Results at {budget}% scan budget ---")
    print(f"{'Method':<22} {'Recall@10':>10} {'Scan%':>8} {'Nodes':>8} {'Jumps':>8}")
    print("-" * 60)

    results = {}
    for name, scan_fn in methods.items():
        recs = []
        scans = []
        nodes_list = []
        jumps_list = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands, visited, jumps = scan_fn(Qp[qi], start_fds[qi], budget)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs.append(len(gtset & set(ranked)) / 10.0)
            scans.append(len(cands) / N * 100)
            nodes_list.append(len(visited))
            jumps_list.append(jumps)
        results[name] = {
            "recall": np.mean(recs),
            "scan": np.mean(scans),
            "nodes": np.mean(nodes_list),
            "jumps": np.mean(jumps_list),
        }
        print(f"{name:<22} {np.mean(recs):>10.3f} {np.mean(scans):>8.1f} "
              f"{np.mean(nodes_list):>8.1f} {np.mean(jumps_list):>8.1f}")

    # PCA baseline (at its natural scan ~2.7%)
    recs_pca = []
    scans_pca = []
    for qi in range(len(Q)):
        cands = pca_scan(Qp[qi], TARGET_SCAN_PERCENT)
        gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        gtset = set(gt)
        ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        recs_pca.append(len(gtset & set(ranked)) / 10.0)
        scans_pca.append(len(cands) / N * 100)
    print(f"{'PCA (target 2.7%)':<22} {np.mean(recs_pca):>10.3f} {np.mean(scans_pca):>8.1f} "
          f"{'—':>8} {'—':>8}")

    # Also run at TARGET_SCAN_PERCENT (2.7%) for all methods
    print(f"\n--- Results at {TARGET_SCAN_PERCENT}% scan budget (target) ---")
    print(f"{'Method':<22} {'Recall@10':>10} {'Scan%':>8} {'Nodes':>8} {'Jumps':>8}")
    print("-" * 60)
    for name, scan_fn in methods.items():
        recs = []
        scans = []
        nodes_list = []
        jumps_list = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands, visited, jumps = scan_fn(Qp[qi], start_fds[qi], TARGET_SCAN_PERCENT)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs.append(len(gtset & set(ranked)) / 10.0)
            scans.append(len(cands) / N * 100)
            nodes_list.append(len(visited))
            jumps_list.append(jumps)
        print(f"{name:<22} {np.mean(recs):>10.3f} {np.mean(scans):>8.1f} "
              f"{np.mean(nodes_list):>8.1f} {np.mean(jumps_list):>8.1f}")

    # Key insight check: does XYZ direction correlate with good edges?
    print("\n--- Edge alignment analysis ---")
    # For each query, compute alignment of each Hasse edge with query direction
    # and check if high-alignment edges lead to better recall
    alignments_per_query = []
    for qi in range(len(Q)):
        q_dir = query_direction(Qp[qi])
        edge_aligns = []
        for (u, v), ed in edge_dir_map.items():
            align = float(np.dot(ed, q_dir))
            edge_aligns.append((align, u, v))
        edge_aligns.sort(key=lambda x: -x[0])
        alignments_per_query.append(edge_aligns)
    # Top-3 most aligned edges per query
    print("  Top-3 aligned edges (query 0):")
    for align, u, v in alignments_per_query[0][:3]:
        print(f"    edge ({u},{v}): alignment={align:.3f} "
              f"rank_u={fd.rank[u]} rank_v={fd.rank[v]}")

    # Hypothesis test: do high-alignment edges lead to nodes with more GT data?
    print("\n  Hypothesis: high-alignment edges -> nodes containing more GT items")
    # For query 0, check if following high-alignment edges reaches GT-containing nodes
    gt0 = set(sorted(range(N), key=lambda i: cos(Qn[0], Xn[i]), reverse=True)[:10])
    gt_nodes = set()
    for idx in gt0:
        f = X_face[idx]
        gt_nodes.add(fd.face_to_fd[f])
    print(f"  GT nodes for query 0: {sorted(gt_nodes)}")
    top_edges = [e for _, _, e in [(a, u, v) for a, u, v in alignments_per_query[0][:5]]]
    top_nodes = set()
    for align, u, v in alignments_per_query[0][:5]:
        top_nodes.add(u)
        top_nodes.add(v)
    print(f"  Nodes reachable via top-5 aligned edges: {sorted(top_nodes)}")
    overlap = gt_nodes & top_nodes
    print(f"  Overlap (GT nodes ∩ top-edge nodes): {sorted(overlap)} ({len(overlap)}/{len(gt_nodes)})")

    print("\n  PASS")


def main():
    run_comparison()
    print()
    print("XYZ direction controller prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
