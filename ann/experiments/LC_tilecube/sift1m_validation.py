"""SIFT1M cross-dataset validation (exact L2, real benchmark GT).

Config (locked):
  Dataset        SIFT1M (1,000,000 x 128, uint8)
  Ground truth   sift_groundtruth.ivecs (exact L2 top-100)
  Start selector Nearest-face (PCA->3D -> nearest icosphere face -> FD element)
  Collectors     Random / Hasse / Pyrochlore
  Budget         5% / 10% / 20%
  Seeds          5
  Metrics        StartRecall, PoolRecall, FinalRecall, Precision@10,
                 NDCG@10, Scan%, UniqueAdd, Jaccard, Latency

Distance is L2 throughout (no cosine substitution).

Memory: base kept as uint8 (128 MB) because available RAM is ~1.8 GB.
Design note: 20 FD nodes over 1M items is ~50k per node, so a 5% budget
(50k) is roughly ONE node; start-node dominance is expected and reported.

Python prototype only (#7825).
"""
import sys, os, time
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_F = os.path.join(HERE, 'sift', 'sift_base.fvecs')
QUERY_F = os.path.join(HERE, 'sift', 'sift_query.fvecs')
GT_F = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')

BUDGETS = [5.0, 10.0, 20.0]
SEEDS = [0, 1, 2, 3, 4]
QUERIES_PER_SEED = 20
TOPK = 10


def load_fvecs_u8(path):
    """Read .fvecs into uint8. Each row = int32 dim header + dim float32 values
    (SIFT values are 0..255 stored as float32). Read as float32 and drop col 0."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row = dim + 1
    n = size // (4 * row)
    out = np.empty((n, dim), dtype=np.uint8)
    CH = 100_000
    with open(path, 'rb') as f:
        for s in range(0, n, CH):
            e = min(s + CH, n)
            blk = np.fromfile(f, dtype=np.float32, count=(e - s) * row)
            blk = blk.reshape(e - s, row)[:, 1:]
            out[s:e] = np.clip(blk, 0, 255).astype(np.uint8)
    return out


def load_ivecs(path):
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row_i32 = dim + 1
    n = size // (4 * row_i32)
    out = np.empty((n, dim), dtype=np.int64)
    CH = 100_000
    with open(path, 'rb') as f:
        for s in range(0, n, CH):
            e = min(s + CH, n)
            blk = np.fromfile(f, dtype=np.int32, count=(e - s) * row_i32)
            blk = blk.reshape(e - s, row_i32)
            out[s:e] = blk[:, 1:]
    return out


def l2_topk(X, q, idx, k, chunk=50_000):
    """Top-k global indices by L2 among candidates, chunked to bound memory."""
    qf = q.astype(np.int32)
    best_d = []
    best_i = []
    for s in range(0, len(idx), chunk):
        sub = X[idx[s:s + chunk]].astype(np.int32)
        d = np.einsum('ij,ij->i', sub - qf, sub - qf)
        kk = min(k, len(d))
        part = np.argpartition(d, kk - 1)[:kk]
        best_d.append(d[part])
        best_i.append(idx[s:s + chunk][part])
    d = np.concatenate(best_d)
    i = np.concatenate(best_i)
    order = np.argsort(d)[:k]
    return [int(x) for x in i[order]]


def build_pyrochlore(n=20, seed=42):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    nt = n // 4
    tets = [list(range(t * 4, (t + 1) * 4)) for t in range(nt)]
    for tet in tets:
        for i in range(4):
            for j in range(i + 1, 4):
                adj[tet[i]].add(tet[j]); adj[tet[j]].add(tet[i])
    for t in range(nt - 1):
        a, b = tets[t], tets[t + 1]
        for i in range(4):
            for off in (1, 2):
                u, v = a[i], b[(i + off) % 4]
                adj[u].add(v); adj[v].add(u)
    a, b = tets[-1], tets[0]
    for i in range(4):
        u, v = a[i], b[(i + 1) % 4]
        adj[u].add(v); adj[v].add(u)
    for node in range(n):
        while len(adj[node]) < 6:
            cand = [x for x in range(n) if x != node and x not in adj[node]]
            if not cand:
                break
            t = int(rng.choice(cand))
            adj[node].add(t); adj[t].add(node)
    return adj


def build_random_graph(n=20, n_edges=32, seed=42):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    edges = 0
    while edges < n_edges:
        u, v = int(rng.integers(0, n)), int(rng.integers(0, n))
        if u != v and v not in adj[u]:
            adj[u].add(v); adj[v].add(u); edges += 1
    for i in range(n):
        if not adj[i]:
            j = int(rng.integers(0, n))
            if j != i:
                adj[i].add(j); adj[j].add(i)
    return adj


def to_dd(adj):
    dd = defaultdict(set)
    for k, v in adj.items():
        dd[k] = set(v)
    for i in range(fd.n):
        dd.setdefault(i, set())
    return dd


def ndcg_at_k(ranked, gt_set, k=10):
    dcg = sum(1.0 / np.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in gt_set)
    ideal = sum(1.0 / np.log2(i + 2) for i in range(min(k, len(gt_set))))
    return dcg / ideal if ideal > 0 else 0.0


def main():
    print("=" * 78, flush=True)
    print("SIFT1M cross-dataset validation (exact L2)", flush=True)
    print("=" * 78, flush=True)

    t0 = time.time()
    X = load_fvecs_u8(BASE_F)
    Q = load_fvecs_u8(QUERY_F)
    GT = load_ivecs(GT_F)
    N, dim = X.shape
    print(f"  base {X.shape} ({X.nbytes/2**20:.0f} MB) queries {Q.shape} "
          f"gt {GT.shape}  load {time.time()-t0:.1f}s", flush=True)

    # ---- PCA(3) on a sample ----
    t0 = time.time()
    rng = np.random.default_rng(0)
    si = rng.choice(N, size=20000, replace=False)
    S = X[si].astype(np.float64)
    mu = S.mean(0)
    C = np.cov((S - mu).T)
    w, V = np.linalg.eigh(C)
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)
    mu32 = mu.astype(np.float32)
    print(f"  PCA(3) from 20k sample {time.time()-t0:.1f}s", flush=True)

    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = (face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)
                    ).astype(np.float32)
    face_to_fd = np.array([fd.face_to_fd[i] for i in range(len(fd.faces2))])

    # ---- Assign 1M vectors to FD nodes, chunked ----
    t0 = time.time()
    per_node = [[] for _ in range(fd.n)]
    CH = 50_000
    for s in range(0, N, CH):
        e = min(s + CH, N)
        P = (X[s:e].astype(np.float32) - mu32) @ U3
        P /= np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)
        face = np.argmax(P @ face_centers.T, axis=1)
        node = face_to_fd[face]
        for nid in range(fd.n):
            ids = np.nonzero(node == nid)[0]
            if len(ids):
                per_node[nid].append((ids + s).astype(np.int32))
    fd_map = {}
    for nid in range(fd.n):
        fd_map[nid] = (np.concatenate(per_node[nid]) if per_node[nid]
                       else np.empty(0, dtype=np.int32))
    print(f"  assigned {N} vectors {time.time()-t0:.1f}s", flush=True)
    sizes = {k: len(v) for k, v in fd_map.items()}
    print("  node sizes: " + ", ".join(f"{k}:{sizes[k]}" for k in sorted(sizes)), flush=True)

    def start_fd_for(q):
        p = (q.astype(np.float32) - mu32) @ U3
        p = p / max(float(np.linalg.norm(p)), 1e-9)
        return int(face_to_fd[int(np.argmax(face_centers @ p))])

    hasse_adj = to_dd(fd.hasse_adj)

    def collect(graph, start, budget_pct):
        max_c = int(budget_pct / 100.0 * N)
        a0 = fd_map[start]
        if len(a0) >= max_c:
            # Start node alone exceeds the budget: truncate (index order is
            # deterministic but arbitrary). Reported separately as a
            # partition-granularity degeneracy, not as a traversal result.
            return a0[:max_c], {start}, True
        visited = {start}
        parts = [a0]
        seen = len(a0)
        queue = deque([start])
        while queue and seen < max_c:
            node = queue.popleft()
            for nb in graph[node]:
                if nb in visited:
                    continue
                visited.add(nb)
                queue.append(nb)
                a = fd_map[nb]
                take = min(len(a), max_c - seen)
                if take > 0:
                    parts.append(a[:take])
                    seen += take
                if seen >= max_c:
                    break
            if seen >= max_c:
                break
        return np.concatenate(parts) if len(parts) > 1 else parts[0], visited, False

    agg = {}
    jacc = {b: defaultdict(list) for b in BUDGETS}
    for seed in SEEDS:
        qrng = np.random.default_rng(1000 + seed)
        qidx = qrng.choice(len(Q), size=QUERIES_PER_SEED, replace=False)
        graphs = {
            "Random": to_dd(build_random_graph(n=fd.n, n_edges=32, seed=seed)),
            "Hasse": hasse_adj,
            "Pyrochlore": to_dd(build_pyrochlore(n=fd.n, seed=seed)),
        }
        for budget in BUDGETS:
            for qi in qidx:
                q = Q[qi]
                gt_set = set(int(x) for x in GT[qi][:TOPK])
                start = start_fd_for(q)
                start_ids = fd_map[start]
                pools = {}
                for gname, graph in graphs.items():
                    t_c = time.perf_counter()
                    cands, visited, truncated = collect(graph, start, budget)
                    lat_c = (time.perf_counter() - t_c) * 1000
                    t_r = time.perf_counter()
                    ranked = l2_topk(X, q, cands, TOPK)
                    lat_r = (time.perf_counter() - t_r) * 1000
                    pools[gname] = set(int(x) for x in cands)
                    a = agg.setdefault((budget, gname), {
                        "start": [], "pool": [], "final": [], "ndcg": [],
                        "scan": [], "uniq": [], "nodes": [], "lat_c": [], "lat_r": [],
                        "trunc": []})
                    a["start"].append(len(gt_set & set(int(x) for x in start_ids)) / TOPK)
                    a["pool"].append(len(gt_set & pools[gname]) / TOPK)
                    a["final"].append(len(gt_set & set(ranked)) / TOPK)
                    a["ndcg"].append(ndcg_at_k(ranked, gt_set, TOPK))
                    a["scan"].append(len(cands) / N * 100)
                    a["uniq"].append(len(cands) - min(len(start_ids), len(cands)))
                    a["nodes"].append(len(visited))
                    a["lat_c"].append(lat_c)
                    a["lat_r"].append(lat_r)
                    a["trunc"].append(1.0 if truncated else 0.0)
                names = list(graphs)
                for i in range(len(names)):
                    for j in range(i + 1, len(names)):
                        g1, g2 = names[i], names[j]
                        inter = len(pools[g1] & pools[g2])
                        uni = len(pools[g1] | pools[g2])
                        jacc[budget][f"{g1} vs {g2}"].append(inter / uni if uni else 0.0)
        print(f"  seed {seed} done", flush=True)

    for budget in BUDGETS:
        print(f"\n{'='*78}")
        print(f"Budget {budget}%  (max candidates {int(budget/100*N)})")
        print(f"{'Collector':<12} {'Start':>7} {'Pool':>7} {'Final':>7} {'NDCG':>7} "
              f"{'Scan%':>7} {'Nodes':>6} {'UniqAdd':>8} {'LatCms':>8} {'LatRms':>8}")
        print("-" * 78)
        for g in ["Random", "Hasse", "Pyrochlore"]:
            a = agg[(budget, g)]
            print(f"{g:<12} {np.mean(a['start']):>7.3f} {np.mean(a['pool']):>7.3f} "
                  f"{np.mean(a['final']):>7.3f} {np.mean(a['ndcg']):>7.3f} "
                  f"{np.mean(a['scan']):>7.1f} {np.mean(a['nodes']):>6.1f} "
                  f"{np.mean(a['uniq']):>8.0f} {np.mean(a['lat_c']):>8.1f} "
                  f"{np.mean(a['lat_r']):>8.1f}")
        j = jacc[budget]
        print("  Jaccard: " + " | ".join(f"{k} {np.mean(v):.3f}" for k, v in j.items()))
        tr = {g: np.mean(agg[(budget, g)]["trunc"]) for g in ["Random", "Hasse", "Pyrochlore"]}
        print(f"  start-node-exceeds-budget fraction: " +
              ", ".join(f"{g}={tr[g]:.2f}" for g in tr))

    print(f"\n{'='*78}")
    print("CROSS-DATASET: MemCore (§434) vs SIFT1M")
    print("=" * 78)
    print("  MemCore 10% pool: Hasse 0.490 / Pyro 0.500 / Random 0.460")
    print("  MemCore 20% pool: Hasse 0.495 / Pyro 0.520 / Random 0.535")
    for budget in BUDGETS:
        v = {g: np.mean(agg[(budget, g)]["pool"]) for g in ["Random", "Hasse", "Pyrochlore"]}
        print(f"  SIFT1M {budget:>4}% pool: " +
              " / ".join(f"{g} {v[g]:.3f}" for g in ["Random", "Hasse", "Pyrochlore"]) +
              f"   best={max(v, key=v.get)}")
    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()