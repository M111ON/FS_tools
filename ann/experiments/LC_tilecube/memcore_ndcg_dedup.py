"""MemCore NDCG: absolute vs duplicate-tolerant (tf73942fc follow-up).

Reports, on the 54k memcore corpus (Ico path, 20% budget):
  - NDCG@10 absolute (GT = top-10 cosine excluding self)
  - NDCG@10 dup-tolerant (GT expanded with near-duplicates cos>0.99)
"""
import os, sys, time, sqlite3
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# import needed helpers by exec
src = open(os.path.join(HERE, 'memcore_granularity.py')).read().split('def main()')[0]
exec(src)

DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
BUDGET = 20.0
TOPK = 10
NQ = 50
DUP_THR = 0.99

def main():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT e.id, m.vec FROM entries e JOIN embeddings m ON m.entry_id = e.id").fetchall()
    con.close()
    ids = [r[0] for r in rows]
    X = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    X /= np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
    N = len(X)
    print(f"loaded {N} x {X.shape[1]}", flush=True)

    rng = np.random.default_rng(0)
    si = rng.choice(N, 10000, replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)
    def proj(a):
        P = (a - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)
    v162, f162 = icosphere(2)
    P3 = proj(X)
    cells = defaultdict(list) if False else None
    from collections import defaultdict, deque
    cells = defaultdict(list)
    for i in range(N):
        d = np.linalg.norm(P3[i] - v162, axis=1)
        cells[int(np.argmin(d))].append(i)
    cells = {k: np.array(v, dtype=np.int32) for k, v in cells.items()}
    full_cells = {i: cells.get(i, np.empty(0, dtype=np.int32)) for i in range(len(v162))}
    graph = vert_adj(len(v162), f162)

    def cos_topk_local(X, q, cands, k):
        if len(cands) == 0:
            return []
        s = X[cands] @ q
        kk = min(k, len(cands))
        part = np.argpartition(-s, kk - 1)[:kk]
        part = part[np.argsort(-s[part])]
        return [int(cands[p]) for p in part]

    q_idx = rng.choice(N, NQ, replace=False)
    max_c = int(BUDGET / 100.0 * N)
    abs_ndcgs = []; dup_ndcgs = []
    t0 = time.time()
    for qi, qi_global in enumerate(q_idx):
        q = X[qi_global]
        # GT top-10 excluding self
        sims = X @ q
        sims[qi_global] = -1
        gt_ids = np.argsort(-sims)[:TOPK]
        gt_set = set(int(x) for x in gt_ids)
        # dup-tolerant GT: expand each GT with corpus items cos>thr
        dup_gt = set(gt_set)
        for g in gt_ids:
            s2 = X @ X[g]
            s2[g] = -1  # exclude self? keep others
            extras = np.where(s2 > DUP_THR)[0]
            for e in extras:
                dup_gt.add(int(e))
        # Ico candidate collection
        start = int(np.argmin(np.linalg.norm(P3[qi_global] - v162, axis=1)))
        cands, vis, tr = collect(full_cells, graph, start, max_c)
        pool = set(int(x) for x in cands)
        ranked = cos_topk_local(X, q, np.array(list(pool)), TOPK)
        abs_ndcgs.append(ndcg(ranked, gt_set))
        dup_ndcgs.append(ndcg(ranked, dup_gt))
        if qi < 3 or qi % 10 == 0:
            print(f"  q{qi}: abs_ndcg={abs_ndcgs[-1]:.3f} dup_ndcg={dup_ndcgs[-1]:.3f} "
                  f"|gt|={len(gt_set)} |dup_gt|={len(dup_gt)} pool={len(pool)}", flush=True)
    print(f"\nN={N}  queries={NQ}  budget={BUDGET}%  dup_thr={DUP_THR}")
    print(f"  absolute NDCG@10  = {np.mean(abs_ndcgs):.4f}  (std {np.std(abs_ndcgs):.4f})")
    print(f"  dup-tolerant NDCG@10 = {np.mean(dup_ndcgs):.4f}  (std {np.std(dup_ndcgs):.4f})")
    print(f"  mean |dup_gt| = {np.mean([len(dup_gt) for _ in [0]]):.1f}")
    print(f"  elapsed {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    main()
