"""§587 note check: does the traversal order carry ANY ordering information?

preNDCG@10 ≈ 0 only says the collector's output order is uninformative AT k=10.
Measured at L2 / 20% (30 queries per dataset):
  spearman   Spearman(traversal position, similarity rank inside the pool)
             ~0 => no ordering signal; >0 => partial signal
  pre@10     NDCG@10 of the first 10 traversal items
  pre@100    NDCG@100 of the first 100 traversal items
  early      fraction of the pool's true top-10 found in the first len(pool)/10
             traversal positions (chance = 0.10)
"""
import os, sys, sqlite3
import numpy as np
from scipy import stats
sys.path.insert(0, '.')
from ico_cost_quality import icosphere, vert_adj, collect  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIFT_BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
SIFT_Q = os.path.join(HERE, 'sift', 'sift_query.fvecs')
SIFT_GT = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')
MEM_DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
LEVEL, BUDGET, NQ = 2, 20.0, 30
_D100 = np.array([1.0 / np.log2(i + 2) for i in range(100)])


def ndcg_k(ranked, gt, k):
    dcg = sum(1.0 / np.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in gt)
    return dcg / _D100[:k].sum()


def _fvecs(p):
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


def _ivecs(p):
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


def load_sift():
    X = _fvecs(SIFT_BASE); Q = _fvecs(SIFT_Q); GT = _ivecs(SIFT_GT)
    return X, Q.astype(np.float32), (lambda qi: GT[qi][:100].astype(int)), 'l2'


def load_memcore():
    con = sqlite3.connect(MEM_DB)
    rows = con.execute(
        "SELECT e.id, m.vec FROM entries e JOIN embeddings m ON m.entry_id = e.id").fetchall()
    con.close()
    X = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    X /= np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
    qpool = np.random.default_rng(777).choice(len(X), NQ, replace=False)
    Q = X[qpool]

    def gt100(qi):
        s = X @ Q[qi]
        s[qpool[qi]] = -2.0
        return np.argsort(-s)[:100].astype(int)

    return X, Q, gt100, 'cos'


def run(name, X, Q, gt100, metric):
    N = len(X)
    rng = np.random.default_rng(0)
    si = rng.choice(N, min(20000, N), replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32); mu32 = mu.astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu32) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    v, f = icosphere(LEVEL)
    nv = 10 * 4 ** LEVEL + 2
    Pn = proj(X)
    node = np.argmax(Pn @ v.T.astype(np.float32), axis=1)
    cells = {i: np.nonzero(node == i)[0].astype(np.int32) for i in range(nv)}
    adj = vert_adj(nv, f)
    max_c = int(BUDGET / 100.0 * N)

    qn = proj(Q)
    starts = [int(np.argmax(v @ qn[i])) for i in range(len(Q))]

    rhos, pre10, pre100, early = [], [], [], []
    for qi in range(min(NQ, len(Q))):
        cands, _, _, _ = collect(cells, adj, starts[qi], max_c)
        n = len(cands)
        if metric == 'cos':
            sim = X[cands] @ Q[qi]
        else:
            qf = Q[qi].astype(np.int32)
            sim = -np.einsum('ij,ij->i', X[cands].astype(np.int32) - qf,
                             X[cands].astype(np.int32) - qf)
        order = np.argsort(-sim)
        rank_of_pos = np.empty(n, dtype=np.int64)
        rank_of_pos[order] = np.arange(n)
        gt = set(int(x) for x in gt100(qi)[:100])
        pre10.append(ndcg_k([int(x) for x in cands[:10]], gt, 10))
        pre100.append(ndcg_k([int(x) for x in cands[:100]], gt, 100))
        rho, _ = stats.spearmanr(np.arange(n), rank_of_pos)
        rhos.append(rho)
        true_top10 = set(int(cands[i]) for i in order[:10])
        k = max(1, n // 10)
        early.append(len(true_top10 & set(int(x) for x in cands[:k])) / 10.0)

    print(f"  {name}  L{LEVEL} {BUDGET:.0f}% pool~{max_c} n_queries={len(rhos)}")
    print(f"    Spearman(traversal position, similarity rank) : {np.mean(rhos):+.4f}")
    print(f"    preNDCG@10                                    : {np.mean(pre10):.4f}")
    print(f"    preNDCG@100                                   : {np.mean(pre100):.4f}")
    print(f"    pool top-10 within first pool/10 positions    : {np.mean(early):.3f}"
          f"  (chance 0.100)")


def main():
    print("=" * 84)
    print("§587 CHECK: is the traversal order uninformative, or only at k=10?")
    print("=" * 84)
    X, Q, gt, m = load_sift()
    run("SIFT1M", X, Q, gt, m)
    del X, Q
    X, Q, gt, m = load_memcore()
    run("MemCore", X, Q, gt, m)


if __name__ == '__main__':
    main()