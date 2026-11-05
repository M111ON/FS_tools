"""GPU (and CPU-torch) rerank for LC/ANN candidate sets.

Uses PyTorch when available (CUDA if present, else CPU torch) for a
BLAS-backed matrix multiply (argmax of inner product). Falls back to the
numpy BLAS path in rerank_fast.py when torch is missing.

Measured on Colab T4 (see GPU-RERANK-RESULTS.md): 32-133x over scipy
cdist for fp32 cosine argmax; fp16 prefilter gives 97-100% agreement.
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import torch
    _HAS_TORCH = True
except Exception:
    torch = None
    _HAS_TORCH = False

from rerank_fast import rerank_dot as _rerank_dot_numpy


def _to_unit(X):
    n = np.linalg.norm(X, axis=1, keepdims=True)
    return X / np.clip(n, 1e-12, None)


def rerank_cosine_gpu(P, V, device=None):
    """Argmax cosine (inner product on unit vectors). Returns int indices."""
    P = np.asarray(P, dtype=np.float32)
    V = np.asarray(V, dtype=np.float32)
    if not _HAS_TORCH:
        Pu = _to_unit(P.astype(np.float64))
        Vu = _to_unit(V.astype(np.float64))
        return _rerank_dot_numpy(Pu, Vu)
    if device is None:
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    with torch.no_grad():
        Pt = torch.from_numpy(P).to(device)
        Vt = torch.from_numpy(V).to(device)
        Pt = Pt / (Pt.norm(dim=1, keepdim=True) + 1e-9)
        Vt = Vt / (Vt.norm(dim=1, keepdim=True) + 1e-9)
        S = Pt @ Vt.T
        return S.argmax(dim=1).cpu().numpy().astype(np.int64)


def rerank_cosine_fp16(P, V):
    """fp16 prefilter (GPU only); falls back to fp32 on CPU."""
    if not _HAS_TORCH or not torch.cuda.is_available():
        return rerank_cosine_gpu(P, V)
    P = np.asarray(P, dtype=np.float32); V = np.asarray(V, dtype=np.float32)
    with torch.no_grad():
        Pt = torch.from_numpy(P).half().cuda()
        Vt = torch.from_numpy(V).half().cuda()
        Pt = Pt / (Pt.norm(dim=1, keepdim=True) + 1e-9)
        Vt = Vt / (Vt.norm(dim=1, keepdim=True) + 1e-9)
        S = Pt @ Vt.T
        return S.argmax(dim=1).cpu().numpy().astype(np.int64)


def rerank_topk_gpu(P, V, k=10, prefilter=4):
    """Two-stage: fp32 (or fp16 on GPU) prefilter -> exact top-k per query."""
    idx = rerank_cosine_gpu(P, V) if False else None
    # simple: use cosine argmax is top-1 only; for top-k use prefilter+rescore
    P = np.asarray(P, dtype=np.float64); V = np.asarray(V, dtype=np.float64)
    nq = P.shape[0]
    # prefilter via gpu cosine matrix (nq x nV) -> top prefilter*k per row
    if _HAS_TORCH:
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        with torch.no_grad():
            Pt = torch.from_numpy(P.astype(np.float32)).to(device)
            Vt = torch.from_numpy(V.astype(np.float32)).to(device)
            Pt = Pt / (Pt.norm(dim=1, keepdim=True) + 1e-9)
            Vt = Vt / (Vt.norm(dim=1, keepdim=True) + 1e-9)
            S = (Pt @ Vt.T).cpu().numpy()
    else:
        Pu = _to_unit(P); Vu = _to_unit(V)
        S = Pu @ Vu.T
    kk = min(prefilter * k, V.shape[0])
    out_idx = np.empty((nq, k), dtype=np.int64)
    out_dist = np.empty((nq, k), dtype=np.float64)
    for i in range(nq):
        cand = np.argpartition(-S[i], kk - 1)[:kk]
        cand = cand[np.argsort(-S[i][cand])]
        D = np.linalg.norm(P[i][None, :] - V[cand], axis=1)
        order = np.argsort(D)[:k]
        out_idx[i] = cand[order]
        out_dist[i] = D[order]
    return out_idx, out_dist


if __name__ == '__main__':
    from ico_lifecycle import _fvecs
    BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
    X = _fvecs(BASE)
    rng = np.random.default_rng(3)
    P = X[rng.choice(len(X), 20, replace=False)]
    V = X[rng.choice(len(X), 5000, replace=False)]
    print('torch available:', _HAS_TORCH, 'cuda:', torch.cuda.is_available() if _HAS_TORCH else False)
    i = rerank_cosine_gpu(P, V)
    print('cosine gpu idx[:5]:', i[:5])
    idx, dist = rerank_topk_gpu(P, V, k=5)
    print('topk idx[0]:', idx[0])
