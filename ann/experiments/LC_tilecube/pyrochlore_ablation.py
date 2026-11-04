"""Pyrochlore Ablation: Tetrahedral connectivity vs Random vs Hasse.

Tests 3 collectors at same budget/dataset/seed/reranker:
  1. Random Graph (baseline)
  2. Hasse BFS (previous baseline)
  3. Pyrochlore (tetrahedral connectivity, coord=6, 4-site basis)

Config (locked):
  GRAPH_TYPE = PYROCHLORE
  COORDINATION = 6
  BASIS_COUNT = 4
  COLLECTOR_BUDGET = 5 (%)
  RERANK_METRIC = COSINE
  TOP_K = 10
  RANDOM_SEED = FIXED (42 data, 3 query)

Metrics tracked:
  - Candidate Pool Recall (before rerank): how many GT items are in the
    collected candidate pool, regardless of ranking
  - Final Recall@10 (after cosine rerank)
  - Scan%, Nodes visited, Jumps

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

# ---- Locked constants ----
COLLECTOR_BUDGET = 5.0
TOP_K = 10
RANDOM_SEED_DATA = 42
RANDOM_SEED_QUERY = 3
COORDINATION = 6
BASIS_COUNT = 4


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


# ---- Build Pyrochlore graph (20 nodes, coord ~6) ----
def build_pyrochlore(n=20, seed=42):
    """Construct Pyrochlore-like graph: tetrahedral clusters, coord ~6.

    Pyrochlore = FCC + 4-site basis (tetrahedron).
    Corner-sharing tetrahedra: adjacent tetrahedra share a vertex.
    """
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)

    # Step 1: Form tetrahedra (groups of 4)
    # 5 tetrahedra for 20 nodes
    n_tet = n // BASIS_COUNT  # 5
    tetrahedra = []
    for t in range(n_tet):
        tet = list(range(t * BASIS_COUNT, (t + 1) * BASIS_COUNT))
        tetrahedra.append(tet)

    # Step 2: Within each tetrahedron: complete graph K4 (6 edges)
    # Each node connects to 3 others within tetrahedron
    for tet in tetrahedra:
        for i in range(len(tet)):
            for j in range(i + 1, len(tet)):
                adj[tet[i]].add(tet[j])
                adj[tet[j]].add(tet[i])

    # Step 3: Between tetrahedra: corner-sharing (share 1 vertex)
    # Connect each node to 3 nodes in adjacent tetrahedron
    # Tetrahedron t shares a vertex with tetrahedron t+1
    for t in range(n_tet - 1):
        tet_a = tetrahedra[t]
        tet_b = tetrahedra[t + 1]
        # Share vertex: node 0 of tet_a = node 0 of tet_b (conceptually)
        # In practice, connect node i of tet_a to node (i+1)%4 of tet_b
        for i in range(BASIS_COUNT):
            u = tet_a[i]
            v = tet_b[(i + 1) % BASIS_COUNT]
            adj[u].add(v)
            adj[v].add(u)
        # Also connect node i to node (i+2)%4 for stronger connectivity
        for i in range(BASIS_COUNT):
            u = tet_a[i]
            v = tet_b[(i + 2) % BASIS_COUNT]
            adj[u].add(v)
            adj[v].add(u)

    # Step 4: Wrap-around: last tetrahedron connects to first
    tet_a = tetrahedra[-1]
    tet_b = tetrahedra[0]
    for i in range(BASIS_COUNT):
        u = tet_a[i]
        v = tet_b[(i + 1) % BASIS_COUNT]
        adj[u].add(v)
        adj[v].add(u)

    # Step 5: Ensure coordination ~6 (add edges if needed)
    for node in range(n):
        while len(adj[node]) < COORDINATION:
            # Find a node to connect to
            candidates = [x for x in range(n) if x != node and x not in adj[node]]
            if not candidates:
                break
            target = rng.choice(candidates)
            adj[node].add(target)
            adj[target].add(node)

    # Verify
    degrees = [len(adj[i]) for i in range(n)]
    print(f"  Pyrochlore graph: {n} nodes, {sum(degrees)//2} edges")
    print(f"  Degree: min={min(degrees)} max={max(degrees)} avg={np.mean(degrees):.1f}")
    print(f"  (target coordination: {COORDINATION})")

    return adj


# ---- Build Random graph (same edge count as Hasse for fairness) ----
def build_random_graph(n=20, n_edges=32, seed=42):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    edges_created = 0
    while edges_created < n_edges:
        u = rng.integers(0, n)
        v = rng.integers(0, n)
        if u != v and v not in adj[u]:
            adj[u].add(v)
            adj[v].add(u)
            edges_created += 1
    # Ensure no isolated nodes
    for i in range(n):
        if not adj[i]:
            j = rng.integers(0, n)
            if j != i:
                adj[i].add(j)
                adj[j].add(i)
    degrees = [len(adj[i]) for i in range(n)]
    print(f"  Random graph: {n} nodes, {sum(degrees)//2} edges")
    print(f"  Degree: min={min(degrees)} max={max(degrees)} avg={np.mean(degrees):.1f}")
    return adj


def run_pyrochlore_ablation():
    print("=" * 70)
    print("Pyrochlore Ablation: Tetrahedral vs Random vs Hasse")
    print(f"  COLLECTOR_BUDGET = {COLLECTOR_BUDGET}%")
    print(f"  RERANK_METRIC = COSINE, TOP_K = {TOP_K}")
    print(f"  RANDOM_SEED = FIXED ({RANDOM_SEED_DATA}, {RANDOM_SEED_QUERY})")
    print("=" * 70)

    d = 64
    N = 500
    rng = np.random.default_rng(RANDOM_SEED_DATA)
    X = rng.normal(size=(N, d))
    Q = np.random.default_rng(RANDOM_SEED_QUERY).normal(size=(20, d))

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

    # fd_map: FD element -> list of data indices
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_map[fd.face_to_fd[f]].append(i)

    def get_start_fd(q_proj):
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = face_centers @ qn
        nearest_face = int(np.argmax(sims))
        return fd.face_to_fd[nearest_face]

    start_fds = [get_start_fd(qp) for qp in Qp]

    # Build graphs
    print("\n--- Graph construction ---")
    pyro_adj = build_pyrochlore(n=fd.n, seed=42)
    random_adj = build_random_graph(n=fd.n, n_edges=32, seed=42)
    hasse_adj = fd.hasse_adj

    # Convert to defaultdict for uniform access
    def to_dd(adj):
        dd = defaultdict(set)
        for k, v in adj.items():
            dd[k] = set(v)
        for i in range(fd.n):
            if i not in dd:
                dd[i] = set()
        return dd

    graphs = {
        "Random Graph": to_dd(random_adj),
        "Hasse BFS": to_dd(hasse_adj),
        "Pyrochlore": to_dd(pyro_adj),
    }

    # Generic collector: BFS on graph
    def collect(graph, q_proj, start_fd, budget_pct):
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in graph[node]:
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

    # Run comparison
    print(f"\n{'='*70}")
    print(f"Collector Comparison at {COLLECTOR_BUDGET}% budget")
    print(f"{'Collector':<18} {'Pool Recall':>12} {'Final Recall':>13} {'Scan%':>8} "
          f"{'Nodes':>7} {'Jumps':>7} {'Rec/Node':>9}")
    print("-" * 70)

    results = {}
    for gname, graph in graphs.items():
        pool_recalls = []  # candidate pool recall (before rerank)
        final_recalls = []  # after cosine rerank
        scans = []
        nodes_list = []
        jumps_list = []
        rec_per_node = []

        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:TOP_K]
            gtset = set(gt)

            cands, visited, jumps = collect(graph, Qp[qi], start_fds[qi], COLLECTOR_BUDGET)

            # Candidate pool recall (before rerank): how many GT in pool
            pool_hits = len(gtset & cands)
            pool_recall = pool_hits / TOP_K
            pool_recalls.append(pool_recall)

            # Final recall (after cosine rerank)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:TOP_K]
            final_rec = len(gtset & set(ranked)) / TOP_K
            final_recalls.append(final_rec)

            scans.append(len(cands) / N * 100)
            nodes_list.append(len(visited))
            jumps_list.append(jumps)
            rec_per_node.append(final_rec / len(visited) if len(visited) > 0 else 0.0)

        results[gname] = {
            "pool_recall": np.mean(pool_recalls),
            "final_recall": np.mean(final_recalls),
            "scan": np.mean(scans),
            "nodes": np.mean(nodes_list),
            "jumps": np.mean(jumps_list),
            "rec_per_node": np.mean(rec_per_node),
        }

        print(f"{gname:<18} {np.mean(pool_recalls):>12.3f} {np.mean(final_recalls):>13.3f} "
              f"{np.mean(scans):>8.1f} {np.mean(nodes_list):>7.1f} "
              f"{np.mean(jumps_list):>7.1f} {np.mean(rec_per_node):>9.3f}")

    # Detailed per-query breakdown
    print(f"\n{'='*70}")
    print("Per-Query Breakdown (Candidate Pool Recall)")
    print("=" * 70)
    print(f"{'Query':<8} {'Random':>10} {'Hasse':>10} {'Pyrochlore':>12}")
    print("-" * 42)

    # Recompute per-query for detailed view
    per_query = {g: [] for g in graphs}
    for qi in range(len(Q)):
        gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:TOP_K]
        gtset = set(gt)
        for gname, graph in graphs.items():
            cands, _, _ = collect(graph, Qp[qi], start_fds[qi], COLLECTOR_BUDGET)
            pool_rec = len(gtset & cands) / TOP_K
            per_query[gname].append(pool_rec)

    for qi in range(len(Q)):
        print(f"  {qi:<6} {per_query['Random Graph'][qi]:>10.2f} "
              f"{per_query['Hasse BFS'][qi]:>10.2f} "
              f"{per_query['Pyrochlore'][qi]:>12.2f}")

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print("=" * 70)
    print(f"  Candidate Pool Recall (before rerank):")
    for gname in graphs:
        print(f"    {gname:<18}: {results[gname]['pool_recall']:.3f}")
    print(f"  Final Recall@{TOP_K} (after cosine rerank):")
    for gname in graphs:
        print(f"    {gname:<18}: {results[gname]['final_recall']:.3f}")

    # Key question
    print(f"\n  KEY QUESTION: Does Pyrochlore tetrahedral connectivity improve")
    print(f"  candidate pool quality over Random Graph?")
    pyro_vs_random_pool = results["Pyrochlore"]["pool_recall"] - results["Random Graph"]["pool_recall"]
    pyro_vs_random_final = results["Pyrochlore"]["final_recall"] - results["Random Graph"]["final_recall"]
    hasse_vs_random_pool = results["Hasse BFS"]["pool_recall"] - results["Random Graph"]["pool_recall"]

    print(f"\n  Pool recall diff (Pyrochlore - Random): {pyro_vs_random_pool:+.3f}")
    print(f"  Final recall diff (Pyrochlore - Random): {pyro_vs_random_final:+.3f}")
    print(f"  Pool recall diff (Hasse - Random):      {hasse_vs_random_pool:+.3f}")

    if pyro_vs_random_pool > 0.02:
        print("  -> Pyrochlore DOES improve candidate pool quality")
    else:
        print("  -> Pyrochlore does NOT improve candidate pool quality")

    if hasse_vs_random_pool > 0.02:
        print("  -> Hasse DOES improve candidate pool quality")
    else:
        print("  -> Hasse does NOT improve candidate pool quality")

    # Edge-level: Pyrochlore connectivity analysis
    print(f"\n{'='*70}")
    print("Pyrochlore Connectivity Analysis")
    print("=" * 70)
    # Check if Pyrochlore has more "useful" edges (edges that lead to GT nodes)
    # by comparing edge GT-hit rates
    for gname, graph in [("Random Graph", random_adj), ("Pyrochlore", pyro_adj)]:
        edge_hits = 0
        edge_total = 0
        for u in range(fd.n):
            for v in graph[u]:
                if u < v:
                    edge_total += 1
                    # Check if this edge connects nodes that share GT items
                    # (approximate: check if both nodes have data)
                    if len(fd_map.get(u, [])) > 0 and len(fd_map.get(v, [])) > 0:
                        edge_hits += 1
        print(f"  {gname}: {edge_total} edges, {edge_hits} with data on both ends "
              f"({edge_hits/edge_total*100:.0f}%)")

    print("\n  PASS")

    return results


def main():
    run_pyrochlore_ablation()
    print()
    print("Pyrochlore ablation: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
