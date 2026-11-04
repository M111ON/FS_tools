"""Growth sweep, corrected: the query is a corpus member, so it IS a legitimate
top-1 result. The earlier sweep excluded it from ground truth while the pool
still contained it, which capped NDCG at 9/10 hits = 0.780 for every query.

Here self is included in GT (correct when the query is itself a stored item).
Reports truncation (the level-bump signal), recall@10 and NDCG@10.
"""
import os, sys
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import _fvecs, Ico, gt_set, l2_topk, TRUNC_LIMIT
from ico_cost_quality import ndcg, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
NS = [10_000, 100_000, 1_000_000]
LEVELS = [1, 2, 3, 4]
BUDGETS = [5.0, 10.0, 20.0]
NQ = 20


def main():
    X_all = _fvecs(BASE)
    print("=" * 100)
    print("GROWTH (corrected GT: self included) — when must the level be bumped?")
    print("=" * 100)
    print(f"{'N':>9} {'lvl':>3} {'budget':>6} {'trunc':>6} {'recall':>7} {'NDCG':>6} "
          f"{'hops':>6} {'build_s':>8} {'verdict'}")
    rng = np.random.default_rng(5)
    for N in NS:
        sub = X_all[rng.choice(len(X_all), size=N, replace=False)]
        qidx = rng.choice(N, size=NQ, replace=False)
        for level in LEVELS:
            ico = Ico(sub, level)
            for budget in BUDGETS:
                tr_l, rec_l, nd_l, hops_l = [], [], [], []
                for qi in qidx:
                    q = sub[qi]
                    gt = gt_set(sub, q, -1)          # self included
                    cands, vis, tr, hops = ico.query(q, budget)
                    ranked = l2_topk(sub, q, cands, TOPK)
                    tr_l.append(1.0 if tr else 0.0)
                    rec_l.append(len(gt & set(ranked)) / TOPK)
                    nd_l.append(ndcg(ranked, gt))
                    hops_l.append(hops)
                t = float(np.mean(tr_l))
                print(f"{N:>9} {level:>3} {budget:>5.0f}% {t:>6.2f} {np.mean(rec_l):>7.3f} "
                      f"{np.mean(nd_l):>6.3f} {np.mean(hops_l):>6.1f} "
                      f"{ico.index_build:>8.2f} {'ok' if t <= TRUNC_LIMIT else 'TRUNC'}")
        del sub
    print(f"\n  truncation limit {TRUNC_LIMIT}; 'TRUNC' = the start cell alone exceeds the budget,")
    print("  so the budget cannot be honoured and the row is not a traversal.")


if __name__ == '__main__':
    main()