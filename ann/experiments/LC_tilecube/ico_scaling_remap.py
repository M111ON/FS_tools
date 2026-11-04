"""Scaling of the exact incremental remap (Method D) with the precompute cached.

Precompute (per-cell candidate order for the vertex pair L2->L3) depends only on
the two vertex sets, so it is computed ONCE and reused at every N; its size and
its one-off cost are reported separately, not charged per transition.

Measured at N = 300k / 600k / 1.2M:
    remap time              how it grows with N
    total transition        when the fixed tail starts to dominate
    peak memory             shadow build growth
    query p95               service impact as the index grows

N > 1,000,000 is drawn with replacement from the 1M SIFT base (duplicates do not
affect the scaling of the mapping work).

RCU/epoch reclamation remains unproven: the 3 ms swap here is a Python reference
assignment on a single thread, which is NOT production atomicity.
"""
import os, sys, time, gc, ctypes
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, _fvecs
from ico_cost_quality import icosphere, vert_adj

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
NS = [300_000, 600_000, 1_200_000]
NQ = 40
BUDGET = 5.0
LEVEL_FROM, LEVEL_TO = 2, 3
CHUNKS = 15


class MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def rss_mb():
    m = MEMSTATUS(); m.dwLength = ctypes.sizeof(MEMSTATUS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return (m.ullTotalPhys - m.ullAvailPhys) / 2 ** 20


def build_cells(assign, nv):
    order = np.argsort(assign, kind='stable')
    counts = np.bincount(assign, minlength=nv)
    parts = np.split(order, np.cumsum(counts)[:-1])
    return {i: (parts[i].astype(np.int32) if len(parts[i]) else np.empty(0, np.int32))
            for i in range(nv)}


def main():
    X_all = _fvecs(BASE)
    v2, f2 = icosphere(LEVEL_FROM)
    v3, f3 = icosphere(LEVEL_TO)
    v2 = v2.astype(np.float64); v3 = v3.astype(np.float64)
    N2, N3 = len(v2), len(v3)

    # ---- precompute ONCE (cache), reused at every N ----
    t0 = time.perf_counter()
    order_per_cell, dv_per_cell = [], []
    for v in range(N2):
        dv = np.linalg.norm(v3 - v2[v], axis=1)
        o = np.argsort(dv, kind='stable')
        order_per_cell.append(o)
        dv_per_cell.append(dv[o])
    t_pre = time.perf_counter() - t0
    cache_bytes = sum(a.nbytes for a in order_per_cell) + sum(a.nbytes for a in dv_per_cell)
    print(f"precompute cache: {t_pre*1000:.1f} ms one-off, {cache_bytes/2**20:.2f} MiB "
          f"({N2} x {N3}) — cached per vertex pair, not charged per transition\n")

    print(f"{'N':>10} {'remap_s':>9} {'build_s':>8} {'swap_s':>7} {'recl_s':>7} "
          f"{'total_s':>8} {'peak_MB':>8} {'struct_MB':>10} {'q_p95_ms':>9} {'base_p95':>9}")
    rows = []
    for N in NS:
        if N <= len(X_all):
            idx = np.random.default_rng(4).choice(len(X_all), size=N, replace=False)
        else:
            idx = np.random.default_rng(4).choice(len(X_all), size=N, replace=True)
        X = X_all[idx]
        qs = X[np.random.default_rng(9).choice(N, size=NQ, replace=False)]

        ico = Ico(X, LEVEL_FROM)
        P = ico.proj(X).astype(np.float64)
        node2 = ico.assign(X)
        v3f = v3.astype(np.float64)

        def serve(n=1):
            lat = []
            for i in range(n):
                q = qs[i % NQ]
                t0 = time.perf_counter()
                cands, vis, tr, h = ico.query(q, BUDGET)
                if isinstance(cands, np.ndarray) and cands.size:
                    sub = X[cands].astype(np.int32)
                    qf = q.astype(np.int32)
                    _ = np.sort(np.einsum('ij,ij->i', sub - qf, sub - qf))[:10]
                lat.append((time.perf_counter() - t0) * 1000)
            return lat

        base_lat = serve(NQ)

        gc.collect()
        rss0 = rss_mb()
        per_cells = max(1, N2 // CHUNKS)
        assign = np.empty(N, dtype=np.int32)
        lat = []
        t0 = time.perf_counter()
        for vs in range(0, N2, per_cells):
            ve = min(vs + per_cells, N2)
            for v in range(vs, ve):
                r = np.nonzero(node2 == v)[0]
                if r.size == 0:
                    continue
                dv_s, o = dv_per_cell[v], order_per_cell[v]
                dpv = np.linalg.norm(P[r] - v2[v], axis=1)
                m = dv_s[1] if len(dv_s) > 1 else np.inf
                skip = 2.0 * dpv < m
                assign[r[skip]] = v
                keep = r[~skip]
                if keep.size == 0:
                    continue
                k = np.maximum(np.searchsorted(dv_s, 2.0 * dpv[~skip], side='right'), 1)
                for kk in np.unique(k):
                    sel = keep[k == kk]
                    C = np.sort(o[:kk])
                    d = np.linalg.norm(P[sel, None, :] - v3f[None, C, :], axis=2)
                    assign[sel] = C[np.argmin(d, axis=1)]
            lat += serve(1)
        t_remap = time.perf_counter() - t0
        peak_mb = rss_mb() - rss0

        t0 = time.perf_counter(); cells = build_cells(assign, N3); t_build = time.perf_counter() - t0
        old = ico.buffers
        t0 = time.perf_counter()
        ico.buffers = [[cells[i]] if len(cells[i]) else [] for i in range(N3)]
        ico.cells = {}
        ico.v, ico.vc, ico.f = v3, v3.T.astype(np.float32), f3
        ico.adj = vert_adj(N3, f3)
        ico.nv = N3
        t_swap = time.perf_counter() - t0
        t0 = time.perf_counter(); del old, cells; gc.collect(); t_recl = time.perf_counter() - t0
        total = t_remap + t_build + t_swap + t_recl

        struct_mb = 12 * N / 2 ** 20      # old 4N + assign 4N + new cells 4N
        print(f"{N:>10} {t_remap:>9.2f} {t_build:>8.3f} {t_swap:>7.3f} {t_recl:>7.3f} "
              f"{total:>8.2f} {peak_mb:>8.1f} {struct_mb:>10.1f} "
              f"{np.percentile(lat,95):>9.1f} {np.percentile(base_lat,95):>9.1f}")
        rows.append((N, t_remap, total, struct_mb, np.percentile(lat, 95)))
        del X, P, qs, ico, assign, lat

    print(f"\nscaling of remap time (relative to N=300k):")
    b = rows[0][1]
    for N, t, tot, sm, p95 in rows:
        print(f"  N={N:>9,}  remap {t:>6.2f}s = {t/b:>5.2f}x   "
              f"total {tot:>6.2f}s = {tot/rows[0][2]:>5.2f}x   "
              f"(N ratio {N/rows[0][0]:.1f}x)")
    print(f"\nfixed tail (build+swap+reclaim) share of total:")
    for N, t, tot, sm, p95 in rows:
        print(f"  N={N:>9,}  remap {t/tot*100:5.1f}%  tail {(tot-t)/tot*100:5.1f}%")
    print("\n  RCU/epoch reclamation NOT proven: the 3 ms swap is a single-thread")
    print("  Python reference assignment, not production atomicity.")


if __name__ == '__main__':
    main()