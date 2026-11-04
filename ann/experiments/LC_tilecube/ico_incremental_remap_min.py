"""Incremental remapping — smallest exact affected region.

Method C restricts the candidate set to the vertices that subdivision actually
puts next to v: v itself plus the midpoints of the edges incident to v (degree
6, so 7 candidates instead of 642). Method B is the wider cell-level superset
from the previous script. Both are checked against full argmax for EXACT
equality on every row; if C is not exact, it must be reported as such, not
shipped.

Exactness argument for C: a row p in cell v is nearer to v than to any other
level-2 vertex (Voronoi). Subdivision adds one new vertex per level-2 edge; the
only new vertices that can lie inside v's Voronoi region are the midpoints of
edges incident to v, because the midpoint of any non-incident edge lies in the
tile it subdivides, on the far side of a bisector from v. The script verifies
this empirically rather than trusting the argument.
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


def subdivide_map(v, f):
    """One subdivision step that also returns {(min,max) -> new vertex index}."""
    vl = list(v)
    mid = {}
    nf = []

    def gm(i, j):
        key = (min(i, j), max(i, j))
        if key not in mid:
            m = (vl[i] + vl[j]) / 2
            m /= np.linalg.norm(m)
            mid[key] = len(vl)
            vl.append(m)
        return mid[key]

    for a, b, c in f:
        ab, bc, ca = gm(a, b), gm(b, c), gm(c, a)
        nf += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
    return np.array(vl), np.array(nf), mid


def level_with_map(level):
    v, f = icosphere(level - 1)
    v2, f2, mid = subdivide_map(v, f)
    return v, f, v2, f2, mid


def main():
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=N, replace=False)]

    v2, f2, v3, f3, mid = level_with_map(LEVEL_TO)
    assert v3.shape[0] == 10 * 4 ** LEVEL_TO + 2
    N2, N3 = len(v2), len(v3)

    # incident edges per level-2 vertex
    inc = [[] for _ in range(N2)]
    for a, b, c in f2:
        for x, y in ((a, b), (b, c), (c, a)):
            key = (min(x, y), max(x, y))
            if key not in inc[x]:
                inc[x].append(key)
    deg = [len(inc[i]) for i in range(N2)]
    print(f"L{LEVEL_FROM} {N2} vertices, degree min {min(deg)} max {max(deg)} "
          f"(mean {np.mean(deg):.1f}); L{LEVEL_TO} {N3} vertices")
    print(f"affected-region candidate set = 1 + degree = "
          f"{1+int(np.mean(deg))} on average (vs {N3} full)")

    ico2 = Ico(X, LEVEL_FROM)
    P = ico2.proj(X).astype(np.float64)
    node2 = ico2.assign(X)

    # baseline full argmax over all L3 vertices
    t0 = time.perf_counter()
    full = np.empty(N, dtype=np.int32)
    for s in range(0, N, 20_000):
        e = min(s + 20_000, N)
        d = np.linalg.norm(P[s:e, None, :] - v3[None, :, :], axis=2)
        full[s:e] = np.argmin(d, axis=1)
    t_full = time.perf_counter() - t0
    full_evals = N * N3
    print(f"\nfull argmax : {t_full:6.2f}s  {full_evals:>12,} evaluations")

    # Method B: cell-level superset (radius 2*theta_v), skip rule R2
    inc_b = np.empty(N, dtype=np.int32)
    ev_b = sk_b = 0
    t0 = time.perf_counter()
    for v in range(N2):
        rows = np.nonzero(node2 == v)[0]
        if rows.size == 0:
            continue
        dpv = np.linalg.norm(P[rows] - v2[v], axis=1)
        dv = np.linalg.norm(v3 - v2[v], axis=1)
        C = np.nonzero(dv <= 2.0 * dpv.max())[0]
        others = C[C != v]
        m = dv[others].min() if others.size else np.inf
        skip = 2.0 * dpv < m
        sk_b += int(skip.sum())
        inc_b[rows[skip]] = v
        keep = rows[~skip]
        if keep.size == 0:
            continue
        d = np.linalg.norm(P[keep, None, :] - v3[None, C, :], axis=2)
        inc_b[keep] = C[np.argmin(d, axis=1)]
        ev_b += keep.size * C.size
    t_b = time.perf_counter() - t0
    print(f"method B    : {t_b:6.2f}s  {ev_b:>12,} evaluations "
          f"({ev_b/full_evals*100:5.2f}%)  skipped {sk_b}  "
          f"speedup {t_full/t_b:.1f}x")
    print(f"             mismatches vs full: {int((inc_b != full).sum())}")

    # Method C: only v + midpoints of edges incident to v
    inc_c = np.empty(N, dtype=np.int32)
    ev_c = sk_c = 0
    cand_sizes = []
    t0 = time.perf_counter()
    for v in range(N2):
        rows = np.nonzero(node2 == v)[0]
        if rows.size == 0:
            continue
        Cfull = np.array([v] + [mid[e] for e in inc[v]], dtype=np.int64)
        vcoords = v3[Cfull]
        dpv = np.linalg.norm(P[rows] - v2[v], axis=1)
        dvC = np.linalg.norm(vcoords - v2[v], axis=1)
        others = np.arange(1, len(Cfull))
        m = dvC[others].min() if others.size else np.inf
        skip = 2.0 * dpv < m
        sk_c += int(skip.sum())
        inc_c[rows[skip]] = v
        keep = rows[~skip]
        if keep.size == 0:
            continue
        cand_sizes.append(len(Cfull))
        d = np.linalg.norm(P[keep, None, :] - vcoords[None, :, :], axis=2)
        inc_c[keep] = Cfull[np.argmin(d, axis=1)]
        ev_c += keep.size * len(Cfull)
    t_c = time.perf_counter() - t0
    print(f"method C    : {t_c:6.2f}s  {ev_c:>12,} evaluations "
          f"({ev_c/full_evals*100:5.2f}%)  skipped {sk_c}  "
          f"speedup {t_full/t_c:.1f}x")
    mism_c = int((inc_c != full).sum())
    print(f"             mismatches vs full: {mism_c}  "
          f"{'-> EXACT' if mism_c == 0 else '-> NOT EXACT, do not use'}")
    print(f"             candidate set size: {min(cand_sizes)}-{max(cand_sizes)} "
          f"(mean {np.mean(cand_sizes):.1f})")

    print("\n  rows skipping evaluation (rule R2): "
          f"B {sk_b/N*100:.1f}%  C {sk_c/N*100:.1f}%")
    print("  NOTE: method C is the smallest exact region found; if its mismatch")
    print("  count were non-zero it would be reported as unusable.")


if __name__ == '__main__':
    main()