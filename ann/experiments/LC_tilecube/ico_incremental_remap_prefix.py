"""Smallest EXACT affected region, take 2.

Method C (v + midpoints of edges incident to v) failed: 15,816/300,000 mismatches.
Reason: an L2 cell is the Voronoi region of v and is much larger than the union
of the 6 incident triangles, so a row in cell v can be closer to a midpoint of a
NON-incident edge.

Method D keeps the R1 bound but applies it with the ROW's own distance instead
of the cell-wide maximum:
    u* is nearest  =>  ||v - u*|| <= 2 * ||v - p||
so for each row only the prefix of vertices sorted by distance from v needs
evaluation. Ties are resolved by index order to match full argmax exactly.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, _fvecs
from ico_cost_quality import icosphere

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N = 300_000
LEVEL_FROM, LEVEL_TO = 2, 3


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=N, replace=False)]
    v2, f2 = icosphere(LEVEL_FROM)
    v3, f3 = icosphere(LEVEL_TO)
    v2 = v2.astype(np.float64); v3 = v3.astype(np.float64)
    N2, N3 = len(v2), len(v3)

    ico2 = Ico(X, LEVEL_FROM)
    P = ico2.proj(X).astype(np.float64)
    node2 = ico2.assign(X)

    t0 = time.perf_counter()
    full = np.empty(N, dtype=np.int32)
    for s in range(0, N, 20_000):
        e = min(s + 20_000, N)
        full[s:e] = np.argmin(np.linalg.norm(P[s:e, None, :] - v3[None, :, :], axis=2), axis=1)
    t_full = time.perf_counter() - t0
    full_evals = N * N3
    print(f"full argmax: {t_full:.2f}s  {full_evals:,} evaluations")

    # precompute candidate order per cell (distance from v, ascending)
    order_per_cell, dv_per_cell = [], []
    t0 = time.perf_counter()
    for v in range(N2):
        dv = np.linalg.norm(v3 - v2[v], axis=1)
        o = np.argsort(dv, kind='stable')
        order_per_cell.append(o)
        dv_per_cell.append(dv[o])
    t_pre = time.perf_counter() - t0
    print(f"precompute per-cell candidate order: {t_pre*1000:.1f} ms "
          f"(one-off, {N2} x {N3} distances)")

    incD = np.empty(N, dtype=np.int32)
    ev_d = 0
    sk_d = 0
    ksum = 0
    t0 = time.perf_counter()
    for v in range(N2):
        rows = np.nonzero(node2 == v)[0]
        if rows.size == 0:
            continue
        dv_s = dv_per_cell[v]
        o = order_per_cell[v]
        dpv = np.linalg.norm(P[rows] - v2[v], axis=1)
        # R2 skip: v strictly nearest when 2*||p-v|| < distance to nearest other vertex
        m = dv_s[1] if len(dv_s) > 1 else np.inf
        skip = 2.0 * dpv < m
        sk_d += int(skip.sum())
        incD[rows[skip]] = v
        keep = rows[~skip]
        if keep.size == 0:
            continue
        k = np.searchsorted(dv_s, 2.0 * dpv[~skip], side='right')
        k = np.maximum(k, 1)
        ksum += int(k.sum())
        for kk in np.unique(k):
            sel = keep[k == kk]
            C = np.sort(o[:kk])                 # index order -> same tie-break as argmax
            d = np.linalg.norm(P[sel, None, :] - v3[None, C, :], axis=2)
            incD[sel] = C[np.argmin(d, axis=1)]
            ev_d += sel.size * kk
    t_d = time.perf_counter() - t0
    mism = int((incD != full).sum())
    print(f"\nmethod D (per-row radius): {t_d:.2f}s  {ev_d:,} evaluations "
          f"({ev_d/full_evals*100:.2f}%)  speedup incl. precompute "
          f"{t_full/(t_d+t_pre):.1f}x")
    print(f"  rows skipped (no evaluation): {sk_d:,} ({sk_d/N*100:.1f}%)")
    print(f"  mean candidate-set size      : {ksum/N:.2f} cells (vs {N3} full)")
    print(f"  mismatches vs full argmax    : {mism}  "
          f"{'-> EXACT' if mism == 0 else '-> NOT EXACT, do not use'}")

    print("\n  comparison")
    print(f"  {'method':<28} {'evaluations':>14} {'% of full':>10} {'exact':>7}")
    print(f"  {'full argmax':<28} {full_evals:>14,} {100.0:>10.2f} {'yes':>7}")
    print(f"  {'B cell-level superset':<28} {4_813_723:>14,} {2.50:>10.2f} {'yes':>7}")
    print(f"  {'C v + incident midpoints':<28} {1_599_664:>14,} {0.83:>10.2f} "
          f"{'NO (15816 bad)':>7}")
    print(f"  {'D per-row radius (prefix)':<28} {ev_d:>14,} {ev_d/full_evals*100:>10.2f} "
          f"{('yes' if mism==0 else 'NO'):>7}")


if __name__ == '__main__':
    main()