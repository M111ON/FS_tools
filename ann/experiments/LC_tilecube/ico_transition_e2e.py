"""End-to-end level transition: precompute + remap + shadow build + swap + reclaim,
with query service interleaved, for full argmax vs exact incremental remap.

Reports, for the SAME harness and the SAME resulting mapping:
    wall-clock per stage, downtime, queries served during the transition,
    query latency during vs baseline, reclaim cost
and answers the only question that matters here: does the 34.5x saving in
distance evaluations translate into transition wall-clock, or is it diluted by
the stages both paths must pay (buffer build, swap, reclaim)?
"""
import os, sys, time, gc
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, gt_set, l2_topk, _fvecs
from ico_cost_quality import icosphere, ndcg, vert_adj, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N = 300_000
NQ = 40
BUDGET = 5.0
LEVEL_FROM, LEVEL_TO = 2, 3
CHUNKS = 15


def build_cells(assign, nv):
    """Identical for both paths: group row ids by the new cell id."""
    order = np.argsort(assign, kind='stable')
    counts = np.bincount(assign, minlength=nv)
    cuts = np.cumsum(counts)[:-1]
    parts = np.split(order, cuts)
    return {i: (parts[i].astype(np.int32) if len(parts[i]) else np.empty(0, np.int32))
            for i in range(nv)}


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=N, replace=False)]
    qidx = rng.choice(N, size=NQ, replace=False)
    qs = X[qidx]

    ico = Ico(X, LEVEL_FROM)
    v2 = ico.v.astype(np.float64)
    v3, f3 = icosphere(LEVEL_TO)
    v3 = v3.astype(np.float64)
    N2, N3 = len(v2), len(v3)
    P = ico.proj(X).astype(np.float64)
    node2 = ico.assign(X)

    def serve(n=1):
        lat = []
        for i in range(n):
            q = qs[i % NQ]
            t0 = time.perf_counter()
            cands, vis, tr, h = ico.query(q, BUDGET)
            l2_topk(X, q, cands, TOPK)
            lat.append((time.perf_counter() - t0) * 1000)
        return lat

    base_lat = serve(NQ)
    print(f"N={N}  L{LEVEL_FROM}->L{LEVEL_TO}  budget {BUDGET}%")
    print(f"baseline query: p50 {np.percentile(base_lat,50):.1f} ms  "
          f"p95 {np.percentile(base_lat,95):.1f} ms\n")

    results = {}

    # ---------------- path 1: full argmax ----------------
    gc.collect()
    t_all0 = time.perf_counter()
    per_rows = N // CHUNKS + 1
    assign_full = np.empty(N, dtype=np.int32)
    lat_full = []
    t0 = time.perf_counter()
    for c, s in enumerate(range(0, N, per_rows)):
        e = min(s + per_rows, N)
        assign_full[s:e] = np.argmin(
            np.linalg.norm(P[s:e, None, :] - v3[None, :, :], axis=2), axis=1)
        lat_full += serve(1)
    t_remap_full = time.perf_counter() - t0

    t0 = time.perf_counter()
    cells_full = build_cells(assign_full, N3)
    t_build_full = time.perf_counter() - t0

    t0 = time.perf_counter()
    old_buffers = ico.buffers
    ico.buffers = [[cells_full[i]] if len(cells_full[i]) else [] for i in range(N3)]
    ico.cells = {}
    ico.v, ico.vc, ico.f = v3, v3.T.astype(np.float32), f3
    ico.adj = vert_adj(N3, f3)
    ico.nv = N3
    t_swap_full = time.perf_counter() - t0

    t0 = time.perf_counter()
    del old_buffers, cells_full
    gc.collect()
    t_reclaim_full = time.perf_counter() - t0
    t_all_full = time.perf_counter() - t_all0
    results["full"] = dict(pre=0.0, remap=t_remap_full, build=t_build_full,
                           swap=t_swap_full, reclaim=t_reclaim_full, total=t_all_full,
                           lat=lat_full, assign=assign_full)

    # ---------------- path 2: exact incremental ----------------
    gc.collect()
    t_all0 = time.perf_counter()
    t0 = time.perf_counter()
    order_per_cell, dv_per_cell = [], []
    for v in range(N2):
        dv = np.linalg.norm(v3 - v2[v], axis=1)
        o = np.argsort(dv, kind='stable')
        order_per_cell.append(o)
        dv_per_cell.append(dv[o])
    t_pre = time.perf_counter() - t0

    assign_inc = np.empty(N, dtype=np.int32)
    lat_inc = []
    per_cells = N2 // CHUNKS + 1
    t0 = time.perf_counter()
    for c, vs in enumerate(range(0, N2, per_cells)):
        ve = min(vs + per_cells, N2)
        for v in range(vs, ve):
            rows = np.nonzero(node2 == v)[0]
            if rows.size == 0:
                continue
            dv_s, o = dv_per_cell[v], order_per_cell[v]
            dpv = np.linalg.norm(P[rows] - v2[v], axis=1)
            m = dv_s[1] if len(dv_s) > 1 else np.inf
            skip = 2.0 * dpv < m
            assign_inc[rows[skip]] = v
            keep = rows[~skip]
            if keep.size == 0:
                continue
            k = np.maximum(np.searchsorted(dv_s, 2.0 * dpv[~skip], side='right'), 1)
            for kk in np.unique(k):
                sel = keep[k == kk]
                C = np.sort(o[:kk])
                d = np.linalg.norm(P[sel, None, :] - v3[None, C, :], axis=2)
                assign_inc[sel] = C[np.argmin(d, axis=1)]
        lat_inc += serve(1)
    t_remap_inc = time.perf_counter() - t0

    t0 = time.perf_counter()
    cells_inc = build_cells(assign_inc, N3)
    t_build_inc = time.perf_counter() - t0

    t0 = time.perf_counter()
    old_buffers = ico.buffers
    ico.buffers = [[cells_inc[i]] if len(cells_inc[i]) else [] for i in range(N3)]
    ico.cells = {}
    ico.v, ico.vc, ico.f = v3, v3.T.astype(np.float32), f3
    ico.adj = vert_adj(N3, f3)
    ico.nv = N3
    t_swap_inc = time.perf_counter() - t0

    t0 = time.perf_counter()
    del old_buffers, cells_inc
    gc.collect()
    t_reclaim_inc = time.perf_counter() - t0
    t_all_inc = time.perf_counter() - t_all0
    results["inc"] = dict(pre=t_pre, remap=t_remap_inc, build=t_build_inc,
                          swap=t_swap_inc, reclaim=t_reclaim_inc, total=t_all_inc,
                          lat=lat_inc, assign=assign_inc)

    # ---------------- report ----------------
    print(f"{'stage':<22} {'full argmax':>14} {'incremental':>14} {'ratio':>8}")
    print("-" * 62)
    for key, name in [("pre", "precompute"), ("remap", "remap (chunked)"),
                      ("build", "cell buffer build"), ("swap", "atomic swap"),
                      ("reclaim", "reclaim old"), ("total", "TOTAL wall-clock")]:
        a, b = results["full"][key], results["inc"][key]
        r = f"{a/b:.1f}x" if b > 1e-9 else "n/a"
        print(f"{name:<22} {a*1000:>11.0f} ms {b*1000:>11.0f} ms {r:>8}")
    print(f"{'queries served during':<22} {len(results['full']['lat']):>11}    "
          f"{len(results['inc']['lat']):>11}")

    for k, label in [("full", "full argmax"), ("inc", "incremental")]:
        l = results[k]["lat"]
        print(f"  query latency during {label:<12}: p50 {np.percentile(l,50):.1f} ms  "
              f"p95 {np.percentile(l,95):.1f} ms   (baseline p50 "
              f"{np.percentile(base_lat,50):.1f}, p95 {np.percentile(base_lat,95):.1f})")

    mism = int((results["inc"]["assign"] != results["full"]["assign"]).sum())
    print(f"\nmapping identical: {mism == 0}  (mismatches {mism})")

    # quality identical
    from ico_growth_fixed import gt_set  # noqa
    rec, nd = [], []
    for qi in qidx[:20]:
        gt = gt_set(X, X[qi], -1)
        cands, vis, tr, h = ico.query(X[qi], BUDGET)
        ranked = l2_topk(X, X[qi], cands, TOPK)
        rec.append(len(gt & set(ranked)) / TOPK)
        nd.append(ndcg(ranked, gt))
    print(f"post-transition quality: recall@10 {np.mean(rec):.4f}  "
          f"NDCG@10 {np.mean(nd):.4f}")

    # amortisation against the lifetime of the level
    print(f"\nAmortisation: transition wall-clock full {results['full']['total']:.2f}s vs "
          f"incremental {results['inc']['total']:.2f}s "
          f"-> saved {results['full']['total']-results['inc']['total']:.2f}s per level bump")
    print("  (compute-only speedup was 34.5x; the saving here is diluted by the")
    print("   buffer build, swap and reclaim that BOTH paths must pay)")


if __name__ == '__main__':
    main()