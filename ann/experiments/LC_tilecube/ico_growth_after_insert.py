"""Does the max-cell criterion survive live growth (insert + compaction)?

Build at N0=100k, then grow to 2x/3x/5x by inserting into the EXISTING index
(no rebuild), and re-check:
    max_cell / N  versus  observed truncated-start-cell fraction
for two insert streams:
    uniform  random rows from the whole corpus (spread everywhere)
    skewed   a contiguous block of SIFT1M, which clusters by source image
and again after compaction (full reassignment at the current N).

The icosphere topology is fixed in all cases; only cell membership moves.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, gt_set, l2_topk, _fvecs, TRUNC_LIMIT
from ico_cost_quality import ndcg, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N0 = 100_000
STAGES = [100_000, 200_000, 300_000, 500_000]
LEVELS = [2, 3]
BUDGETS = [5.0, 10.0, 20.0]
NQ = 20


def measure(ico, X, qidx, budgets):
    out = {}
    for budget in budgets:
        tr, rec, nd, hops = [], [], [], []
        for qi in qidx:
            q = X[qi]
            gt = gt_set(X, q, -1)
            cands, vis, t, h = ico.query(q, budget)
            ranked = l2_topk(X, q, cands, TOPK)
            tr.append(1.0 if t else 0.0)
            rec.append(len(gt & set(ranked)) / TOPK)
            nd.append(ndcg(ranked, gt))
            hops.append(h)
        out[budget] = dict(trunc=float(np.mean(tr)), recall=float(np.mean(rec)),
                           ndcg=float(np.mean(nd)), hops=float(np.mean(hops)))
    return out


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(3)

    for stream in ["uniform", "skewed"]:
        print("=" * 104)
        print(f"STREAM: {stream}"
              + ("  (random rows spread over the whole corpus)"
                 if stream == "uniform" else
                 "  (contiguous block: descriptors cluster by source image)"))
        print("=" * 104)
        if stream == "uniform":
            order = rng.permutation(len(X_all))[:max(STAGES)]
        else:
            start_block = 500_000
            order = np.arange(start_block, start_block + max(STAGES))
        Xgrow = X_all[order]

        for level in LEVELS:
            qidx = rng.choice(N0, size=NQ, replace=False)
            ico = Ico(Xgrow[:N0], level)
            print(f"\n  level {level} ({ico.nv} cells)  build at N={N0} "
                  f"({ico.index_build:.2f}s)")
            print(f"  {'N':>8} {'metric':>10} {'maxCell/N':>10} {'maxCell':>9} "
                  f"{'budget':>6} {'trunc':>6} {'pred':>6} {'recall':>7} {'NDCG':>6} {'hops':>6}")
            prev = N0
            for N in STAGES:
                if N > prev:
                    newX = Xgrow[prev:N]
                    ico.X = Xgrow[:N]
                    ico.insert(newX)
                    prev = N
                sizes = [len(ico.cell(i)) for i in range(ico.nv)]
                mx = max(sizes)
                qs = qidx[qidx < N]
                m = measure(ico, Xgrow[:N], qs, BUDGETS)
                for budget in BUDGETS:
                    d = m[budget]
                    pred = mx / N > budget / 100.0
                    print(f"  {N:>8} {'live':>10} {mx/N*100:>9.2f}% {mx:>9} "
                          f"{budget:>5.0f}% {d['trunc']:>6.2f} "
                          f"{'TRUNC' if pred else 'ok':>6} {d['recall']:>7.3f} "
                          f"{d['ndcg']:>6.3f} {d['hops']:>6.1f}")
                # compaction at the current N
                t0 = time.perf_counter()
                ico2 = Ico(Xgrow[:N], level)
                comp = time.perf_counter() - t0
                sizes2 = [len(ico2.cell(i)) for i in range(ico2.nv)]
                mx2 = max(sizes2)
                m2 = measure(ico2, Xgrow[:N], qs, BUDGETS)
                for budget in BUDGETS:
                    d = m2[budget]
                    pred = mx2 / N > budget / 100.0
                    print(f"  {N:>8} {'compacted':>10} {mx2/N*100:>9.2f}% {mx2:>9} "
                          f"{budget:>5.0f}% {d['trunc']:>6.2f} "
                          f"{'TRUNC' if pred else 'ok':>6} {d['recall']:>7.3f} "
                          f"{d['ndcg']:>6.3f} {d['hops']:>6.1f}   (compact {comp:.2f}s)")
                del ico2
            del ico
        del Xgrow

    print(f"\n  pred = max_cell/N > budget (the criterion under test)")
    print("  Scope: candidate retrieval cost/quality only.")


if __name__ == '__main__':
    main()