"""Harness correctness + query-distribution effect (trimmed for runtime)."""
import os, sys
from collections import deque
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import _fvecs, Ico, gt_set, l2_topk
from ico_cost_quality import TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
QF = os.path.join(HERE, 'sift', 'sift_query.fvecs')
GTF = os.path.join(HERE, 'sift', 'sift_groundtruth.ivecs')


def ivecs(p):
    size = os.path.getsize(p)
    with open(p, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row = dim + 1
    n = size // (4 * row)
    out = np.empty((n, dim), dtype=np.int64)
    with open(p, 'rb') as f:
        for s in range(0, n, 100_000):
            e = min(s + 100_000, n)
            blk = np.fromfile(f, dtype=np.int32, count=(e - s) * row)
            out[s:e] = blk.reshape(e - s, row)[:, 1:]
    return out


def main():
    X = _fvecs(BASE)
    Q = _fvecs(QF)
    GT = ivecs(GTF)

    agree = sum(1 for qi in range(20) if gt_set(X, Q[qi], -1)
                == set(int(x) for x in GT[qi][:TOPK]))
    print(f"GT agreement with official ground truth: {agree}/20 queries exact match")

    ico = Ico(X, 2)
    rng = np.random.default_rng(7)
    base_q = X[rng.choice(len(X), 10, replace=False)]
    print("\nrecall@10 by query distribution (L2, 162 cells, 10 queries)")
    print(f"  {'query set':<20} {'budget':>7} {'recall':>7} {'hops':>6} {'pool':>8}")
    for label, QS in [("official", Q[:10]), ("uniform base", base_q)]:
        for budget in [20.0, 50.0]:
            recs, hops_l, sizes = [], [], []
            for q in QS:
                gt = gt_set(X, q, -1)
                cands, vis, tr, hops = ico.query(q, budget)
                ranked = l2_topk(X, q, cands, TOPK)
                recs.append(len(gt & set(ranked)) / TOPK)
                hops_l.append(hops); sizes.append(len(cands))
            print(f"  {label:<20} {budget:>6.0f}% {np.mean(recs):>7.3f} "
                  f"{np.mean(hops_l):>6.1f} {np.mean(sizes):>8.0f}")

    print("\nstart-cell size (L2)")
    for label, QS in [("official", Q[:200]), ("uniform base", X[rng.choice(len(X), 200, replace=False)])]:
        cells = []
        for q in QS:
            start = int(np.argmax(ico.v @ ico.proj(q.reshape(1, -1))[0]))
            cells.append(len(ico.cell(start)))
        print(f"  {label:<14} mean={np.mean(cells):>8.0f} median={np.median(cells):>7.0f} "
              f"min={np.min(cells):>5} max={np.max(cells):>6}")

    dist = {None: 0}
    all_d = []
    for qi in rng.choice(len(X), 30, replace=False):
        q = X[qi]
        start = int(np.argmax(ico.v @ ico.proj(q.reshape(1, -1))[0]))
        gt = gt_set(X, q, int(qi))
        dd = {start: 0}
        dq = deque([start])
        while dq:
            n = dq.popleft()
            for nb in ico.adj[n]:
                if nb not in dd:
                    dd[nb] = dd[n] + 1
                    dq.append(nb)
        all_d.extend(dd.get(int(ico.node[g]), 99) for g in gt)
    print(f"\ngraph distance from start cell to the cell of each true neighbour:")
    print(f"  mean={np.mean(all_d):.2f} median={np.median(all_d):.0f} "
          f"p90={np.percentile(all_d,90):.0f} max={np.max(all_d)}")


if __name__ == '__main__':
    main()