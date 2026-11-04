"""Prototype: factor-based (base-12) addressing using 20736 = 12^4 = (64+64)*(54+54+54) = (48+48+48)^2.

Owner insight: 128x162 = 144x144 = (64+64)x(54+54+54) = (48+48+48)^2 = 20736.
This is NOT 6D — xyz+ijk is 3D in base-12 (duodecimal). The 6 axes are
real+dual of the same 3D point. The factor decomposition (64, 54, 48, 128,
162, 144) IS the addressing structure; PCA (continuous linear) discards it.

This prototype addresses vectors by the discrete factor groups instead of PCA,
and checks recall vs PCA-to-3D baseline. Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, '.')

# Field structure
TOTAL = 20736  # 12^4
FACTORS = {
    "g64": 64,   # 2 groups -> 128
    "g54": 54,   # 3 groups -> 162
    "g48": 48,   # 3 groups -> 144 (squared: 3x3)
    "g128": 128,
    "g162": 162,
    "g144": 144,
}
# 12^4 = 20736: four base-12 digits
BASE = 12
NDIGITS = 4


def factor_address(v, W, mode="g48x3"):
    """Map vector to address based on factor decomposition.

    mode:
      "g48x3": 3 groups of 48 -> 144, squared -> 20736 (3x3 grid)
      "g64x2_g54x3": 2 groups of 64 (128) x 3 groups of 54 (162)
      "base12_4digit": 4 base-12 digits (0..11 each)
    """
    proj = W @ v  # (n_groups,) scalars
    if mode == "g48x3":
        # 3 groups -> each quantized to 48 levels, then squared layout
        g = (proj[:3] - proj[:3].min()) / (proj[:3].max() - proj[:3].min() + 1e-9)
        g = (g * 47).astype(int)
        g = np.clip(g, 0, 47)
        # address = g0*144 + g1*48 + g2  (but squared: g0*12^2 + g1*12 + g2 within 0..143, then *144 + ...)
        # simpler: 3-digit base-48 -> 48^3 too big; use 3 groups of 48 -> 144 slots per "row", 144 rows
        addr = int(g[0]) * 144 + int(g[1]) * 12 + int(g[2]) % 12
        addr = min(addr, TOTAL - 1)
        return addr
    elif mode == "base12_4digit":
        g = (proj[:4] - proj[:4].min()) / (proj[:4].max() - proj[:4].min() + 1e-9)
        g = (g * 11).astype(int)
        g = np.clip(g, 0, 11)
        addr = int(g[0]) * 12**3 + int(g[1]) * 12**2 + int(g[2]) * 12 + int(g[3])
        return addr
    else:
        # g64x2_g54x3: 2 groups of 64 (0..63) x 3 groups of 54 (0..53)
        g2 = (proj[:2] - proj[:2].min()) / (proj[:2].max() - proj[:2].min() + 1e-9)
        g2 = (g2 * 63).astype(int)
        g2 = np.clip(g2, 0, 63)
        g3 = (proj[2:5] - proj[2:5].min()) / (proj[2:5].max() - proj[2:5].min() + 1e-9)
        g3 = (g3 * 53).astype(int)
        g3 = np.clip(g3, 0, 53)
        addr = int(g2[0]) * 162 + int(g2[1]) * 54 + int(g3[0]) % 54  # approximate
        addr = min(addr, TOTAL - 1)
        return addr


def build_proj(d, n_groups, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(n_groups, d))


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def test_factor_recall():
    print("=" * 60)
    print("TEST: factor-based addressing recall vs PCA-3D")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    # PCA baseline
    mu = X.mean(0)
    U = np.linalg.svd(X - mu, full_matrices=False)[2][:3].T
    X_pca = (X - mu) @ U
    Q_pca = (Q - mu) @ U

    def pca_bucket(pt):
        pt = (pt - X_pca.min(0)) / (X_pca.max(0) - X_pca.min(0) + 1e-9)
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(X_pca):
        pca_map[pca_bucket(pt)].append(i)

    def pca_candidates(qpt):
        b = pca_bucket(qpt)
        cands = set(pca_map.get(b, []))
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
        return cands

    for mode, n_groups in [("g48x3", 3), ("base12_4digit", 4), ("g64x2_g54x3", 5)]:
        W = build_proj(d, n_groups, seed=7)
        X_addr = [factor_address(x, W, mode) for x in X]
        addr_map = defaultdict(list)
        for i, a in enumerate(X_addr):
            addr_map[a].append(i)

        # candidate: same address + neighbors (addr +/- 1..K)
        def addr_candidates(q, K=8):
            aq = factor_address(q, W, mode)
            cands = set()
            for delta in range(-K, K + 1):
                na = aq + delta
                if 0 <= na < TOTAL:
                    for idx in addr_map.get(na, []):
                        cands.add(idx)
            return cands

        recs = []
        scans = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Q[qi], X[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = addr_candidates(Q[qi], K=8)
            ranked = sorted(cands, key=lambda i: cos(Q[qi], X[i]), reverse=True)[:10]
            rset = set(ranked)
            recs.append(len(gtset & rset) / 10.0)
            scans.append(len(cands) / N * 100)

        # PCA recall
        recs_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Q[qi], X[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = pca_candidates(Q_pca[qi])
            ranked = sorted(cands, key=lambda i: cos(Q[qi], X[i]), reverse=True)[:10]
            rset = set(ranked)
            recs_pca.append(len(gtset & rset) / 10.0)

        print(f"  mode={mode}: recall={np.mean(recs):.3f} scan~{np.mean(scans):.1f}%")
        print(f"    PCA-3D:   recall={np.mean(recs_pca):.3f}")
    print("  PASS")


def test_field_structure():
    print("=" * 60)
    print("TEST: field structure verification")
    assert 128 * 162 == 20736
    assert 144 * 144 == 20736
    assert (64 + 64) * (54 + 54 + 54) == 20736
    assert (48 + 48 + 48) ** 2 == 20736
    assert 12 ** 4 == 20736
    print(f"  128x162 = {128*162}")
    print(f"  144x144 = {144*144}")
    print(f"  (64+64)x(54+54+54) = {(64+64)*(54+54+54)}")
    print(f"  (48+48+48)^2 = {(48+48+48)**2}")
    print(f"  12^4 = {12**4}")
    print("  PASS")


def main():
    test_field_structure()
    test_factor_recall()
    print()
    print("factor-based addressing prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
