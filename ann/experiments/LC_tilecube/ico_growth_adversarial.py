"""Boundary of the max-cell guarantee: what breaks the criterion?

The uniform and moderately skewed streams keep max_cell/N stable. This test
pushes the adversarial case: all growth lands in the few largest cells (one
tenant / one topic / one image batch). It measures when the criterion stops
predicting, i.e. the condition a selector must guard against.
"""
import os, sys
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, gt_set, l2_topk, _fvecs
from ico_cost_quality import ndcg, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N0 = 100_000
STAGES = [100_000, 200_000, 300_000, 500_000]
LEVEL = 2
BUDGETS = [5.0, 10.0, 20.0]
NQ = 15
TOP_CELLS = 5


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(3)

    # assign the whole corpus once, to build an insert stream drawn from the
    # largest cells only
    probe = Ico(X_all, LEVEL)
    sizes = np.array([len(probe.cell(i)) for i in range(probe.nv)])
    order_cells = np.argsort(-sizes)
    big = set(int(c) for c in order_cells[:TOP_CELLS])
    print(f"largest {TOP_CELLS} cells hold "
          f"{sizes[order_cells[:TOP_CELLS]].sum() / len(X_all) * 100:.1f}% of 1M "
          f"(sizes {sizes[order_cells[:TOP_CELLS]].tolist()})")

    hot = np.nonzero(np.isin(probe.node, list(big)))[0]
    cold = np.nonzero(~np.isin(probe.node, list(big)))[0]
    print(f"stream: {len(hot)} hot-region rows available vs {len(cold)} elsewhere")
    del probe

    base_pick = rng.choice(cold, size=N0, replace=False)      # initial index: cold rows
    maxN = N0 + len(hot)
    stages = [s for s in STAGES if s <= maxN]
    if stages[-1] != maxN:
        stages.append(maxN)
    stream_pick = rng.choice(hot, size=maxN - N0, replace=False)
    order = np.concatenate([base_pick, stream_pick])
    Xgrow = X_all[order]

    ico = Ico(Xgrow[:N0], LEVEL)
    qidx = rng.choice(N0, size=NQ, replace=False)
    print(f"\nlevel {LEVEL}: build at N={N0}, then insert hot-region rows only")
    print(f"  {'N':>8} {'maxCell/N':>10} {'maxCell':>9} {'budget':>6} {'trunc':>6} "
          f"{'pred':>6} {'recall':>7} {'NDCG':>6} {'hops':>6}")
    prev = N0
    for N in stages:
        if N > prev:
            ico.X = Xgrow[:N]
            ico.insert(Xgrow[prev:N])
            prev = N
        sizes = [len(ico.cell(i)) for i in range(ico.nv)]
        mx = max(sizes)
        qs = qidx[qidx < N]
        for budget in BUDGETS:
            tr, rec, nd, hops = [], [], [], []
            for qi in qs:
                q = Xgrow[qi]
                gt = gt_set(Xgrow[:N], q, -1)
                cands, vis, t, h = ico.query(q, budget)
                ranked = l2_topk(Xgrow[:N], q, cands, TOPK)
                tr.append(1.0 if t else 0.0)
                rec.append(len(gt & set(ranked)) / TOPK)
                nd.append(ndcg(ranked, gt))
                hops.append(h)
            pred = mx / N > budget / 100.0
            print(f"  {N:>8} {mx/N*100:>9.2f}% {mx:>9} {budget:>5.0f}% "
                  f"{np.mean(tr):>6.2f} {'TRUNC' if pred else 'ok':>6} "
                  f"{np.mean(rec):>7.3f} {np.mean(nd):>6.3f} {np.mean(hops):>6.1f}")


if __name__ == '__main__':
    main()