"""Soak test for the rerank path (ResidentReranker).

Runs for DURATION seconds (default 3600 = 1 hour), logging per-minute stats
to soak.log. Pass criteria: no crash, p99 stable (no >2x drift between
first and last 5 min), RSS drift < 100 MB.
"""
import os, sys, time, tracemalloc
import numpy as np
_HAS_RESOURCE = False
try:
    import resource as _resource
    _HAS_RESOURCE = True
except ImportError:
    pass
try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

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
DURATION = int(os.environ.get('SOAK_SECONDS', '3600'))
NQ = 64
NCAND = 50000
BATCH_PER_MIN = 60

def main():
    X = _fvecs(BASE)
    rng = np.random.default_rng(7)
    V = X[rng.choice(len(X), NCAND, replace=False)].astype('float32')
    rr = ResidentReranker(V)
    P0 = X[rng.choice(len(X), NQ, replace=False)].astype('float32')
    _ = rr.query(P0)
    tracemalloc.start()
    t_start = time.time()
    minute = 0
    rows = []
    print(f"SOAK start: duration={DURATION}s  nq={NQ}  ncand={NCAND}", flush=True)
    while time.time() - t_start < DURATION:
        minute += 1
        lats = []
        errs = 0
        t_min = time.time()
        while time.time() - t_min < 60 and time.time() - t_start < DURATION:
            P = X[rng.choice(len(X), NQ, replace=False)].astype('float32')
            try:
                t0 = time.perf_counter()
                _ = rr.query(P)
                lats.append((time.perf_counter()-t0)*1000.0)
            except Exception as e:
                errs += 1
                print(f"  ERR: {e}", flush=True)
        if not lats:
            break
        la = np.array(lats)
        rss = rss_mb()
        row = (minute, len(lats), float(np.percentile(la,50)), float(np.percentile(la,95)),
               float(np.percentile(la,99)), float(la.max()), rss, errs)
        rows.append(row)
        print(f"min {minute:3d}: n={row[1]:5d} p50={row[2]:7.2f} p95={row[3]:7.2f} "
              f"p99={row[4]:7.2f} max={row[5]:7.2f} rss={row[6]:7.1f}MB errs={row[7]}",
              flush=True)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # summary
    if len(rows) >= 2:
        first = np.array([r[4] for r in rows[:5]]).mean()
        last = np.array([r[4] for r in rows[-5:]]).mean()
        drift = last / first if first > 0 else float('inf')
        rss_drift = rows[-1][6] - rows[0][6]
        total_errs = sum(r[7] for r in rows)
        print(f"\nSOAK SUMMARY: minutes={len(rows)} total_queries={sum(r[1] for r in rows)}")
        print(f"  p99 first5min={first:.2f} ms  last5min={last:.2f} ms  drift={drift:.2f}x")
        print(f"  rss_drift={rss_drift:+.1f} MB  total_errors={total_errs}")
        print(f"  traced_peak={peak/1024/1024:.1f} MB")
        ok = (total_errs == 0) and (drift < 2.0) and (abs(rss_drift) < 100)
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    else:
        print("SOAK: insufficient data")

if __name__ == '__main__':
    main()
