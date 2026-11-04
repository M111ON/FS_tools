"""Diagnostic: is the MemCore 'ranking loss' real, or near-duplicate tie noise?

For the L2 162-cell, 20% budget setup, per query:
  h            GT items present in the pool
  pool_gap     cos(10th GT item) - cos(best pool item that is NOT in GT)
               negative/zero => the reranker cannot separate them (tie/dup)
  ndcg_post    achieved NDCG@10
  ndcg_soft    NDCG@10 counting any pool item within 1e-4 cosine of a GT item
               as a hit (duplicate tolerant)
  n_dup        number of pool items within 1e-4 of the 10th GT cosine
"""
import sys, os, sqlite3
from collections import defaultdict, deque
import numpy as np
sys.path.insert(0, '.')
from ico_cost_quality import icosphere, vert_adj, collect, ceiling_ndcg, ndcg, TOPK, ico_counts

MEM_DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
LEVEL = 2
BUDGET = 20.0
EPS = 1e-4


def main():
    con = sqlite3.connect(MEM_DB)
    rows = con.execute(
        "SELECT e.id, e.content, m.vec FROM entries e JOIN embeddings m ON m.entry_id = e.id"
    ).fetchall()
    con.close()
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    X = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
    X /= np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
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
    nv, _, _ = ico_counts(LEVEL)
    Pn = proj(X)
    node = np.argmax(Pn @ v.T.astype(np.float32), axis=1)
    cells = {i: np.nonzero(node == i)[0].astype(np.int32) for i in range(nv)}
    adj = vert_adj(nv, f)

    qsel = np.random.default_rng(999).choice(N, size=100, replace=False)
    max_c = int(BUDGET / 100.0 * N)

    gaps, losses, soft_losses, hdup = [], [], [], []
    exact_dup_texts = 0
    for qi in qsel:
        qi = int(qi)
        sims = X @ X[qi]
        sims[qi] = -2.0
        gt = set(int(x) for x in np.argsort(-sims)[:TOPK])
        gt_min = min(sims[g] for g in gt)

        cands, _, _, _ = collect(cells, adj, int(node[qi]), max_c)
        pool = set(int(x) for x in cands)
        h = len(gt & pool)
        sims_pool = X[cands] @ X[qi]
        order = np.argsort(-sims_pool)[:TOPK]
        ranked = [int(cands[o]) for o in order]

        non_gt = [c for c in pool if c not in gt]
        best_non_gt = max((sims[c] for c in non_gt), default=-2.0)
        gaps.append(gt_min - best_non_gt)

        post = ndcg(ranked, gt)
        ceil = ceiling_ndcg(h)
        losses.append(ceil - post)

        # duplicate-tolerant: any item within EPS of a GT sim counts
        soft_hits = sum(1 for i, c in enumerate(ranked)
                        if c in gt or any(abs(sims[c] - sims[g]) <= EPS for g in gt))
        soft = sum(1.0 / np.log2(i + 2) for i in range(min(soft_hits, TOPK))) / sum(
            1.0 / np.log2(i + 2) for i in range(TOPK))
        soft_losses.append(ceil - soft)

        hdup.append(sum(1 for c in pool if abs(sims[c] - gt_min) <= EPS))

        # exact duplicate text count among GT
        if len({texts[g] for g in gt}) < len(gt):
            exact_dup_texts += 1

    print(f"level L{LEVEL} 162 cells, budget {BUDGET}%, 100 queries, N={N}")
    print(f"  mean ranking loss (ceiling - post)      : {np.mean(losses):.3f}")
    print(f"  mean loss after duplicate tolerance     : {np.mean(soft_losses):.3f}")
    print(f"  mean cos gap (10th GT - best non-GT)    : {np.mean(gaps):+.5f}")
    print(f"  queries with gap <= 0 (unseparable)     : {int(np.sum(np.array(gaps) <= 0))}/100")
    print(f"  queries with gap <= 1e-3                : {int(np.sum(np.array(gaps) <= 1e-3))}/100")
    print(f"  mean pool items tied with 10th GT (1e-4): {np.mean(hdup):.2f}")
    print(f"  queries whose GT top-10 contains exact duplicate texts: {exact_dup_texts}/100")


if __name__ == "__main__":
    main()