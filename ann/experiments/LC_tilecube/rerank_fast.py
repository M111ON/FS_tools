"""Fast rerank kernels for LC/ANN candidate sets (tb6848cad).

Two exact-ish paths:
  - rerank_euclid: scipy.spatial.distance.cdist (BLAS, ~10x vs per-row norm)
  - rerank_dot: BLAS matrix multiply (argmax of inner product, ~72x) for
    unit-normalised / cosine spaces. For non-unit data, normalise first or
    use rerank_euclid.

Partial rerank: rerank_topk(P, V, k) ranks only the top-k per query using a
two-stage (float32 prefilter -> float64 rescore) path.
"""
import os, sys, time
import numpy as np
from scipy.spatial.distance import cdist

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def rerank_euclid(P, V):
    P = np.asarray(P, dtype=np.float64)
    V = np.asarray(V, dtype=np.float64)
    D = cdist(P, V, metric='euclidean')
    return np.argmin(D, axis=1)


def rerank_dot(P, V):
    P = np.asarray(P, dtype=np.float64)
    V = np.asarray(V, dtype=np.float64)
    S = P @ V.T
    return np.argmax(S, axis=1)


def rerank_topk(P, V, k=10, prefilter=4):
    """Two-stage: float32 dot prefilter keeps prefilter*k candidates, then
    float64 euclidean rescore to top-k. Returns (indices (nq,k), distances)."""
    P = np.asarray(P, dtype=np.float64)
    V = np.asarray(V, dtype=np.float64)
    nq = P.shape[0]
    # prefilter
    S = P.astype(np.float32) @ V.astype(np.float32).T  # (nq, nV)
    # keep top prefilter*k per query
    kk = min(prefilter * k, V.shape[0])
    cand = np.argpartition(-S, kk-1, axis=1)[:, :kk]  # (nq, kk)
    out_idx = np.empty((nq, k), dtype=np.int64)
    out_dist = np.empty((nq, k), dtype=np.float64)
    for i in range(nq):
        ci = cand[i]
        D = np.linalg.norm(P[i][None, :] - V[ci], axis=1)
        order = np.argsort(D)[:k]
        out_idx[i] = ci[order]
        out_dist[i] = D[order]
    return out_idx, out_dist


if __name__ == "__main__":
    from ico_lifecycle import _fvecs
    BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
    X = _fvecs(BASE)
    rng = np.random.default_rng(2)
    P = X[rng.choice(len(X), 30, replace=False)].astype(np.float64)
    V = X[rng.choice(len(X), 20000, replace=False)].astype(np.float64)
    i1 = rerank_euclid(P, V)
    i2 = rerank_dot(P / (np.linalg.norm(P, axis=1, keepdims=True)+1e-12),
                    V / (np.linalg.norm(V, axis=1, keepdims=True)+1e-12))
    print("euclid idx[:5]:", i1[:5])
    print("dot    idx[:5]:", i2[:5])
    idx, dist = rerank_topk(P, V, k=5)
    print("topk idx[0]:", idx[0], "dist:", np.round(dist[0], 3))
