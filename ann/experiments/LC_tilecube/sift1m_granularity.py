"""Granularity control on SIFT1M: is structure useful once cells are fine enough?

Diagnosis from §489: the 20-node FD partition is far too coarse for 1M items
(8 nodes > 50k, 97% of queries start in a >50k node), so the 5%/10% budget
experiment measured arbitrary truncation of a huge cell, not traversal.

This control holds the PARTITION fixed and varies only the TRAVERSAL GRAPH:
  Partition: 162 cells = nearest icosphere-L2 vertex of the unit PCA-3D
             projection (avg 6.2k items/cell, so budgets 5/10/20% are
             real traversals, not truncations).
  Graphs (162 nodes):
    Ico    - icosphere edge adjacency (geometric)
    Config - random graph with the SAME degree sequence (configuration model)
    ER     - Erdos-Renyi with the same edge count
  Plus the original 20-node FD partition (Hasse/Config) for reference.

Metrics: StartRecall, PoolRecall, FinalRecall, NDCG@10, Scan%, Nodes,
         UniqAdd, Jaccard, Latency.  Distance = exact L2.
Seeds: 5 x 20 queries.  Budgets: 5 / 10 / 20 %.

Python prototype only (#7825).
"""
import sys, os, time
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')

import fd3_hasse as fd
import icosphere_address as ico_geo

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_F = os.path.join(HERE, 'sift', 'sift_base.fvecs')
QUERY_F = os.path.join(HERE, 'sift', 'sift_query.fvecs')
GT_F = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')

BUDGETS = [5.0, 10.0, 20.0]
SEEDS = [0, 1, 2, 3, 4]
QPS = 20
TOPK = 10
CELL = 162


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


def face_adj(n_face, faces):
    e2f = defaultdict(list)
    for i, f in enumerate(faces):
        for k in range(3):
            a, b = f[k], f[(k + 1) % 3]
            e2f[(min(a, b), max(a, b))].append(i)
    adj = defaultdict(set)
    for _, fl in e2f.items():
        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                adj[fl[i]].add(fl[j]); adj[fl[j]].add(fl[i])
    return {i: set(adj.get(i, set())) for i in range(n_face)}


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
    for i in range(len(deg)):
        adj.setdefault(i, set())
    return adj


def er_graph(n, m, seed):
    rng = np.random.default_rng(seed)
    adj = defaultdict(set)
    e = 0
    while e < m:
        a, b = int(rng.integers(0, n)), int(rng.integers(0, n))
        if a != b and b not in adj[a]:
            adj[a].add(b); adj[b].add(a); e += 1
    for i in range(n):
        adj.setdefault(i, set())
    return adj


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


def run_partition(X, Q, GT, cells, starts, graphs, label):
    N = len(X)
    agg = {}
    jac = {b: defaultdict(list) for b in BUDGETS}
    for seed in SEEDS:
        qidx = np.random.default_rng(1000 + seed).choice(len(Q), QPS, replace=False)
        for budget in BUDGETS:
            for qi in qidx:
                q = Q[qi]
                gt = set(int(x) for x in GT[qi][:TOPK])
                start = starts[qi]
                max_c = int(budget / 100.0 * N)
                pools = {}
                for gname, graph in graphs.items():
                    tc = time.perf_counter()
                    cands, vis, tr = collect(cells, graph, start, max_c)
                    latc = (time.perf_counter() - tc) * 1000
                    tr0 = time.perf_counter()
                    ranked = l2_topk(X, q, cands, TOPK)
                    latr = (time.perf_counter() - tr0) * 1000
                    pools[gname] = set(int(x) for x in cands)
                    a = agg.setdefault((budget, gname), {
                        "s": [], "p": [], "f": [], "n": [], "sc": [],
                        "u": [], "nd": [], "lc": [], "lr": [], "tr": []})
                    a["s"].append(len(gt & set(int(x) for x in cells[start])) / TOPK)
                    a["p"].append(len(gt & pools[gname]) / TOPK)
                    a["f"].append(len(gt & set(ranked)) / TOPK)
                    a["nd"].append(ndcg(ranked, gt))
                    a["sc"].append(len(cands) / N * 100)
                    a["u"].append(len(cands) - min(len(cells[start]), len(cands)))
                    a["n"].append(len(vis))
                    a["lc"].append(latc); a["lr"].append(latr)
                    a["tr"].append(1.0 if tr else 0.0)
                names = list(graphs)
                for i in range(len(names)):
                    for j in range(i + 1, len(names)):
                        g1, g2 = names[i], names[j]
                        inter = len(pools[g1] & pools[g2])
                        uni = len(pools[g1] | pools[g2])
                        jac[budget][f"{g1}-{g2}"].append(inter / uni if uni else 0)

    print(f"\n{'#'*78}")
    print(f"# PARTITION: {label}")
    print("#" * 78)
    for budget in BUDGETS:
        print(f"\n-- budget {budget}% (max {int(budget/100*N)}) --")
        print(f"{'Graph':<8} {'Start':>7} {'Pool':>7} {'Final':>7} {'NDCG':>7} "
              f"{'Scan%':>7} {'Nodes':>6} {'Uniq':>8} {'Trunc':>6} "
              f"{'LatCms':>8} {'LatRms':>8}")
        for g in graphs:
            a = agg[(budget, g)]
            print(f"{g:<8} {np.mean(a['s']):>7.3f} {np.mean(a['p']):>7.3f} "
                  f"{np.mean(a['f']):>7.3f} {np.mean(a['nd']):>7.3f} "
                  f"{np.mean(a['sc']):>7.1f} {np.mean(a['n']):>6.1f} "
                  f"{np.mean(a['u']):>8.0f} {np.mean(a['tr']):>6.2f} "
                  f"{np.mean(a['lc']):>8.1f} {np.mean(a['lr']):>8.1f}")
        print("  Jaccard: " + " | ".join(f"{k} {np.mean(v):.3f}" for k, v in jac[budget].items()))


def main():
    print("=" * 78)
    print("SIFT1M granularity control: 162 cells vs 20 cells")
    print("=" * 78, flush=True)
    X = load_fvecs_u8(BASE_F); Q = load_fvecs_u8(QUERY_F); GT = load_ivecs(GT_F)
    N, dim = X.shape
    print(f"  base {X.shape} queries {Q.shape} gt {GT.shape}", flush=True)

    rng = np.random.default_rng(0)
    si = rng.choice(N, 20000, replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    # ---- 162-vertex icosphere cells ----
    v162, f162 = icosphere(2)
    vladj = vert_adj(162, f162)
    t0 = time.time()
    cells162 = [[] for _ in range(162)]
    CH = 50_000
    for s in range(0, N, CH):
        e = min(s + CH, N)
        Pn = proj(X[s:e])
        node = np.argmax(Pn @ v162.T.astype(np.float32), axis=1)
        for nid in range(162):
            ids = np.nonzero(node == nid)[0]
            if len(ids):
                cells162[nid].append((ids + s).astype(np.int32))
    cells162 = {i: (np.concatenate(cells162[i]) if cells162[i] else np.empty(0, np.int32))
                for i in range(162)}
    sizes = np.sort([len(cells162[i]) for i in range(162)])[::-1]
    print(f"  162 cells built {time.time()-t0:.1f}s  sizes top10={sizes[:10].tolist()} "
          f"max={sizes[0]} min={sizes[-1]}", flush=True)

    Qn = proj(Q)
    starts162 = [int(np.argmax(v162 @ Qn[qi])) for qi in range(len(Q))]

    deg = [len(vladj[i]) for i in range(162)]
    m_edges = sum(deg) // 2
    graphs162 = {
        "Ico": vladj,
        "Config": config_model(deg, seed=7),
        "ER": er_graph(162, m_edges, seed=7),
    }

    run_partition(X, Q, GT, cells162, starts162, graphs162,
                  "162 icosphere-vertex cells (avg ~6.2k)")

    # ---- 20-node FD partition for reference ----
    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = (face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)).astype(np.float32)
    ftf = np.array([fd.face_to_fd[i] for i in range(len(fd.faces2))])
    t0 = time.time()
    raw20 = [[] for _ in range(20)]
    for s in range(0, N, CH):
        e = min(s + CH, N)
        Pn = proj(X[s:e])
        node = ftf[np.argmax(Pn @ face_centers.T, axis=1)]
        for nid in range(20):
            ids = np.nonzero(node == nid)[0]
            if len(ids):
                raw20[nid].append((ids + s).astype(np.int32))
    cells20 = {i: (np.concatenate(raw20[i]) if raw20[i] else np.empty(0, np.int32))
               for i in range(20)}
    starts20 = [int(ftf[int(np.argmax(face_centers @ Qn[qi]))]) for qi in range(len(Q))]
    deg20 = [len(fd.hasse_adj[i]) for i in range(20)]
    graphs20 = {
        "Hasse": {i: set(fd.hasse_adj[i]) for i in range(20)},
        "Config": config_model(deg20, seed=7),
    }
    print(f"  20 cells built {time.time()-t0:.1f}s", flush=True)
    run_partition(X, Q, GT, cells20, starts20, graphs20,
                  "20 FD cells (avg ~50k, the §489 setup)")

    print("\n  PASS", flush=True)


if __name__ == "__main__":
    main()