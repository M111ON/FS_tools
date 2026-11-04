"""Incremental remapping L2 -> L3 versus full argmax.

Claim under test: because level-n vertex indices are preserved in level n+1,
most rows keep their cell and only the cells whose boundary a row sits near
need re-evaluation. This must produce EXACTLY the same assignment as full
argmax — not an approximation.

Exact rules used (Euclidean metric on the unit 3-D projection, all vectors
unit length, so distances are chord lengths):

  cell v holds rows within theta_v = max_p ||p - v||
  a row p in cell v can only be nearest to an L3 vertex u with
      ||v - u|| <= ||v - p|| + ||p - u|| <= 2 * ||v - p|| <= 2 * theta_v     (R1)
  so the candidate set is  C_v = { u in L3 : ||v - u|| <= 2 * theta_v }      (superset)
  and v is strictly nearest for every u != v when
      2 * ||v - p|| < m_v,  with m_v = min_{u in C_v, u != v} ||v - u||     (R2)
  so rows satisfying (R2) are skipped without any distance evaluation.

Both rules are sufficient conditions; the script asserts equality with full
argmax over all 642 vertices for every row.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, gt_set, l2_topk, _fvecs
from ico_cost_quality import icosphere, ndcg, TOPK, vert_adj

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N = 300_000
NQ = 30
BUDGET = 5.0
LEVEL_FROM, LEVEL_TO = 2, 3


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=N, replace=False)]

    ico2 = Ico(X, LEVEL_FROM)
    v2 = ico2.v.astype(np.float64)
    v3, _ = icosphere(LEVEL_TO)
    assert v3.shape[0] == 10 * 4 ** LEVEL_TO + 2
    assert np.allclose(v3[:v2.shape[0]], v2, atol=1e-12), "level-2 vertices must be preserved"
    P = ico2.proj(X).astype(np.float64)          # N x 3 unit vectors
    N2, N3 = v2.shape[0], v3.shape[0]
    print(f"N={N}  L{LEVEL_FROM}: {N2} cells  L{LEVEL_TO}: {N3} cells "
          f"(level-{LEVEL_FROM} vertices preserved: {v2.shape[0]} of {N3})")

    # ---------- baseline: full argmax over all 642 vertices, float64 ----------
    t0 = time.perf_counter()
    full = np.empty(N, dtype=np.int32)
    for s in range(0, N, 20_000):
        e = min(s + 20_000, N)
        d = np.linalg.norm(P[s:e, None, :] - v3[None, :, :], axis=2)
        full[s:e] = np.argmin(d, axis=1)          # first minimum on ties
    t_full = time.perf_counter() - t0
    full_evals = N * N3
    print(f"\nfull argmax: {t_full:.2f}s, {full_evals:,} distance evaluations")

    # ---------- incremental ----------
    node2 = ico2.assign(X)                       # L2 cell per row
    t0 = time.perf_counter()
    inc = np.empty(N, dtype=np.int32)
    rows_skipped = 0
    rows_examined = 0
    inc_evals = 0
    cand_total = 0
    for v in range(N2):
        rows = np.nonzero(node2 == v)[0]
        if rows.size == 0:
            continue
        dpv = np.linalg.norm(P[rows] - v2[v], axis=1)        # ||p - v||
        theta = dpv.max()
        # R1: candidate superset for this cell
        dv = np.linalg.norm(v3 - v2[v], axis=1)
        C = np.nonzero(dv <= 2.0 * theta)[0]
        cand_total += C.size
        # R2: rows that cannot change cell, no distance evaluation at all
        others = C[C != v]
        m = dv[others].min() if others.size else np.inf
        skip = 2.0 * dpv < m
        rows_skipped += int(skip.sum())
        keep = rows[~skip]
        inc[rows[skip]] = v
        if keep.size == 0:
            continue
        rows_examined += keep.size
        # exact evaluation over the candidate superset only
        d = np.linalg.norm(P[keep, None, :] - v3[None, C, :], axis=2)
        inc[keep] = C[np.argmin(d, axis=1)]
        inc_evals += keep.size * C.size
    t_inc = time.perf_counter() - t0

    print(f"incremental: {t_inc:.2f}s, {inc_evals:,} distance evaluations "
          f"({inc_evals/full_evals*100:.2f}% of full)")
    print(f"  rows skipped without any evaluation : {rows_skipped:,} "
          f"({rows_skipped/N*100:.1f}%)")
    print(f"  rows examined                       : {rows_examined:,} "
          f"({rows_examined/N*100:.1f}%)")
    print(f"  mean candidate-set size              : "
          f"{cand_total/N2:.1f} cells (vs {N3} full)")
    print(f"  speedup {t_full/t_inc:.2f}x")

    # ---------- exactness ----------
    mism = int((inc != full).sum())
    print(f"\nEXACTNESS: {mism} mismatching rows out of {N}  -> "
          f"{'PASS (identical mapping)' if mism == 0 else 'FAIL'}")

    # ---------- quality must be identical ----------
    def build_node_array(assign):
        return assign

    ico2.node = inc
    ico2.buffers = [[] for _ in range(N3)]
    ico2.cells = {}
    for nid in range(N3):
        idx = np.nonzero(inc == nid)[0]
        if idx.size:
            ico2.buffers[nid] = [idx.astype(np.int32)]
    # swap the structure to L3: vertices, adjacency and cell count must follow
    v3f, f3 = icosphere(LEVEL_TO)
    ico2.v = v3f
    ico2.vc = v3f.T.astype(np.float32)
    ico2.f = f3
    ico2.adj = vert_adj(N3, f3)
    ico2.nv = N3
    qidx = rng.choice(N, size=NQ, replace=False)

    def quality(ico):
        rec, nd = [], []
        for qi in qidx:
            q = X[qi]
            gt = gt_set(X, q, -1)
            cands, vis, tr, h = ico.query(q, BUDGET)
            ranked = l2_topk(X, q, cands, TOPK)
            rec.append(len(gt & set(ranked)) / TOPK)
            nd.append(ndcg(ranked, gt))
        return float(np.mean(rec)), float(np.mean(nd))

    ico3f = Ico(X, LEVEL_TO)
    ico3f.node = full.copy()
    ico3f.buffers = [[] for _ in range(ico3f.nv)]
    ico3f.cells = {}
    for nid in range(ico3f.nv):
        idx = np.nonzero(full == nid)[0]
        if idx.size:
            ico3f.buffers[nid] = [idx.astype(np.int32)]

    r_inc, n_inc = quality(ico2)
    r_f, n_f = quality(ico3f)
    print(f"\nquality L3 via incremental: recall@10 {r_inc:.4f}  NDCG@10 {n_inc:.4f}")
    print(f"quality L3 via full argmax: recall@10 {r_f:.4f}  NDCG@10 {n_f:.4f}")
    print(f"  identical: {abs(r_inc-r_f) < 1e-12 and abs(n_inc-n_f) < 1e-12}")

    sizes = np.bincount(full, minlength=N3)
    print(f"\nmax cell after transition: {sizes.max()} = {sizes.max()/N*100:.2f}% of N "
          f"(budget {BUDGET}%)")


if __name__ == '__main__':
    main()