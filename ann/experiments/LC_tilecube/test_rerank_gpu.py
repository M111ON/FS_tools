"""Self-check for rerank_gpu.py (GPU rerank)."""
import os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ico_lifecycle import _fvecs
from rerank_gpu import rerank_cosine_gpu, rerank_topk_gpu, ResidentReranker, _HAS_TORCH

BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')

def test_cosine_gpu_correct():
    X = _fvecs(BASE)
    rng = np.random.default_rng(11)
    P = X[rng.choice(len(X), 15, replace=False)]
    V = X[rng.choice(len(X), 3000, replace=False)]
    got = rerank_cosine_gpu(P, V)
    Pu = P / (np.linalg.norm(P, axis=1, keepdims=True)+1e-12)
    Vu = V / (np.linalg.norm(V, axis=1, keepdims=True)+1e-12)
    ref = np.argmax(Pu @ Vu.T, axis=1)
    assert np.array_equal(got, ref), (got[:5], ref[:5])

def test_topk_gpu():
    X = _fvecs(BASE)
    rng = np.random.default_rng(12)
    P = X[rng.choice(len(X), 8, replace=False)]
    V = X[rng.choice(len(X), 2000, replace=False)]
    idx, dist = rerank_topk_gpu(P, V, k=6)
    assert idx.shape == (8, 6) and dist.shape == (8, 6)
    for i in range(8):
        assert np.all(np.diff(dist[i]) >= -1e-9)
        for j in range(6):
            d = np.linalg.norm(P[i].astype(np.float64)-V[idx[i,j]].astype(np.float64))
            assert abs(d-dist[i,j]) < 1e-6

def test_resident_reranker():
    X = _fvecs(BASE)
    rng = np.random.default_rng(14)
    P = X[rng.choice(len(X), 12, replace=False)]
    V = X[rng.choice(len(X), 1500, replace=False)]
    rr = ResidentReranker(V)
    got = rr.query(P)
    Pu = P/(np.linalg.norm(P,axis=1,keepdims=True)+1e-12)
    Vu = V/(np.linalg.norm(V,axis=1,keepdims=True)+1e-12)
    ref = np.argmax(Pu @ Vu.T, axis=1)
    assert np.array_equal(got, ref)
    idx, dist = rr.query_topk(P, k=4)
    assert idx.shape == (12, 4)

def test_speedup_vs_numpy():
    X = _fvecs(BASE)
    rng = np.random.default_rng(13)
    P = X[rng.choice(len(X), 32, replace=False)]
    V = X[rng.choice(len(X), 20000, replace=False)]
    t0=time.perf_counter(); rerank_cosine_gpu(P,V); tg=time.perf_counter()-t0
    Pu = P/(np.linalg.norm(P,axis=1,keepdims=True)+1e-12)
    Vu = V/(np.linalg.norm(V,axis=1,keepdims=True)+1e-12)
    t0=time.perf_counter(); np.argmax(Pu@Vu.T,axis=1); tn=time.perf_counter()-t0
    print(f'  gpu/cosine path {tg*1000:.1f}ms vs numpy {tn*1000:.1f}ms (torch={_HAS_TORCH})')

if __name__ == '__main__':
    t0=time.perf_counter()
    test_cosine_gpu_correct(); print('cosine_gpu_correct OK')
    test_topk_gpu(); print('topk_gpu OK')
    test_resident_reranker(); print('resident_reranker OK')
    test_speedup_vs_numpy(); print('speedup OK')
    print(f'ALL PASS in {time.perf_counter()-t0:.2f}s')
