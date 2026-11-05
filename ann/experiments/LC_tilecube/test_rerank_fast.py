"""Self-check for rerank_fast.py (tb6848cad)."""
import os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ico_lifecycle import _fvecs
from rerank_fast import rerank_euclid, rerank_dot, rerank_topk

BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')

def test_euclid_exact():
    X = _fvecs(BASE)
    rng = np.random.default_rng(5)
    P = X[rng.choice(len(X), 20, replace=False)].astype(np.float64)
    V = X[rng.choice(len(X), 5000, replace=False)].astype(np.float64)
    got = rerank_euclid(P, V)
    # reference: per-row argmin
    ref = np.array([int(np.argmin(np.linalg.norm(P[i][None,:]-V, axis=1))) for i in range(P.shape[0])])
    assert np.array_equal(got, ref)

def test_dot_matches_cosine():
    X = _fvecs(BASE)
    rng = np.random.default_rng(6)
    P = X[rng.choice(len(X), 20, replace=False)].astype(np.float64)
    V = X[rng.choice(len(X), 5000, replace=False)].astype(np.float64)
    Pn = P / (np.linalg.norm(P, axis=1, keepdims=True)+1e-12)
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True)+1e-12)
    got = rerank_dot(Pn, Vn)
    # reference: argmax cosine
    S = Pn @ Vn.T
    ref = np.argmax(S, axis=1)
    assert np.array_equal(got, ref)

def test_topk_shape_and_sorted():
    X = _fvecs(BASE)
    rng = np.random.default_rng(7)
    P = X[rng.choice(len(X), 10, replace=False)].astype(np.float64)
    V = X[rng.choice(len(X), 3000, replace=False)].astype(np.float64)
    idx, dist = rerank_topk(P, V, k=7)
    assert idx.shape == (10, 7) and dist.shape == (10, 7)
    # distances sorted ascending per row
    assert all(np.all(np.diff(dist[i]) >= -1e-9) for i in range(10))
    # idx/dist consistency
    for i in range(10):
        for j in range(7):
            d = np.linalg.norm(P[i]-V[idx[i,j]])
            assert abs(d-dist[i,j]) < 1e-6

def test_speedup():
    X = _fvecs(BASE)
    rng = np.random.default_rng(8)
    P = X[rng.choice(len(X), 30, replace=False)].astype(np.float64)
    V = X[rng.choice(len(X), 20000, replace=False)].astype(np.float64)
    t0=time.perf_counter(); rerank_euclid(P,V); te=time.perf_counter()-t0
    # baseline per-row
    t0=time.perf_counter()
    for i in range(P.shape[0]):
        np.argmin(np.linalg.norm(P[i][None,:]-V, axis=1))
    tb=time.perf_counter()-t0
    print(f"  euclid {te*1000:.1f} ms vs baseline {tb*1000:.1f} ms = {tb/te:.1f}x")
    assert te < tb

if __name__ == "__main__":
    t0=time.perf_counter()
    test_euclid_exact(); print("euclid_exact OK")
    test_dot_matches_cosine(); print("dot_matches_cosine OK")
    test_topk_shape_and_sorted(); print("topk OK")
    test_speedup(); print("speedup OK")
    print(f"ALL PASS in {time.perf_counter()-t0:.2f}s")
