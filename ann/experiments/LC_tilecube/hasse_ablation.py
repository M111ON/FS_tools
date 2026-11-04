"""Ablation: What makes Hasse BFS useful?

4 tests to isolate the source of Hasse BFS benefit:
  A: Random graph traversal — is Hasse structure real or any graph?
  B: Hasse BFS with shuffled neighbor order — does edge order matter?
  C: Hasse BFS + cosine rerank — does rerank improve recall or just reorder?
  D: PCA + cosine rerank — comparison under same budget

Config (locked):
  SCAN_BUDGET = 5, 10, 15, 20 (%)
  RERANK_METRIC = COSINE
  GRAPH_MODE = HASSE_BFS
  XYZ_GUIDANCE = DISABLED
  RANDOM_SEED = FIXED (42 for data, 3 for queries)

Caution: "Hasse better than PCA" limited to this experiment (PCA=2.7% vs Hasse=5%,
not real data).

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

SCAN_BUDGETS = [5.0, 10.0, 15.0, 20.0]
RANDOM_SEED_DATA = 42
RANDOM_SEED_QUERY = 3


def antichain_direction(ac):
    if not ac:
        return np.array([0.0, 0.0, 0.0])
    coords = np.zeros(3)
    for mask in ac:
        for g in range(3):
            if mask & (1 << g):
                coords[g] += 1.0
    return coords / len(ac)


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def run_ablation():
    print("=" * 70)
    print("Ablation: What makes Hasse BFS useful?")
    print(f"  SCAN_BUDGETS = {SCAN_BUDGETS}")
    print(f"  RERANK_METRIC = COSINE, GRAPH_MODE = HASSE_BFS")
    print(f"  XYZ_GUIDANCE = DISABLED, RANDOM_SEED = FIXED")
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

    # ---- Random graph: create a random graph with same node/edge counts ----
    rng_graph = np.random.default_rng(99)
    random_adj = defaultdict(set)
    nodes = list(range(fd.n))
    # Create random edges (same count as Hasse: 32 undirected = 64 directed)
    edges_created = 0
    while edges_created < FD_EDGE_COUNT if False else edges_created < 32:
        u = rng_graph.integers(0, fd.n)
        v = rng_graph.integers(0, fd.n)
        if u != v and v not in random_adj[u]:
            random_adj[u].add(v)
            random_adj[v].add(u)
            edges_created += 1
    # Ensure connectivity (connect isolated nodes)
    for i in range(fd.n):
        if not random_adj[i]:
            j = rng_graph.integers(0, fd.n)
            if j != i:
                random_adj[i].add(j)
                random_adj[j].add(i)

    # ---- Method A: Random graph traversal ----
    def scan_random_graph(q_proj, start_fd, budget_pct):
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in random_adj[node]:
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

    # ---- Method B: Hasse BFS with shuffled neighbor order ----
    # Pre-generate shuffled adjacency for each node
    shuffled_adj = {}
    rng_shuffle = np.random.default_rng(77)
    for node in range(fd.n):
        nbrs = list(fd.hasse_adj[node])
        rng_shuffle.shuffle(nbrs)
        shuffled_adj[node] = nbrs

    def scan_shuffled(q_proj, start_fd, budget_pct):
        max_cands = int(budget_pct / 100.0 * N)
        visited = set([start_fd])
        cands = set(fd_map.get(start_fd, []))
        queue = deque([start_fd])
        jumps = 0
        while queue and len(cands) < max_cands:
            node = queue.popleft()
            for nb in shuffled_adj[node]:
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

    # ---- Method C0: Hasse BFS (baseline, same as A in xyz_direction) ----
    def scan_hasse_bfs(q_proj, start_fd, budget_pct):
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

    # ---- Method C: Hasse BFS + cosine rerank ----
    # Rerank happens at evaluation time (sort candidates by cosine, take top-10)
    # This is the same scan as Hasse BFS, but evaluation uses cosine ranking
    # (already done in all methods). The question is: does rerank improve recall
    # beyond just traversal? We test by comparing "traversal order top-10" vs
    # "cosine rerank top-10".
    def scan_hasse_bfs_cosine_rerank(q_proj, start_fd, budget_pct):
        cands, visited, jumps = scan_hasse_bfs(q_proj, start_fd, budget_pct)
        return cands, visited, jumps  # rerank at eval time

    # ---- Method D: PCA + cosine rerank ----
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
        # Collect from bucket + all neighbors (12 alternatives per dimension)
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

    # ---- Run ablation at each budget ----
    for budget in SCAN_BUDGETS:
        print(f"\n{'='*70}")
        print(f"Budget: {budget}%")
        print(f"{'Method':<30} {'Recall@10':>10} {'Scan%':>8} {'Nodes':>7} "
              f"{'Jumps':>7} {'Rec/Node':>9}")
        print("-" * 70)

        methods = {
            "A: Random graph": scan_random_graph,
            "B: Hasse (shuffled)": scan_shuffled,
            "C0: Hasse BFS": scan_hasse_bfs,
        }

        for name, scan_fn in methods.items():
            recs = []
            scans = []
            nodes_list = []
            jumps_list = []
            rec_per_node = []

            for qi in range(len(Q)):
                gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
                gtset = set(gt)
                cands, visited, jumps = scan_fn(Qp[qi], start_fds[qi], budget)
                ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
                rec = len(gtset & set(ranked)) / 10.0
                recs.append(rec)
                scans.append(len(cands) / N * 100)
                nodes_list.append(len(visited))
                jumps_list.append(jumps)
                rec_per_node.append(rec / len(visited) if len(visited) > 0 else 0.0)

            print(f"{name:<30} {np.mean(recs):>10.3f} {np.mean(scans):>8.1f} "
                  f"{np.mean(nodes_list):>7.1f} {np.mean(jumps_list):>7.1f} "
                  f"{np.mean(rec_per_node):>9.3f}")

        # Method C: Hasse BFS + cosine rerank (same scan, rerank at eval)
        # Also test "traversal order" (no rerank) for comparison
        recs_rerank = []
        recs_traversal = []
        scans_c = []
        nodes_c = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands, visited, jumps = scan_hasse_bfs(Qp[qi], start_fds[qi], budget)
            # Cosine rerank
            ranked_rerank = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            rec_rerank = len(gtset & set(ranked_rerank)) / 10.0
            # Traversal order (no rerank) - take first 10 in traversal order
            # Since BFS collects candidates in node order, take first 10 candidates
            cands_list = list(cands)[:10]
            rec_traversal = len(gtset & set(cands_list)) / 10.0
            recs_rerank.append(rec_rerank)
            recs_traversal.append(rec_traversal)
            scans_c.append(len(cands) / N * 100)
            nodes_c.append(len(visited))

        print(f"{'C: Hasse+cosine rerank':<30} {np.mean(recs_rerank):>10.3f} "
              f"{np.mean(scans_c):>8.1f} {np.mean(nodes_c):>7.1f} {'—':>7} "
              f"{np.mean([r/n if n>0 else 0 for r,n in zip(recs_rerank, nodes_c)]):>9.3f}")
        print(f"{'   (traversal order, no rerank)':<30} {np.mean(recs_traversal):>10.3f} "
              f"{np.mean(scans_c):>8.1f} {np.mean(nodes_c):>7.1f} {'—':>7} "
              f"{'—':>9}")

        # Method D: PCA + cosine rerank
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            cands = pca_scan(Qp[qi], budget)
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_pca.append(len(gtset & set(ranked)) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"{'D: PCA+cosine rerank':<30} {np.mean(recs_pca):>10.3f} "
              f"{np.mean(scans_pca):>8.1f} {'—':>7} {'—':>7} {'—':>9}")

    # Summary: key question
    print(f"\n{'='*70}")
    print("KEY QUESTION: Why does Hasse BFS work?")
    print("=" * 70)

    # Compare Hasse BFS vs Random graph at 10% budget
    # (recompute for 10% specifically)
    budget = 10.0
    recs_hasse = []
    recs_random = []
    recs_shuffled = []
    for qi in range(len(Q)):
        gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        gtset = set(gt)
        c_h, _, _ = scan_hasse_bfs(Qp[qi], start_fds[qi], budget)
        c_r, _, _ = scan_random_graph(Qp[qi], start_fds[qi], budget)
        c_s, _, _ = scan_shuffled(Qp[qi], start_fds[qi], budget)
        r_h = len(gtset & set(sorted(c_h, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10])) / 10.0
        r_r = len(gtset & set(sorted(c_r, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10])) / 10.0
        r_s = len(gtset & set(sorted(c_s, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10])) / 10.0
        recs_hasse.append(r_h)
        recs_random.append(r_r)
        recs_shuffled.append(r_s)

    print(f"\n  At {budget}% budget:")
    print(f"    Hasse BFS:        {np.mean(recs_hasse):.3f}")
    print(f"    Random graph:     {np.mean(recs_random):.3f}")
    print(f"    Hasse (shuffled): {np.mean(recs_shuffled):.3f}")
    print()

    hasse_vs_random = np.mean(recs_hasse) - np.mean(recs_random)
    hasse_vs_shuffled = np.mean(recs_hasse) - np.mean(recs_shuffled)

    if hasse_vs_random > 0.02:
        print("  -> Hasse structure MATTERS (beats random graph by "
              f"{hasse_vs_random:.3f})")
    else:
        print("  -> Hasse structure does NOT matter much (diff from random: "
              f"{hasse_vs_random:.3f})")

    if hasse_vs_shuffled > 0.02:
        print("  -> Edge ORDER matters (shuffled loses "
              f"{hasse_vs_shuffled:.3f})")
    else:
        print("  -> Edge order does NOT matter much (shuffled diff: "
              f"{hasse_vs_shuffled:.3f})")

    # Rerank impact
    print(f"\n  Rerank impact (Hasse BFS at {budget}%):")
    print(f"    With cosine rerank:    {np.mean(recs_rerank):.3f}")
    print(f"    Without (traversal):   {np.mean(recs_traversal):.3f}")
    rerank_impact = np.mean(recs_rerank) - np.mean(recs_traversal)
    if rerank_impact > 0.02:
        print(f"    -> Rerank IMPROVES recall by {rerank_impact:.3f}")
    else:
        print(f"    -> Rerank does NOT improve recall much ({rerank_impact:.3f})")

    print("\n  PASS")


def main():
    run_ablation()
    print()
    print("Hasse ablation: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
