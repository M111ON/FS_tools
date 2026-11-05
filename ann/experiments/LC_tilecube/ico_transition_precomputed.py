"""Transition L2 -> L3 using a PRECOMPUTED order asset (ico_precompute_order.py).

Exact equality with full argmin is asserted. Timing comparison vs the
on-the-fly incremental remap (ico_incremental_remap.py) is printed.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ico_lifecycle import Ico, _fvecs
from ico_precompute_order import asset_path

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')


def transition_precomputed(X, from_level, to_level, asset):
    ico_from = Ico(X, from_level)
    vf = asset['vf'].astype(np.float64)
    vt = asset['vt'].astype(np.float64)
    P = ico_from.proj(X).astype(np.float64)
    node_from = ico_from.assign(X)
    dv_pre = asset['dv']; m_pre = asset['m']

    t0 = time.perf_counter()
    inc = np.empty(X.shape[0], dtype=np.int32)
    rows_skipped = 0; rows_examined = 0; inc_evals = 0
    for v in range(vf.shape[0]):
        rows = np.nonzero(node_from == v)[0]
        if rows.size == 0:
            continue
        dpv = np.linalg.norm(P[rows] - vf[v], axis=1)
        theta = dpv.max()
        # candidate superset from PRECOMPUTED dv (no geometry recompute)
        C = np.nonzero(dv_pre[v] <= 2.0 * theta)[0]
        others = C[C != v]
        m = m_pre[v]
        skip = 2.0 * dpv < m
        rows_skipped += int(skip.sum())
        keep = rows[~skip]
        inc[rows[skip]] = v
        if keep.size == 0:
            continue
        rows_examined += keep.size
        d = np.linalg.norm(P[keep, None, :] - vt[None, C, :], axis=2)
        inc[keep] = C[np.argmin(d, axis=1)]
        inc_evals += keep.size * C.size
    t = time.perf_counter() - t0
    return inc, t, rows_skipped, rows_examined, inc_evals, P, vt


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--N", type=int, default=300_000)
    ap.add_argument("--from", dest="fl", type=int, default=2)
    ap.add_argument("--to", dest="tl", type=int, default=3)
    args = ap.parse_args()

    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=args.N, replace=False)]

    asset = np.load(asset_path(args.fl, args.tl))
    assert int(asset['from_level']) == args.fl and int(asset['to_level']) == args.tl

    # full argmin baseline
    ico_from = Ico(X, args.fl)
    P = ico_from.proj(X).astype(np.float64)
    vt = asset['vt'].astype(np.float64)
    t0 = time.perf_counter()
    full = np.empty(X.shape[0], dtype=np.int32)
    for s in range(0, X.shape[0], 20_000):
        e = min(s + 20_000, X.shape[0])
        d = np.linalg.norm(P[s:e, None, :] - vt[None, :, :], axis=2)
        full[s:e] = np.argmin(d, axis=1)
    t_full = time.perf_counter() - t0

    inc, t_inc, skipped, examined, evals, P2, vt2 = transition_precomputed(
        X, args.fl, args.tl, asset)

    assert np.array_equal(inc, full), "MISMATCH vs full argmin"
    print(f"N={args.N}  L{args.fl}->L{args.tl}")
    print(f"full argmin : {t_full:.2f}s")
    print(f"precomputed : {t_inc:.2f}s  (evals {evals:,}, skipped {skipped:,}, examined {examined:,})")
    print(f"speedup     : {t_full/t_inc:.2f}x   evals {evals/(X.shape[0]*vt.shape[0])*100:.2f}% of full")
    print("EXACT MATCH: yes")


if __name__ == "__main__":
    main()
