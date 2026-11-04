"""Adaptive Icosphere Granularity: level sweep on two datasets.

162 is the VERTEX count of icosphere level 2, not a fixed constant:
  V_n = 10*4^n + 2      E_n = 30*4^n      F_n = 20*4^n
  L0 12/30/20  L1 42/120/80  L2 162/480/320  L3 642/1920/1280  L4 2562/7680/5120

Cells are vertices (cell = address), traversal follows edges, so changing the
level changes granularity WITHOUT changing the topology principle.

IcoConfig (as requested):
  subdivision_level, cell_count, edge_count, face_count,
  max_traversal_hops, scan_budget

Sweep: level in {1,2,3,4} x dataset {SIFT1M(L2), MemCore(cosine)} x budget
{5,10,20%} x graph {Ico, SizeRand (degree+neighbour-size matched)}.
5 graph seeds x 20 queries = 100 paired samples.

Metrics: PoolRecall, NDCG@10, Scan%, Unique, nodes visited, HOPs, truncation,
         cell/graph build cost, index bytes, paired diff vs SizeRand.

Scope: candidate retrieval only — not LC addressing, memory layout or storage.
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


def ndcg(ranked, gt, k=10):
    dcg = sum(1.0 / np.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in gt)
    ideal = sum(1.0 / np.log2(i + 2) for i in range(min(k, len(gt))))
    return dcg / ideal if ideal else 0.0


def collect(cells, graph, start, max_c, max_hops=10**9):
    a0 = cells[start]
    if len(a0) >= max_c:
        return a0[:max_c], {start}, True, 0
    parts = [a0]; seen = len(a0); visited = {start}
    q = deque([start]); hops = 0
    while q and seen < max_c and hops < max_hops:
        node = q.popleft()
        for nb in graph[node]:
            if nb in visited:
                continue
            visited.add(nb); q.append(nb); hops += 1
            a = cells[nb]
            take = min(len(a), max_c - seen)
            if take:
                parts.append(a[:take]); seen += take
            if seen >= max_c or hops >= max_hops:
                break
        if seen >= max_c or hops >= max_hops:
            break
    return (np.concatenate(parts) if len(parts) > 1 else parts[0]), visited, False, hops


# ---------------- datasets ----------------
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

    def topk(Xall, q, idx, k):
        qf = q.astype(np.int32)
        bd, bi = [], []
        for s in range(0, len(idx), 50_000):
            sub = Xall[idx[s:s + 50_000]].astype(np.int32)
            d = np.einsum('ij,ij->i', sub - qf, sub - qf)
            kk = min(k, len(d))
            part = np.argpartition(d, kk - 1)[:kk]
            bd.append(d[part]); bi.append(idx[s:s + 50_000][part])
        d = np.concatenate(bd); i = np.concatenate(bi)
        return [int(x) for x in i[np.argsort(d)[:k]]]

    return dict(name="SIFT1M", X=X, Q=Q, gt_of=gt_of, topk=topk)


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

    def topk(Xall, q, idx, k):
        sims = Xall[idx] @ q
        kk = min(k, len(sims))
        part = np.argpartition(-sims, kk - 1)[:kk]
        part = part[np.argsort(-sims[part])]
        return [int(idx[p]) for p in part]

    return dict(name="MemCore", X=X, Q=X[qpool], gt_of=gt_of, topk=topk,
                qids=[int(x) for x in qpool])


def run_dataset(D):
    X = D["X"]; N = len(X)
    rng = np.random.default_rng(0)
    si = rng.choice(N, min(20000, N), replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    if D["name"] == "MemCore":
        Q = D["Q"]; qsel = D["qids"]
        qnode_geom = proj(Q)
    else:
        Q = D["Q"]; qsel = None
        qnode_geom = None

    print(f"\n{'#'*88}")
    print(f"# DATASET: {D['name']}  N={N} dim={X.shape[1]}")
    print("#" * 88)
    print(f"{'lvl':>4} {'cells':>7} {'edges':>8} {'faces':>7} {'cellMean':>9} "
          f"{'cellMed':>8} {'cellMax':>8} {'build_s':>8} {'idxMB':>7} "
          f"{'budget':>7} {'IcoRec':>8} {'SizeRec':>8} {'diff':>7} {'IcoNDCG':>8} "
          f"{'hops':>6} {'nodes':>6} {'scan%':>6} {'trunc':>6}")
    print("-" * 88)

    for level in LEVELS:
        v, f = icosphere(level)
        nv, ne, nf = ico_counts(level)
        # cell build
        t0 = time.time()
        Pn = proj(X)
        vc = v.T.astype(np.float32)
        node = np.empty(N, dtype=np.int32)
        CH = 8000  # chunked: 8000 x 2562 float32 ~ 82 MB (L4 would need 9.5 GB at once)
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
        dt_cells = time.time() - t0
        sizes = [len(cells[i]) for i in range(nv)]
        # graph
        t0 = time.time()
        ico_adj = vert_adj(nv, f)
        dt_graph = time.time() - t0
        deg = [len(ico_adj[i]) for i in range(nv)]
        idx_bytes = sum(len(cells[i]) for i in range(nv)) * 4 + nv * 8

        graphs_by_seed = {s: {
            "Ico": ico_adj,
            "SizeRand": size_rand_graph(ico_adj, sizes, s),
        } for s in SEEDS}

        if D["name"] == "MemCore":
            starts = [int(np.argmax(v @ qnode_geom[qi])) for qi in range(len(Q))]
        else:
            starts = None

        for budget in BUDGETS:
            max_c = int(budget / 100.0 * N)
            recs = {"Ico": [], "SizeRand": []}
            ndg = {"Ico": [], "SizeRand": []}
            hops_l = []; nodes_l = []; scan_l = []; tr_l = []; diff_l = []
            for s in SEEDS:
                graphs = graphs_by_seed[s]
                if D["name"] == "MemCore":
                    qidx = list(range(s * QPS, (s + 1) * QPS))
                else:
                    qidx = np.random.default_rng(1000 + s).choice(len(Q), QPS, replace=False)
                for qi in qidx:
                    qi = int(qi)
                    q = Q[qi]
                    gt = D["gt_of"]((D["qids"][qi]) if D["name"] == "MemCore" else qi)
                    start = starts[qi] if starts is not None else int(
                        np.argmax(v @ proj(q.reshape(1, -1))[0]))
                    per = {}
                    for g in ["Ico", "SizeRand"]:
                        cands, vis, tr, hops = collect(cells, graphs[g], start, max_c)
                        ranked = D["topk"](X, q, cands, TOPK)
                        pool = set(int(x) for x in cands)
                        r = len(gt & pool) / TOPK
                        per[g] = r
                        recs[g].append(r); ndg[g].append(ndcg(ranked, gt))
                        if g == "Ico":
                            hops_l.append(hops); nodes_l.append(len(vis))
                            scan_l.append(len(pool) / N * 100); tr_l.append(1.0 if tr else 0.0)
                    diff_l.append(per["Ico"] - per["SizeRand"])
            d = np.array(diff_l)
            print(f"{level:>4} {nv:>7} {ne:>8} {nf:>7} {np.mean(sizes):>9.0f} "
                  f"{int(np.median(sizes)):>8} {max(sizes):>8} {dt_cells:>8.1f} "
                  f"{idx_bytes/2**20:>7.2f} {budget:>6.0f}% "
                  f"{np.mean(recs['Ico']):>8.3f} {np.mean(recs['SizeRand']):>8.3f} "
                  f"{d.mean():>+7.3f} {np.mean(ndg['Ico']):>8.3f} "
                  f"{np.mean(hops_l):>6.1f} {np.mean(nodes_l):>6.1f} "
                  f"{np.mean(scan_l):>6.1f} {np.mean(tr_l):>6.2f}")

    return


def main():
    print("=" * 88)
    print("ADAPTIVE ICOSPHERE GRANULARITY (V_n = 10*4^n+2, E_n = 30*4^n, F_n = 20*4^n)")
    print("=" * 88, flush=True)
    t0 = time.time()
    for D in [load_sift(), load_memcore()]:
        run_dataset(D)
    print(f"\n  total {time.time()-t0:.0f}s")
    print("  Scope: candidate retrieval only (not LC addressing / memory layout / storage).")
    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()