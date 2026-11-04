"""Ico Cost-Quality Benchmark (build latency, query latency p50/p95, hops,
candidate count, rerank latency, recall@10, NDCG@10 pre/post/ceiling).

Corrects the earlier apples-to-oranges comparison by measuring, on the SAME
candidate pool:
  ceiling_NDCG(pool)  best NDCG@10 achievable if the pool were ordered perfectly
                      ceiling(h) = sum_{i<h} 1/log2(i+2) / sum_{i<10} 1/log2(i+2)
  pre_NDCG            NDCG@10 of the traversal order itself (no similarity)
  post_NDCG           NDCG@10 after exact rerank (L2 for SIFT, cosine for MemCore)
  ranking_loss        ceiling - post   (what the reranker leaves on the table)
  rerank_gain         post - pre       (what reranking adds over traversal order)

Sweep: levels L1..L4, budgets 5/10/20%, 5 graph seeds x 20 queries = 100 queries.
Graph = Ico (icosphere vertex adjacency). Cells = vertices.

Scope: candidate retrieval cost/quality only — not LC addressing, memory layout
or storage.
"""
import sys, os, time, sqlite3
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

HERE = os.path.dirname(os.path.abspath(__file__))
SIFT_BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
SIFT_Q = os.path.join(HERE, 'sift', 'sift_query.fvecs')
SIFT_GT = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')
MEM_DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"

LEVELS = [1, 2, 3, 4]
BUDGETS = [5.0, 10.0, 20.0]
SEEDS = [0, 1, 2, 3, 4]
QPS = 20
TOPK = 10

# ideal DCG for binary gains, k=10
_D = np.array([1.0 / np.log2(i + 2) for i in range(TOPK)])
_IDEAL = _D.sum()


def ceiling_ndcg(h):
    """Best NDCG@10 achievable when only h of the 10 GT are in the pool."""
    h = int(min(max(h, 0), TOPK))
    return float(_D[:h].sum() / _IDEAL)


def ndcg(ranked, gt, k=TOPK):
    dcg = sum(1.0 / np.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in gt)
    return dcg / _IDEAL


def pct(v, p):
    return float(np.percentile(np.asarray(v, dtype=float), p)) if len(v) else float('nan')


def ico_counts(n):
    return (10 * 4 ** n + 2, 30 * 4 ** n, 20 * 4 ** n)


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


def collect(cells, graph, start, max_c):
    """Returns (candidates in traversal order, visited, truncated, hops)."""
    a0 = cells[start]
    if len(a0) >= max_c:
        return a0[:max_c], {start}, True, 0
    parts = [a0]; seen = len(a0); visited = {start}
    q = deque([start]); hops = 0
    while q and seen < max_c:
        node = q.popleft()
        for nb in graph[node]:
            if nb in visited:
                continue
            visited.add(nb); q.append(nb); hops += 1
            a = cells[nb]
            take = min(len(a), max_c - seen)
            if take:
                parts.append(a[:take]); seen += take
            if seen >= max_c:
                break
        if seen >= max_c:
            break
    return (np.concatenate(parts) if len(parts) > 1 else parts[0]), visited, False, hops


def load_sift():
    def fvecs(p):
        size = os.path.getsize(p)
        with open(p, 'rb') as f:
            dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
        row = dim + 1; n = size // (4 * row)
        out = np.empty((n, dim), dtype=np.uint8)
        with open(p, 'rb') as f:
            for s in range(0, n, 100_000):
                e = min(s + 100_000, n)
                blk = np.fromfile(f, dtype=np.float32, count=(e - s) * row)
                out[s:e] = np.clip(blk.reshape(e - s, row)[:, 1:], 0, 255).astype(np.uint8)
        return out

    def ivecs(p):
        size = os.path.getsize(p)
        with open(p, 'rb') as f:
            dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
        row = dim + 1; n = size // (4 * row)
        out = np.empty((n, dim), dtype=np.int64)
        with open(p, 'rb') as f:
            for s in range(0, n, 100_000):
                e = min(s + 100_000, n)
                blk = np.fromfile(f, dtype=np.int32, count=(e - s) * row)
                out[s:e] = blk.reshape(e - s, row)[:, 1:]
        return out

    X = fvecs(SIFT_BASE); Q = fvecs(SIFT_Q); GT = ivecs(SIFT_GT)

    def gt_of(qi):
        return set(int(x) for x in GT[qi][:TOPK])

    def rank(Xall, q, idx, k):
        qf = q.astype(np.int32)
        bd, bi = [], []
        for s in range(0, len(idx), 50_000):
            sub = Xall[idx[s:s + 50_000]].astype(np.int32)
            d = np.einsum('ij,ij->i', sub - qf, sub - qf)
            kk = min(k, len(d))
            part = np.argpartition(d, kk - 1)[:kk]
            bd.append(d[part]); bi.append(idx[s:s + 50_000][part])
        d = np.concatenate(bd); i = np.concatenate(bi)
        o = np.argsort(d)[:k]
        return [int(i[p]) for p in o]

    return dict(name="SIFT1M", X=X, Q=Q, gt_of=gt_of, rank=rank)


def load_memcore():
    con = sqlite3.connect(MEM_DB)
    rows = con.execute(
        "SELECT e.id, m.vec FROM entries e JOIN embeddings m ON m.entry_id = e.id").fetchall()
    con.close()
    X = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    X /= np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
    N = len(X)
    qpool = np.random.default_rng(999).choice(N, size=200, replace=False)
    gtmap = {}
    for qi in qpool:
        s = X @ X[int(qi)]
        s[int(qi)] = -2.0
        gtmap[int(qi)] = set(int(x) for x in np.argsort(-s)[:TOPK])

    def gt_of(qi):
        return gtmap[int(qi)]

    def rank(Xall, q, idx, k):
        sims = Xall[idx] @ q
        kk = min(k, len(sims))
        part = np.argpartition(-sims, kk - 1)[:kk]
        part = part[np.argsort(-sims[part])]
        return [int(idx[p]) for p in part]

    return dict(name="MemCore", X=X, Q=X[qpool], gt_of=gt_of, rank=rank,
                qids=[int(x) for x in qpool])


def run(D):
    X = D["X"]; N = len(X)
    rng = np.random.default_rng(0)
    si = rng.choice(N, min(20000, N), replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    Q = D["Q"]
    is_mem = D["name"] == "MemCore"

    print(f"\n{'#'*112}")
    print(f"# {D['name']}  N={N} dim={X.shape[1]}  metric={'cosine' if is_mem else 'L2'}")
    print("#" * 112)
    hdr = (f"{'lvl':>3} {'cells':>6} {'bld_cell':>8} {'bld_graph':>9} {'budget':>6} "
           f"{'recall':>7} {'ceil':>6} {'preNDCG':>8} {'postNDCG':>8} {'loss':>6} "
           f"{'gain':>6} {'hops':>6} {'cands':>7} {'trav p50':>8} {'trav p95':>8} "
           f"{'rank p50':>8} {'rank p95':>8} {'trunc':>6}")
    print(hdr)
    print("-" * 112)

    for level in LEVELS:
        v, f = icosphere(level)
        nv, ne, nf = ico_counts(level)

        t0 = time.perf_counter()
        Pn = proj(X)
        vc = v.T.astype(np.float32)
        node = np.empty(N, dtype=np.int32)
        CH = 8000
        for s0 in range(0, N, CH):
            e0 = min(s0 + CH, N)
            node[s0:e0] = np.argmax(Pn[s0:e0] @ vc, axis=1)
        buckets = [[] for _ in range(nv)]
        for nid in range(nv):
            idx = np.nonzero(node == nid)[0]
            if len(idx):
                buckets[nid] = [idx.astype(np.int32)]
        cells = {i: (np.concatenate(buckets[i]) if buckets[i] else np.empty(0, np.int32))
                 for i in range(nv)}
        bld_cell = time.perf_counter() - t0

        t0 = time.perf_counter()
        ico_adj = vert_adj(nv, f)
        bld_graph = time.perf_counter() - t0

        # starts (identical for every budget)
        if is_mem:
            Qn = proj(Q)
            starts = [int(np.argmax(v @ Qn[i])) for i in range(len(Q))]
        else:
            Qn = proj(Q)
            starts = [int(np.argmax(v @ Qn[i])) for i in range(len(Q))]

        for budget in BUDGETS:
            max_c = int(budget / 100.0 * N)
            rec, ceil_, pre, post, loss, gain = [], [], [], [], [], []
            hops_l, cand_l, trav, rank_t, tr_l = [], [], [], [], []
            for s in SEEDS:
                qidx = (range(s * QPS, (s + 1) * QPS) if is_mem
                        else np.random.default_rng(1000 + s).choice(len(Q), QPS, replace=False))
                for qi in qidx:
                    qi = int(qi)
                    q = Q[qi]
                    gt = D["gt_of"](D["qids"][qi] if is_mem else qi)
                    start = starts[qi]

                    t1 = time.perf_counter()
                    cands, vis, tr, hops = collect(cells, ico_adj, start, max_c)
                    trav.append((time.perf_counter() - t1) * 1000)

                    t1 = time.perf_counter()
                    ranked = D["rank"](X, q, cands, TOPK)
                    rank_t.append((time.perf_counter() - t1) * 1000)

                    pool = set(int(x) for x in cands)
                    h = len(gt & pool)
                    rec.append(h / TOPK)
                    ceil_.append(ceiling_ndcg(h))
                    pre.append(ndcg([int(x) for x in cands[:TOPK]], gt))
                    p = ndcg(ranked, gt)
                    post.append(p)
                    loss.append(ceiling_ndcg(h) - p)
                    gain.append(p - pre[-1])
                    hops_l.append(hops); cand_l.append(len(pool))
                    tr_l.append(1.0 if tr else 0.0)
            print(f"{level:>3} {nv:>6} {bld_cell:>8.2f} {bld_graph:>9.3f} {budget:>5.0f}% "
                  f"{np.mean(rec):>7.3f} {np.mean(ceil_):>6.3f} {np.mean(pre):>8.3f} "
                  f"{np.mean(post):>8.3f} {np.mean(loss):>6.3f} {np.mean(gain):>6.3f} "
                  f"{np.mean(hops_l):>6.1f} {np.mean(cand_l):>7.0f} "
                  f"{pct(trav,50):>8.2f} {pct(trav,95):>8.2f} "
                  f"{pct(rank_t,50):>8.2f} {pct(rank_t,95):>8.2f} "
                  f"{np.mean(tr_l):>6.2f}")


def main():
    print("=" * 112)
    print("ICO COST-QUALITY BENCHMARK (pre/post/ceiling NDCG on the same pool)")
    print("=" * 112, flush=True)
    t0 = time.time()
    for D in [load_sift(), load_memcore()]:
        run(D)
    print(f"\n  total {time.time()-t0:.0f}s")
    print("  Scope: candidate retrieval cost/quality only.")
    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()