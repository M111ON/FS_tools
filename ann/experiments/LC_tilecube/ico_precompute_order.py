"""Precompute the per-cell geometry for a (level-from, level-to) icosphere pair.

The candidate set C_v = { u in L_to : ||v - u|| <= 2*theta_v } and the
skip threshold m_v = min_{u in C_v, u != v} ||v - u|| depend only on the
level pair's vertices, NOT on the data. Only theta_v = max_p ||p - v|| is
data-dependent. So dv (N_from x N_to) and m_v (N_from,) can be built once
at index-build time and shipped as an asset; a transition then pays only
the data-dependent theta + rerank.

Output: order_L{from}_L{to}.npz with keys:
  dv   : float64 (N_from, N_to)  chord distances between cell centers
  m    : float64 (N_from,)       min dv to a different vertex (inf if none)
  from_level, to_level (scalars)
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ico_cost_quality import icosphere

HERE = os.path.dirname(os.path.abspath(__file__))


def build(from_level, to_level):
    vf, _ = icosphere(from_level)
    vt, _ = icosphere(to_level)
    vf = vf.astype(np.float64)
    vt = vt.astype(np.float64)
    # chord distances (unit sphere -> chord length)
    # dv[v,u] = ||vf[v] - vt[u]||
    diff = vf[:, None, :] - vt[None, :, :]          # (Nf, Nt, 3)
    dv = np.linalg.norm(diff, axis=2)               # (Nf, Nt)
    # m_v: min distance to a vertex different from v.
    # Level-n vertices are preserved in level n+1 (prefix), so v maps to
    # vertex v in L_to; exclude it. For pairs where v >= N_to, take global min.
    m = np.empty(vf.shape[0], dtype=np.float64)
    for v in range(vf.shape[0]):
        row = dv[v].copy()
        if v < row.shape[0]:
            row[v] = np.inf
        m[v] = row.min() if np.isfinite(row).any() else np.inf
    return dv, m, vf.astype(np.float64), vt.astype(np.float64)


def asset_path(from_level, to_level):
    return os.path.join(HERE, f"order_L{from_level}_L{to_level}.npz")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="fl", type=int, default=2)
    ap.add_argument("--to", dest="tl", type=int, default=3)
    args = ap.parse_args()
    t0 = time.perf_counter()
    dv, m, vf, vt = build(args.fl, args.tl)
    dt = time.perf_counter() - t0
    path = asset_path(args.fl, args.tl)
    np.savez(path, dv=dv, m=m, vf=vf, vt=vt,
             from_level=np.int64(args.fl), to_level=np.int64(args.tl), version=np.int64(1))
    size = os.path.getsize(path)
    print(f"levels {args.fl}->{args.tl}: {dv.shape[0]} x {dv.shape[1]}  build {dt*1000:.2f} ms  "
          f"size {size/1024/1024:.3f} MiB  -> {path}")
    # sanity: candidate-set size at a representative theta
    print(f"dv range [{dv.min():.4f}, {dv.max():.4f}]  m range "
          f"[{np.nanmin(m):.4f}, {np.nanmax(m[np.isfinite(m)]):.4f}]")


if __name__ == "__main__":
    main()
