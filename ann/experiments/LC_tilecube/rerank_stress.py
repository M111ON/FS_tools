"""Stress / soak test for the rerank path (ResidentReranker).

Sustained load: N_ITER query batches over V (NCAND x 128), measuring
p50/p99/p999 latency and RSS drift (leak check). CPU (local) or GPU (colab).
"""
import os, sys, time, tracemalloc
import numpy as np
_HAS_RESOURCE = False
_HAS_PSUTIL = False
try:
    import resource as _resource
    _HAS_RESOURCE = True
except ImportError:
    pass
try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    pass

def rss_mb():
    if _HAS_RESOURCE:
        return _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024.0
    if _HAS_PSUTIL:
        return _psutil.Process().memory_info().rss / 1024 / 1024
    return float('nan')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ico_lifecycle import _fvecs
from rerank_gpu import ResidentReranker

BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N_ITER = 200
NQ = 64
NCAND = 50000

def main():
    X = _fvecs(BASE)
    rng = np.random.default_rng(99)
    V = X[rng.choice(len(X), NCAND, replace=False)].astype('float32')
    rr = ResidentReranker(V)
    P0 = X[rng.choice(len(X), NQ, replace=False)].astype('float32')
    _ = rr.query(P0)
    tracemalloc.start()
    lat = []
    rss0 = rss_mb()
    for it in range(N_ITER):
        P = X[rng.choice(len(X), NQ, replace=False)].astype('float32')
        t0 = time.perf_counter()
        _ = rr.query(P)
        lat.append((time.perf_counter()-t0)*1000.0)
    rss1 = rss_mb()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    lat = np.array(lat)
    print(f"iters={N_ITER}  nq={NQ}  ncand={NCAND}")
    print(f"  p50={np.percentile(lat,50):.2f} ms  p95={np.percentile(lat,95):.2f} ms  "
          f"p99={np.percentile(lat,99):.2f} ms  max={lat.max():.2f} ms")
    print(f"  mean={lat.mean():.2f} ms  std={lat.std():.2f} ms")
    print(f"  RSS start={rss0:.1f} MB  end={rss1:.1f} MB  delta={rss1-rss0:+.1f} MB")
    print(f"  traced peak={peak/1024/1024:.1f} MB")
    p99 = np.percentile(lat, 99)
    leak = rss1 - rss0
    print(f"  PASS criteria: p99<100ms ({p99<100})  rss_drift<50MB ({leak<50 or (rss0!=rss0)})")

if __name__ == '__main__':
    main()
