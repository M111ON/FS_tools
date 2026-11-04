"""Locked control: does geometric topology still win after matching degree
AND neighbour size?

Locked variables
  Partition      162 icosphere cells (identical for every graph)
  Start node     identical (nearest icosphere vertex of the query)
  Graphs         Ico / Config / ER / SizeRand
                   Ico      geometric (icosphere vertex adjacency)
                   Config   configuration model, exact same degree sequence
                   ER       Erdos-Renyi, same edge count
                   SizeRand degree exact AND each neighbour replaced by a
                            random cell in the SAME log2 size bin
                            (neighbour-size profile matched, geometry destroyed)
  Budgets        5% / 10% / 20%
  Seeds          5 graph seeds x 5 query subsets x 20 queries = 100 paired samples
  Metrics        PoolRecall, Recall@10, NDCG@10, Scan%, Latency(collect,rerank),
                 UniqueCandidates, query-level paired difference vs Ico
  Decision rule  Ico must still beat Config/ER/SizeRand at equal budget after
                 degree + neighbour-size matching, with a paired test that
                 excludes zero, otherwise topology's advantage is bucket-size.

Scope note: this is evidence about SEMANTIC CANDIDATE RETRIEVAL on SIFT, not
about LC addressing, memory layout, or storage.
"""
import sys, os, time
from collections import defaultdict, deque
import numpy as np
from scipy import stats
sys.path.insert(0, '.')

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_F = os.path.join(HERE, 'sift', 'sift_base.fvecs')
QUERY_F = os.path.join(HERE, 'sift', 'sift_query.fvecs')
GT_F = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')

BUDGETS = [5.0, 10.0, 20.0]
SEEDS = [0, 1, 2, 3, 4]
QPS = 20
TOPK = 10


# ---------- io ----------
def load_fvecs_u8(path):
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
            out[s:e] = np.clip(blk.reshape(e - s, row)[:, 1:], 0, 255).astype(np.uint8)
    return out


def load_ivecs(path):
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row = dim + 1
    n = size // (4 * row)
    out = np.empty((n, dim), dtype=np.int64)
    CH = 100_000
    with open(path, 'rb') as f:
        for s in range(0, n, CH):
            e = min(s + CH, n)
            blk = np.fromfile(f, dtype=np.int32, count=(e - s) * row)
            out[s:e] = blk.reshape(e - s, row)[:, 1:]
    return out


def l2_topk(X, q, idx, k, chunk=50_000):
    qf = q.astype(np.int32)
    bd, bi = [], []
    for s in range(0, len(idx), chunk):
        sub = X[idx[s:s + chunk]].astype(np.int32)
        d = np.einsum('ij,ij->i', sub - qf, sub - qf)
        kk = min(k, len(d))
        part = np.argpartition(d, kk - 1)[:kk]
        bd.append(d[part]); bi.append(idx[s:s + chunk][part])
    d = np.concatenate(bd); i = np.concatenate(bi)
    return [int(x) for x in i[np.argsort(d)[:k]]]


# ---------- icosphere ----------
def icosphere(level):
    PHI = (1 + 5 ** 0.5) / 2
    v = np.array([(0,1,PHI),(0,1,-PHI),(0,-1,PHI),(0,-1,-PHI),(1,PHI,0),
                  (1,-PHI,0),(-1,PHI,0),(-1,-PHI,0),(PHI,0,1),(PHI,0,-1),
                  (-PHI,0,1),(-PHI,0,-1)], dtype=float)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    f = np.array([(0,2,8),(0,2,10),(0,4,6),(0,4,8),(0,6,10),(1,3,9),(1,3,11),
                  (1,4,6),(1,4,9),(1,6,11),(2,5,7),(2,5,8),(2,7,10),(3,5,7),
                  (3,5,9),(3,7,11),(4,8,9),(5,8,9),(6,10,11),(7,10,11)], dtype=int)
    for _ in range(level):
        vl = list(v); mc = {}
        def gm(i, j):
            key = (min(i, j), max(i, j))
            if key not in mc:
                m = (vl[i] + vl[j]) / 2; m /= np.linalg.norm(m)
                mc[key] = len(vl); vl.append(m)
            return mc[key]
        nf = []
        for a, b, c in f:
            ab, bc, ca = gm(a, b), gm(b, c), gm(c, a)
            nf += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        f = np.array(nf); v = np.array(vl)
    return v, f


def vert_adj(nv, faces):
    adj = defaultdict(set)
    for f in faces:
        for k in range(3):
            a, b = f[k], f[(k + 1) % 3]
            adj[a].add(b); adj[b].add(a)
    return {i: set(adj.get(i, set())) for i in range(nv)}


# ---------- graph builders ----------
def config_model(deg, seed):
    rng = np.random.default_rng(seed)
    stubs = []
    for i, d in enumerate(deg):
        stubs += [i] * int(d)
    rng.shuffle(stubs)
    adj = defaultdict(set)
    for i in range(0, len(stubs) - 1, 2):
        a, b = stubs[i], stubs[i + 1]
        if a != b:
            adj[a].add(b); adj[b].add(a)
    return {i: set(adj.get(i, set())) for i in range(len(deg))}


def er_graph(n, m, seed):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    e = 0
    while e < m:
        a, b = int(rng.integers(0, n)), int(rng.integers(0, n))
        if a != b and b not in adj[a]:
            adj[a].add(b); adj[b].add(a); e += 1
    return {i: set(adj.get(i, set())) for i in range(n)}


def size_bin(size):
    return int(np.floor(np.log2(max(int(size), 1))))


def size_rand_graph(ico_adj, sizes, seed):
    """Degree exact; each neighbour replaced by a random cell from the same
    log2-size bin, so the neighbour-size profile is preserved while all
    geometric locality is destroyed. Directed access structure."""
    rng = np.random.default_rng(seed)
    bins = defaultdict(list)
    for i, s in enumerate(sizes):
        bins[size_bin(s)].append(i)
    adj = {}
    for i, nbrs in ico_adj.items():
        out = set()
        for nb in nbrs:
            pool = [x for x in bins[size_bin(sizes[nb])] if x != i]
            if not pool:
                continue
            out.add(int(rng.choice(pool)))
        while len(out) < len(nbrs):
            pool = [x for x in range(len(sizes)) if x != i and x not in out]
            if not pool:
                break
            out.add(int(rng.choice(pool)))
        adj[i] = out
    return adj


# ---------- metrics ----------
def ndcg(ranked, gt, k=10):
    dcg = sum(1.0 / np.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in gt)
    ideal = sum(1.0 / np.log2(i + 2) for i in range(min(k, len(gt))))
    return dcg / ideal if ideal else 0.0


def collect(cells, graph, start, max_c):
    a0 = cells[start]
    if len(a0) >= max_c:
        return a0[:max_c], {start}, True
    parts = [a0]; seen = len(a0); visited = {start}
    q = deque([start])
    while q and seen < max_c:
        node = q.popleft()
        for nb in graph[node]:
            if nb in visited:
                continue
            visited.add(nb); q.append(nb)
            a = cells[nb]
            take = min(len(a), max_c - seen)
            if take:
                parts.append(a[:take]); seen += take
            if seen >= max_c:
                break
        if seen >= max_c:
            break
    return (np.concatenate(parts) if len(parts) > 1 else parts[0]), visited, False


def main():
    print("=" * 80)
    print("LOCKED CONTROL: degree-matched AND neighbour-size-matched")
    print("=" * 80, flush=True)

    X = load_fvecs_u8(BASE_F); Q = load_fvecs_u8(QUERY_F); GT = load_ivecs(GT_F)
    N, dim = X.shape
    print(f"  base {X.shape} queries {Q.shape}", flush=True)

    rng = np.random.default_rng(0)
    si = rng.choice(N, 20000, replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)
    mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    v162, f162 = icosphere(2)
    ico_adj = vert_adj(162, f162)

    t0 = time.time()
    buckets = [[] for _ in range(162)]
    CH = 50_000
    for s in range(0, N, CH):
        e = min(s + CH, N)
        node = np.argmax(proj(X[s:e]) @ v162.T.astype(np.float32), axis=1)
        for nid in range(162):
            ids = np.nonzero(node == nid)[0]
            if len(ids):
                buckets[nid].append((ids + s).astype(np.int32))
    cells = {i: (np.concatenate(buckets[i]) if buckets[i] else np.empty(0, np.int32))
             for i in range(162)}
    sizes = [len(cells[i]) for i in range(162)]
    print(f"  cells built {time.time()-t0:.1f}s  max={max(sizes)} min={min(sizes)} "
          f"mean={np.mean(sizes):.0f}", flush=True)

    Qn = proj(Q)
    starts = [int(np.argmax(v162 @ Qn[qi])) for qi in range(len(Q))]

    deg = [len(ico_adj[i]) for i in range(162)]
    m_edges = sum(deg) // 2

    # neighbour-size diagnostics
    def nbr_sizes(adj):
        out = []
        for i in range(162):
            for nb in adj[i]:
                out.append(sizes[nb])
        return np.array(out, dtype=float)

    graphs_by_seed = {}
    for s in SEEDS:
        graphs_by_seed[s] = {
            "Ico": ico_adj,
            "Config": config_model(deg, seed=s),
            "ER": er_graph(162, m_edges, seed=s),
            "SizeRand": size_rand_graph(ico_adj, sizes, seed=s),
        }

    print("\n  neighbour-size profile (should match between Ico and SizeRand):")
    print(f"  {'Graph':<10} {'edges':>7} {'meanNbrSize':>12} {'medNbrSize':>11} "
          f"{'degMean':>8}")
    for g in ["Ico", "Config", "ER", "SizeRand"]:
        a = graphs_by_seed[0][g]
        ns = nbr_sizes(a)
        ne = sum(len(a[i]) for i in range(162))
        dm = np.mean([len(a[i]) for i in range(162)])
        print(f"  {g:<10} {ne:>7} {ns.mean():>12.0f} {np.median(ns):>11.0f} {dm:>8.2f}")

    # ---------- run ----------
    recs = {b: {g: [] for g in ["Ico", "Config", "ER", "SizeRand"]} for b in BUDGETS}
    ndcgs = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    latc = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    latr = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    uniq = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    truncf = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    pair = {b: {g: [] for g in ["Config", "ER", "SizeRand"]} for b in BUDGETS}

    t_all = time.time()
    for s in SEEDS:
        graphs = graphs_by_seed[s]
        qidx = np.random.default_rng(1000 + s).choice(len(Q), QPS, replace=False)
        for budget in BUDGETS:
            max_c = int(budget / 100.0 * N)
            for qi in qidx:
                q = Q[qi]
                gt = set(int(x) for x in GT[qi][:TOPK])
                start = starts[qi]
                per_g = {}
                for gname, graph in graphs.items():
                    tc = time.perf_counter()
                    cands, vis, tr = collect(cells, graph, start, max_c)
                    latc[budget][gname].append((time.perf_counter() - tc) * 1000)
                    t_r = time.perf_counter()
                    ranked = l2_topk(X, q, cands, TOPK)
                    latr[budget][gname].append((time.perf_counter() - t_r) * 1000)
                    pool = set(int(x) for x in cands)
                    r = len(gt & pool) / TOPK
                    per_g[gname] = r
                    recs[budget][gname].append(r)
                    ndcgs[budget][gname].append(ndcg(ranked, gt))
                    uniq[budget][gname].append(len(pool))
                    truncf[budget][gname].append(1.0 if tr else 0.0)
                for base in ["Config", "ER", "SizeRand"]:
                    pair[budget][base].append(per_g["Ico"] - per_g[base])
        print(f"  seed {s} done ({time.time()-t_all:.0f}s)", flush=True)

    # ---------- report ----------
    for budget in BUDGETS:
        print(f"\n{'='*80}")
        print(f"Budget {budget}%  (max candidates {int(budget/100*N)})")
        print(f"{'Graph':<10} {'PoolRec':>8} {'Recall@10':>10} {'NDCG@10':>8} "
              f"{'Scan%':>7} {'Uniq':>8} {'Trunc':>6} {'LatCms':>7} {'LatRms':>7}")
        print("-" * 80)
        for gname in ["Ico", "Config", "ER", "SizeRand"]:
            print(f"{gname:<10} {np.mean(recs[budget][gname]):>8.3f} "
                  f"{np.mean(recs[budget][gname]):>10.3f} "
                  f"{np.mean(ndcgs[budget][gname]):>8.3f} "
                  f"{np.mean(uniq[budget][gname])/N*100:>7.1f} "
                  f"{np.mean(uniq[budget][gname]):>8.0f} "
                  f"{np.mean(truncf[budget][gname]):>6.2f} "
                  f"{np.mean(latc[budget][gname]):>7.1f} "
                  f"{np.mean(latr[budget][gname]):>7.1f}")

        print("\n  Query-level paired difference vs Ico (n=%d):" % len(pair[budget]["Config"]))
        print(f"  {'vs':<10} {'meanDiff':>9} {'95% CI':>20} {'wins':>6} "
              f"{'ties':>6} {'losses':>7} {'t-p':>9} {'wilcoxon-p':>11}")
        for base in ["Config", "ER", "SizeRand"]:
            d = np.array(pair[budget][base])
            n = len(d)
            m = d.mean()
            se = d.std(ddof=1) / np.sqrt(n)
            lo, hi = m - 1.96 * se, m + 1.96 * se
            wins = int((d > 0).sum()); ties = int((d == 0).sum()); loss = int((d < 0).sum())
            t_p = stats.ttest_1samp(d, 0.0).pvalue if n > 1 and d.std() > 0 else float('nan')
            try:
                w_p = stats.wilcoxon(d).pvalue if np.any(d != 0) else float('nan')
            except Exception:
                w_p = float('nan')
            print(f"  {base:<10} {m:>+9.3f} [{lo:>+7.3f},{hi:>+7.3f}] {wins:>6} "
                  f"{ties:>6} {loss:>7} {t_p:>9.2g} {w_p:>11.2g}")

    # ---------- verdict ----------
    print(f"\n{'='*80}")
    print("VERDICT (rule: Ico must still win after degree + neighbour-size matching)")
    print("=" * 80)
    for budget in BUDGETS:
        ok = []
        for base in ["Config", "ER", "SizeRand"]:
            d = np.array(pair[budget][base])
            m = d.mean()
            se = d.std(ddof=1) / np.sqrt(len(d))
            lo = m - 1.96 * se
            ok.append((base, m, lo))
        s = ", ".join(f"{b} {m:+.3f} (CI_lo {l:+.3f})" for b, m, l in ok)
        passed = all(l > 0 for _, _, l in ok)
        print(f"  {budget:>4}%: {s}   -> {'PASS' if passed else 'FAIL'}")

    print("\n  Scope: Semantic Candidate Retrieval on SIFT1M only — not LC addressing,")
    print("  memory layout, or storage.")
    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()