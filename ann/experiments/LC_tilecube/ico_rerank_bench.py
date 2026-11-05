"""Rerank cost: SIMD/BLAS-friendly kernels vs baseline (tb6848cad).

Baseline: np.linalg.norm(P[:,None,:]-V[None,:,:], axis=2) then argmin.
Faster options tested:
  A. squared-distance via BLAS: D2 = ||p||^2 + ||v||^2 - 2 p.v (unit vecs -> argmax of p.v)
  B. float32 dot (prefilter) + float64 rescore of top-M
  C. scipy.spatial.distance.cdist (euclidean)
Measures p50/p95 over repeated runs on a fixed candidate set.
"""
import os, sys, time
import numpy as np
from scipy.spatial.distance import cdist

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ico_lifecycle import _fvecs

BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')

def make_data(nq=200, ncand=50000, d=128, seed=1):
    X = _fvecs(BASE)
    rng = np.random.default_rng(seed)
    # pick ncand rows as candidate pool, nq queries
    cand_idx = rng.choice(len(X), size=ncand, replace=False)
    q_idx = rng.choice(len(X), size=nq, replace=False)
    P = X[q_idx].astype(np.float64)
    V = X[cand_idx].astype(np.float64)
    # normalize to unit for cosine-ish (sift is already ~unit after... keep raw)
    return P, V

def bench(name, fn, P, V, reps=5):
    # warmup
    fn(P[:1], V)
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        out = fn(P, V)
        times.append((time.perf_counter()-t0)*1000.0)
    times.sort()
    p50 = times[len(times)//2]
    p95 = times[int(0.95*(len(times)-1))]
    return p50, p95, out

def base(P, V):
    # P: (nq,d), V:(nc,d) -> argmin per query
    # chunk to avoid huge memory
    out = np.empty(P.shape[0], dtype=np.int64)
    for i in range(P.shape[0]):
        d = np.linalg.norm(P[i][None,:] - V, axis=1)
        out[i] = int(np.argmin(d))
    return out

def dot(P, V):
    # argmax of P@V.T (for unit vectors, min dist = max dot)
    S = P @ V.T
    return np.argmax(S, axis=1)

def dot_f32(P, V):
    S = (P.astype(np.float32) @ V.astype(np.float32).T)
    return np.argmax(S, axis=1)

def cdist_euclid(P, V):
    D = cdist(P, V, metric='euclidean')
    return np.argmin(D, axis=1)

def main():
    P, V = make_data(nq=50, ncand=50000)
    print(f"P={P.shape} V={V.shape}")
    methods = [("base_norm", base), ("dot_blas", dot), ("dot_f32", dot_f32),
               ("cdist", cdist_euclid)]
    results = {}
    for name, fn in methods:
        try:
            p50, p95, out = bench(name, fn, P, V, reps=4)
            results[name] = (p50, p95, out)
            print(f"{name:12s}  p50={p50:8.2f} ms  p95={p95:8.2f} ms")
        except Exception as e:
            print(f"{name:12s}  ERR {e}")
    # correctness: compare argmin indices to base (base uses euclidean; dot uses cosine-ish)
    # Only dot variants need unit-normalization for equivalence; report overlap
    base_out = results["base_norm"][2]
    for name in ["dot_blas", "dot_f32", "cdist"]:
        if name in results:
            o = results[name][2]
            overlap = float(np.mean(o == base_out))
            print(f"  {name} vs base argmin-index overlap = {overlap:.3f} (note: different metrics)")

if __name__ == "__main__":
    main()
