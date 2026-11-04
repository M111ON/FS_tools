"""Diagnostics: Why do all graph collectors give the same result?

Checks:
  1. Do all graphs use the same starting node?
  2. How much candidate pool overlap between graphs?
  3. Does traversal stop by node visit or unique candidate budget?
  4. Is random graph regenerated per query or fixed?

Uses same setup as pyrochlore_ablation.py.
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

RANDOM_SEED_DATA = 42
RANDOM_SEED_QUERY = 3
COLLECTOR_BUDGET = 5.0


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def build_pyrochlore(n=20, seed=42):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    n_tet = n // 4
    tetrahedra = [list(range(t * 4, (t + 1) * 4)) for t in range(n_tet)]
    for tet in tetrahedra:
        for i in range(len(tet)):
            for j in range(i + 1, len(tet)):
                adj[tet[i]].add(tet[j])
                adj[tet[j]].add(tet[i])
    for t in range(n_tet - 1):
        tet_a = tetrahedra[t]
        tet_b = tetrahedra[t + 1]
        for i in range(4):
            u = tet_a[i]
            v = tet_b[(i + 1) % 4]
            adj[u].add(v)
            adj[v].add(u)
        for i in range(4):
            u = tet_a[i]
            v = tet_b[(i + 2) % 4]
            adj[u].add(v)
            adj[v].add(u)
    tet_a = tetrahedra[-1]
    tet_b = tetrahedra[0]
    for i in range(4):
        u = tet_a[i]
        v = tet_b[(i + 1) % 4]
        adj[u].add(v)
        adj[v].add(u)
    for node in range(n):
        while len(adj[node]) < 6:
            candidates = [x for x in range(n) if x != node and x not in adj[node]]
            if not candidates:
                break
            target = rng.choice(candidates)
            adj[node].add(target)
            adj[target].add(node)
    return adj


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
    for i in range(n):
        if not adj[i]:
            j = rng.integers(0, n)
            if j != i:
                adj[i].add(j)
                adj[j].add(i)
    return adj


def run_diagnostics():
    print("=" * 70)
    print("Diagnostics: Why do all collectors give the same result?")
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
    pyro_adj = build_pyrochlore(n=fd.n, seed=42)
    random_adj = build_random_graph(n=fd.n, n_edges=32, seed=42)
    hasse_adj = fd.hasse_adj

    graphs = {
        "Random": random_adj,
        "Hasse": hasse_adj,
        "Pyrochlore": pyro_adj,
    }

    # Convert to defaultdict
    def to_dd(adj):
        dd = defaultdict(set)
        for k, v in (adj.items() if hasattr(adj, 'items') else []):
            dd[k] = set(v)
        for i in range(fd.n):
            if i not in dd:
                dd[i] = set()
        return dd

    graphs = {k: to_dd(v) for k, v in graphs.items()}

    # ---- Check 1: Starting nodes ----
    print("\n1. Starting nodes (per query):")
    print(f"   {'Query':<8} {'Start FD':>10}")
    print("   " + "-" * 20)
    unique_starts = set(start_fds)
    print(f"   Unique starting nodes across 20 queries: {len(unique_starts)}")
    print(f"   Starting nodes: {sorted(unique_starts)}")
    from collections import Counter
    start_counts = Counter(start_fds)
    print(f"   Most common start: {start_counts.most_common(3)}")

    # ---- Check 2: Candidate pool overlap ----
    print("\n2. Candidate pool overlap (Jaccard similarity):")
    max_cands = int(COLLECTOR_BUDGET / 100.0 * N)

    def collect(graph, start_fd):
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in graph[node]:
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
        return cands, visited

    # Compute overlap for each query
    overlaps = defaultdict(list)
    pool_sizes = defaultdict(list)
    for qi in range(len(Q)):
        pools = {}
        for gname, graph in graphs.items():
            cands, visited = collect(graph, start_fds[qi])
            pools[gname] = cands
            pool_sizes[gname].append(len(cands))

        # Pairwise Jaccard
        gnames = list(graphs.keys())
        for i in range(len(gnames)):
            for j in range(i + 1, len(gnames)):
                g1, g2 = gnames[i], gnames[j]
                inter = len(pools[g1] & pools[g2])
                union = len(pools[g1] | pools[g2])
                jaccard = inter / union if union > 0 else 0
                overlaps[f"{g1} vs {g2}"].append(jaccard)

    for pair, jacs in overlaps.items():
        print(f"   {pair}: avg Jaccard = {np.mean(jacs):.3f} "
              f"(min={np.min(jacs):.3f}, max={np.max(jacs):.3f})")

    print(f"\n   Average pool size: "
          + ", ".join(f"{g}={np.mean(pool_sizes[g]):.0f}" for g in graphs))

    # ---- Check 3: Node visit vs candidate budget ----
    print("\n3. Stopping condition analysis:")
    for gname, graph in graphs.items():
        nodes_visited = []
        cands_collected = []
        for qi in range(len(Q)):
            visited = set([start_fds[qi]])
            cands = set(fd_map.get(start_fds[qi], []))
            queue = deque([start_fds[qi]])
            while queue and len(cands) < max_cands:
                node = queue.popleft()
                for nb in graph[node]:
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
            nodes_visited.append(len(visited))
            cands_collected.append(len(cands))
        print(f"   {gname}: avg nodes visited = {np.mean(nodes_visited):.1f}, "
              f"avg candidates = {np.mean(cands_collected):.0f} "
              f"(target budget = {max_cands})")

    # ---- Check 4: Random graph stability ----
    print("\n4. Random graph stability (regenerated per query vs fixed):")
    # Fixed graph (already built)
    # Regenerated graph (new seed per query)
    fixed_overlaps = []
    regen_overlaps = []
    for qi in range(len(Q)):
        # Fixed graph pool
        cands_fixed, _ = collect(random_adj, start_fds[qi])
        # Regenerated graph
        regen_adj = build_random_graph(n=fd.n, n_edges=32, seed=42 + qi)
        regen_dd = to_dd(regen_adj)
        cands_regen, _ = collect(regen_dd, start_fds[qi])
        # Overlap between fixed and regen
        inter = len(cands_fixed & cands_regen)
        union = len(cands_fixed | cands_regen)
        regen_overlaps.append(inter / union if union > 0 else 0)
        # Overlap between two fixed-graph runs (should be 1.0)
        cands_fixed2, _ = collect(random_adj, start_fds[qi])
        inter2 = len(cands_fixed & cands_fixed2)
        union2 = len(cands_fixed | cands_fixed2)
        fixed_overlaps.append(inter2 / union2 if union2 > 0 else 0)

    print(f"   Fixed graph (same graph, different runs): "
          f"avg overlap = {np.mean(fixed_overlaps):.3f}")
    print(f"   Regenerated graph (different seed per query): "
          f"avg overlap with fixed = {np.mean(regen_overlaps):.3f}")

    # ---- Check 5: Candidate pool composition ----
    print("\n5. Candidate pool composition (unique nodes contributing):")
    for gname, graph in graphs.items():
        node_contributions = defaultdict(int)
        total_cands = 0
        for qi in range(len(Q)):
            cands, visited = collect(graph, start_fds[qi])
            for node in visited:
                node_contributions[node] += 1
            total_cands += len(cands)
        # How many nodes contribute candidates across all queries?
        active_nodes = sum(1 for v in node_contributions.values() if v > 0)
        print(f"   {gname}: {active_nodes}/{fd.n} nodes active across queries, "
              f"avg {total_cands/len(Q):.0f} cands/query")

    # ---- Check 6: Does the start node dominate? ----
    print("\n6. Start node dominance:")
    results = {}
    for gname, graph in graphs.items():
        start_only_recalls = []
        pool_recalls = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            start_cands = set(fd_map.get(start_fds[qi], []))
            pool_rec_start = len(gtset & start_cands) / 10.0
            start_only_recalls.append(pool_rec_start)
            cands, _ = collect(graph, start_fds[qi])
            pool_rec_full = len(gtset & cands) / 10.0
            pool_recalls.append(pool_rec_full)
        results[gname] = {"pool_recall": np.mean(pool_recalls)}
        print(f"   {gname}: start-node-only recall = {np.mean(start_only_recalls):.3f}, "
              f"full traversal recall = {results[gname]['pool_recall']:.3f}")

    print("\n  PASS")

    return results


def main():
    run_diagnostics()
    print()
    print("Diagnostics: done")
    return 0




def main():
    run_diagnostics()
    print()
    print("Diagnostics: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
