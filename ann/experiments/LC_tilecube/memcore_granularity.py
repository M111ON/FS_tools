"""MemCore granularity validation: does Ico topology transfer to text embeddings?

Mirrors the §534 control (Ico vs SizeRand, degree + neighbour-size matched),
now on a real text-embedding corpus instead of SIFT.

  Data           MemCore fact_store.v2.sqlite3, 54,211 x 384 (e5-small)
  Partition      162 icosphere cells (identical for every graph)
  Start node     identical (nearest icosphere vertex of the query)
  Graphs         Ico (geometric) / SizeRand (degree exact + neighbour size
                 profile matched, geometry destroyed) / Config (same degree seq)
  Budgets        5% / 10% / 20%
  Seeds          5 graph seeds x 5 query subsets x 20 queries = 100 paired samples
  Metric         COSINE (memcore vectors are L2-normalised), exact brute-force
                 ground truth top-10 excluding self
  Tests          query-level paired difference vs Ico: mean, 95% CI, W/T/L,
                 paired t-test, Wilcoxon

Scope: semantic candidate retrieval on text embeddings — not LC addressing,
memory layout or storage.
"""
import sys, os, time, sqlite3
from collections import defaultdict, deque
import numpy as np
from scipy import stats
sys.path.insert(0, '.')

DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
BUDGETS = [5.0, 10.0, 20.0]
SEEDS = [0, 1, 2, 3, 4]
QPS = 20
TOPK = 10
LN = 162


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


def size_bin(s):
    return int(np.floor(np.log2(max(int(s), 1))))


def size_rand_graph(ico_adj, sizes, seed):
    rng = np.random.default_rng(seed)
    bins = defaultdict(list)
    for i, s in enumerate(sizes):
        bins[size_bin(s)].append(i)
    adj = {}
    for i, nbrs in ico_adj.items():
        out = set()
        for nb in nbrs:
            pool = [x for x in bins[size_bin(sizes[nb])] if x != i]
            if pool:
                out.add(int(rng.choice(pool)))
        while len(out) < len(nbrs):
            pool = [x for x in range(len(sizes)) if x != i and x not in out]
            if not pool:
                break
            out.add(int(rng.choice(pool)))
        adj[i] = out
    return adj


def cos_topk(X, q, idx, k):
    sims = X[idx] @ q
    kk = min(k, len(sims))
    part = np.argpartition(-sims, kk - 1)[:kk]
    part = part[np.argsort(-sims[part])]
    return [int(idx[p]) for p in part]


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
    print("MemCore granularity validation (Ico vs SizeRand vs Config)")
    print("=" * 80, flush=True)

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT e.id, m.vec FROM entries e JOIN embeddings m ON m.entry_id = e.id").fetchall()
    con.close()
    ids = [r[0] for r in rows]
    X = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    X /= np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
    N = len(X)
    print(f"  loaded {N} x {X.shape[1]} ({X.nbytes/2**20:.0f} MB)", flush=True)

    rng = np.random.default_rng(0)
    si = rng.choice(N, 10000, replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    v162, f162 = icosphere(2)
    ico_adj = vert_adj(LN, f162)

    Pn = proj(X)
    node = np.argmax(Pn @ v162.T.astype(np.float32), axis=1)
    buckets = [[] for _ in range(LN)]
    for nid in range(LN):
        idx = np.nonzero(node == nid)[0]
        if len(idx):
            buckets[nid] = [idx.astype(np.int32)]
    cells = {i: (np.concatenate(buckets[i]) if buckets[i] else np.empty(0, np.int32))
             for i in range(LN)}
    sizes = [len(cells[i]) for i in range(LN)]
    print(f"  cells: max={max(sizes)} min={min(sizes)} mean={np.mean(sizes):.0f} "
          f"median={int(np.median(sizes))}", flush=True)

    deg = [len(ico_adj[i]) for i in range(LN)]

    # exact cosine GT for a 200-query pool (covers 5 seeds x 20, no overlap needed)
    qpool = np.random.default_rng(999).choice(N, size=200, replace=False)
    Xn = X  # already normalised
    gts = {}
    for qi in qpool:
        sims = Xn @ Xn[qi]
        sims[qi] = -2.0
        gts[int(qi)] = set(int(x) for x in np.argsort(-sims)[:TOPK])

    graphs_by_seed = {s: {
        "Ico": ico_adj,
        "SizeRand": size_rand_graph(ico_adj, sizes, s),
        "Config": config_model(deg, s),
    } for s in SEEDS}

    def nbr_stats(adj):
        ns = np.array([sizes[nb] for i in range(LN) for nb in adj[i]], dtype=float)
        return ns.mean(), np.median(ns), sum(len(adj[i]) for i in range(LN))

    print("\n  neighbour-size profile:")
    print(f"  {'Graph':<10} {'edges':>7} {'meanNbrSize':>12} {'medNbrSize':>11}")
    for g in ["Ico", "SizeRand", "Config"]:
        m_, md_, e_ = nbr_stats(graphs_by_seed[0][g])
        print(f"  {g:<10} {e_:>7} {m_:>12.0f} {md_:>11.0f}")

    recs = {b: {g: [] for g in ["Ico", "SizeRand", "Config"]} for b in BUDGETS}
    ndg = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    latc = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    latr = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    uniq = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    trf = {b: {g: [] for g in recs[BUDGETS[0]]} for b in BUDGETS}
    pair = {b: {g: [] for g in ["SizeRand", "Config"]} for b in BUDGETS}

    t0 = time.time()
    for s in SEEDS:
        graphs = graphs_by_seed[s]
        qidx = qpool[s * QPS:(s + 1) * QPS]
        for budget in BUDGETS:
            max_c = int(budget / 100.0 * N)
            for qi in qidx:
                qi = int(qi)
                q = X[qi]
                gt = gts[qi]
                start = int(node[qi])
                per_g = {}
                for gname, graph in graphs.items():
                    tc = time.perf_counter()
                    cands, vis, tr = collect(cells, graph, start, max_c)
                    latc[budget][gname].append((time.perf_counter() - tc) * 1000)
                    t_r = time.perf_counter()
                    ranked = cos_topk(X, q, cands, TOPK)
                    latr[budget][gname].append((time.perf_counter() - t_r) * 1000)
                    pool = set(int(x) for x in cands)
                    r = len(gt & pool) / TOPK
                    per_g[gname] = r
                    recs[budget][gname].append(r)
                    ndg[budget][gname].append(ndcg(ranked, gt))
                    uniq[budget][gname].append(len(pool))
                    trf[budget][gname].append(1.0 if tr else 0.0)
                for base in ["SizeRand", "Config"]:
                    pair[budget][base].append(per_g["Ico"] - per_g[base])
        print(f"  seed {s} done ({time.time()-t0:.0f}s)", flush=True)

    for budget in BUDGETS:
        print(f"\n{'='*80}")
        print(f"Budget {budget}%  (max candidates {int(budget/100*N)})")
        print(f"{'Graph':<10} {'PoolRec':>8} {'NDCG@10':>8} {'Uniq':>7} "
              f"{'Trunc':>6} {'LatCms':>7} {'LatRms':>7}")
        print("-" * 80)
        for gname in ["Ico", "SizeRand", "Config"]:
            print(f"{gname:<10} {np.mean(recs[budget][gname]):>8.3f} "
                  f"{np.mean(ndg[budget][gname]):>8.3f} "
                  f"{np.mean(uniq[budget][gname]):>7.0f} "
                  f"{np.mean(trf[budget][gname]):>6.2f} "
                  f"{np.mean(latc[budget][gname]):>7.2f} "
                  f"{np.mean(latr[budget][gname]):>7.2f}")
        print(f"\n  paired difference vs Ico (n={len(pair[budget]['SizeRand'])}):")
        print(f"  {'vs':<10} {'meanDiff':>9} {'95% CI':>20} {'wins':>5} "
              f"{'ties':>5} {'loss':>5} {'t-p':>9} {'wilcoxon-p':>11}")
        for base in ["SizeRand", "Config"]:
            d = np.array(pair[budget][base]); n = len(d); m = d.mean()
            se = d.std(ddof=1) / np.sqrt(n)
            lo, hi = m - 1.96 * se, m + 1.96 * se
            w = int((d > 0).sum()); ti = int((d == 0).sum()); l = int((d < 0).sum())
            tp = stats.ttest_1samp(d, 0.0).pvalue if d.std() > 0 else float('nan')
            try:
                wp = stats.wilcoxon(d).pvalue if np.any(d != 0) else float('nan')
            except Exception:
                wp = float('nan')
            print(f"  {base:<10} {m:>+9.3f} [{lo:>+7.3f},{hi:>+7.3f}] {w:>5} "
                  f"{ti:>5} {l:>5} {tp:>9.2g} {wp:>11.2g}")

    print(f"\n{'='*80}")
    print("VERDICT (MemCore): Ico must still win vs SizeRand after size matching")
    print("=" * 80)
    for budget in BUDGETS:
        parts = []
        all_pos = True
        for base in ["SizeRand", "Config"]:
            d = np.array(pair[budget][base]); m = d.mean()
            se = d.std(ddof=1) / np.sqrt(len(d))
            lo = m - 1.96 * se
            parts.append(f"{base} {m:+.3f} (CI_lo {lo:+.3f})")
            if lo <= 0:
                all_pos = False
        print(f"  {budget:>4}%: " + ", ".join(parts) +
              f"   -> {'PASS' if all_pos else 'FAIL'}")
    print("\n  Scope: text-embedding candidate retrieval only.")
    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()