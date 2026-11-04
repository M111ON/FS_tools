"""Why does section A report ~0.78 NDCG at 20% when a direct check says recall 1.0?"""
import os, sys
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import _fvecs, Ico, gt_set, l2_topk
from ico_cost_quality import ndcg, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(5)
    N = len(X_all)
    sub = X_all[rng.choice(N, size=N, replace=False)]
    qidx = rng.choice(N, size=20, replace=False)
    ico = Ico(sub, 2)

    print(f"{'qi':>4} {'recall':>7} {'ndcg':>7} {'nGT_in_pool':>12} {'brute_ndcg':>11} "
          f"{'trunc':>6} {'hops':>5} {'pool':>8}")
    recs, nds = [], []
    for k, qi in enumerate(qidx):
        q = sub[qi]
        gt = gt_set(sub, q, int(qi))
        cands, vis, tr, hops = ico.query(q, 20.0)
        ranked = l2_topk(sub, q, cands, TOPK)
        r = len(gt & set(ranked)) / TOPK
        n_gt_in_pool = len(gt & set(int(x) for x in cands))
        # brute force over the whole subset, same GT definition
        brute = l2_topk(sub, q, np.arange(N), TOPK)
        bn = ndcg(brute, gt)
        recs.append(r); nds.append(ndcg(ranked, gt))
        print(f"{qi:>4} {r:>7.3f} {ndcg(ranked,gt):>7.3f} {n_gt_in_pool:>12} "
              f"{bn:>11.3f} {int(tr):>6} {hops:>5} {len(cands):>8}")
    print(f"\nmean recall@10={np.mean(recs):.3f}  mean NDCG@10={np.mean(nds):.3f}")
    print("brute_ndcg = NDCG@10 when the pool is the ENTIRE corpus (tie/GT sanity)")
    print("if brute_ndcg < 1.0, the GT set itself is not reproducible by the reranker")


if __name__ == '__main__':
    main()