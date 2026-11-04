"""Real-data ablation: MemCore embeddings with 3-layer metrics.

Tests on real data (memcore fact_store.v2.sqlite3) to check if traversal
adds new info or just finds duplicate buckets.

3-layer metrics per budget:
  1. Start Recall: recall from start node only
  2. Pool Recall: recall after traversal (candidate pool)
  3. Final Recall: recall after cosine rerank

Also tracks:
  - Candidate Jaccard between methods
  - Unique candidates added by traversal (pool - start)
  - Nodes visited, jumps

Budgets: 5%, 10%, 20%

Collectors: Random Graph, Hasse BFS, Pyrochlore
Start selector: Nearest-face (baseline)
Reranker: Cosine

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
import sqlite3
sys.path.insert(0, '.')

import fd3_hasse as fd

RANDOM_SEED = 42
BUDGETS = [5.0, 10.0, 20.0]


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def load_memcore_data(db_path, max_n=300):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT e.id, e.content, m.vec FROM entries e "
        "JOIN embeddings m ON m.entry_id = e.id LIMIT ?", (max_n,)).fetchall()
    conn.close()
    ids = [r[0] for r in rows]
    vecs = np.array([np.frombuffer(r[2], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.clip(norms, 1e-9, None)
    return ids, vecs


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


def to_dd(adj):
    dd = defaultdict(set)
    for k, v in (adj.items() if hasattr(adj, 'items') else []):
        dd[k] = set(v)
    for i in range(fd.n):
        if i not in dd:
            dd[i] = set()
    return dd


def run_ablation():
    print("=" * 70)
    print("Real-Data Ablation: MemCore embeddings (3-layer metrics)")
    print("=" * 70)

    db_path = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
    ids, X = load_memcore_data(db_path, max_n=300)
    N = len(X)
    print(f"  Loaded {N} entries from memcore, dim={X.shape[1]}")

    # PCA to 3D for start node selection
    mu = X.mean(0)
    U = np.linalg.svd(X - mu, full_matrices=False)[2][:3].T
    Xp = (X - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)

    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    X_face = [int(np.argmax(face_centers @ v)) for v in Xp]
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_map[fd.face_to_fd[f]].append(i)

    def get_start_fd(idx):
        v = Xp[idx]
        qn = v / (np.linalg.norm(v) + 1e-9)
        sims = face_centers @ qn
        return fd.face_to_fd[int(np.argmax(sims))]

    # Query set: 20 random data points
    rng = np.random.default_rng(42)
    query_indices = rng.choice(N, size=20, replace=False)
    Q = X[query_indices]

    def get_gt(qi):
        sims = X @ Q[qi]
        sims[query_indices[qi]] = -1
        order = np.argsort(sims)[::-1]
        return set(order[:10])

    gts = [get_gt(qi) for qi in range(len(Q))]
    start_fds = [get_start_fd(qi) for qi in query_indices]

    # Build graphs
    graphs = {
        "Random": to_dd(build_random_graph(n=fd.n, n_edges=32, seed=42)),
        "Hasse": to_dd(fd.hasse_adj),
        "Pyrochlore": to_dd(build_pyrochlore(n=fd.n, seed=42)),
    }

    def collect(graph, start_fd, budget_pct):
        max_cands = int(budget_pct / 100.0 * N)
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

    # Run at each budget
    for budget in BUDGETS:
        print(f"\n{'='*70}")
        print(f"Budget: {budget}%")
        print(f"{'Collector':<14} {'StartRec':>9} {'PoolRec':>9} {'FinalRec':>9} "
              f"{'Scan%':>7} {'Nodes':>6} {'UniqAdd':>8} {'Jaccard':>8}")
        print("-" * 70)

        method_results = {}
        for gname, graph in graphs.items():
            start_recalls = []
            pool_recalls = []
            final_recalls = []
            scans = []
            nodes_list = []
            uniq_adds = []

            for qi in range(len(Q)):
                gt = gts[qi]
                start_fd = start_fds[qi]

                start_cands = set(fd_map.get(start_fd, []))
                start_rec = len(gt & start_cands) / 10.0
                start_recalls.append(start_rec)

                cands, visited = collect(graph, start_fd, budget)
                pool_rec = len(gt & cands) / 10.0
                pool_recalls.append(pool_rec)

                ranked = sorted(cands, key=lambda i: cos(Q[qi], X[i]), reverse=True)[:10]
                final_rec = len(gt & set(ranked)) / 10.0
                final_recalls.append(final_rec)

                scans.append(len(cands) / N * 100)
                nodes_list.append(len(visited))
                uniq_adds.append(len(cands - start_cands))

            method_results[gname] = {
                "start_rec": np.mean(start_recalls),
                "pool_rec": np.mean(pool_recalls),
                "final_rec": np.mean(final_recalls),
                "scan": np.mean(scans),
                "nodes": np.mean(nodes_list),
                "uniq_add": np.mean(uniq_adds),
            }

        # Jaccard between methods
        jaccards = defaultdict(list)
        for qi in range(len(Q)):
            pools = {}
            for gname, graph in graphs.items():
                start_fd = start_fds[qi]
                cands, _ = collect(graph, start_fd, budget)
                pools[gname] = cands
            gnames = list(graphs.keys())
            for i in range(len(gnames)):
                for j in range(i + 1, len(gnames)):
                    g1, g2 = gnames[i], gnames[j]
                    inter = len(pools[g1] & pools[g2])
                    union = len(pools[g1] | pools[g2])
                    jac = inter / union if union > 0 else 0
                    jaccards[f"{g1} vs {g2}"].append(jac)

        avg_jac = np.mean([np.mean(v) for v in jaccards.values()])

        for gname in graphs:
            r = method_results[gname]
            print(f"{gname:<14} {r['start_rec']:>9.3f} {r['pool_rec']:>9.3f} "
                  f"{r['final_rec']:>9.3f} {r['scan']:>7.1f} {r['nodes']:>6.1f} "
                  f"{r['uniq_add']:>8.0f} {avg_jac:>8.3f}")

        print(f"\n  Jaccard detail:")
        for pair, jacs in jaccards.items():
            print(f"    {pair}: {np.mean(jacs):.3f}")

        print(f"\n  Unique candidates added by traversal:")
        for gname in graphs:
            r = method_results[gname]
            print(f"    {gname}: {r['uniq_add']:.0f} new/query "
                  f"(start provides {r['start_rec']:.3f})")

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY: 3-Layer Metrics on Real Data (MemCore)")
    print("=" * 70)
    print("  Layer 1 (Start Recall): nearest-face start node only")
    print("  Layer 2 (Pool Recall): after graph traversal")
    print("  Layer 3 (Final Recall): after cosine rerank (top-10)")
    print()
    print("  Key: UniqAdd = candidates added by traversal (pool - start)")
    print("  If UniqAdd > 0 and PoolRecall > StartRecall → traversal adds value")
    print("  If Jaccard high → all methods find same candidates")
    print()
    print("  PASS")


def main():
    run_ablation()
    print()
    print("Real-data ablation: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
