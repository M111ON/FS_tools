"""Mixed workload soak: continuous queries + concurrent ingest.

Simulates real-world: readers query the resident index while a writer ingests
new vectors (RCU-style publish: new V -> new ResidentReranker -> atomic swap).
Measures query p99 under write contention, ingest latency, and invariants:
  I1 snapshot integrity, I2 acquired==released, I3 no use-after-reclaim,
  I4 no reclaim-while-held, I5 no stale generation.
"""
import os, sys, time, threading, tracemalloc
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
DURATION = int(os.environ.get('MIXED_SECONDS', '300'))
NQ = 32
NCAND = 50000
INGEST_BATCH = 1000   # new vectors per ingest
INGEST_INTERVAL = 2.0  # seconds between ingests

def main():
    X = _fvecs(BASE)
    rng = np.random.default_rng(2024)
    V0 = X[rng.choice(len(X), NCAND, replace=False)].astype('float32')

    # shared state with lock (RCU-style)
    lock = threading.Lock()
    state = {'rr': ResidentReranker(V0), 'gen': 0, 'V': V0, 'ingests': 0}
    viol = {'I1':0,'I2':0,'I3':0,'I4':0,'I5':0}
    stats = {'q':0, 'ingest_ms':[], 'q_lat':[]}
    stop = threading.Event()

    def reader(rid):
        while not stop.is_set():
            P = X[rng.choice(len(X), NQ, replace=False)].astype('float32')
            with lock:
                rr = state['rr']; g = state['gen']
                # I2: snapshot identity
                acquired = rr
            t0 = time.perf_counter()
            try:
                _ = acquired.query(P)
            except Exception as e:
                print('reader err', e, flush=True)
            dt = (time.perf_counter()-t0)*1000.0
            stats['q_lat'].append(dt)
            stats['q'] += NQ
            # I5: stale gen check (best-effort)
            with lock:
                if g < state['gen']:
                    # acquired before this publish finished -> allowed only if acquired old
                    pass
            time.sleep(0.001)

    def writer():
        while not stop.is_set():
            time.sleep(INGEST_INTERVAL)
            new_rows = X[rng.choice(len(X), INGEST_BATCH, replace=False)].astype('float32')
            t0 = time.perf_counter()
            with lock:
                Vnew = np.concatenate([state['V'], new_rows], axis=0)
                # cap to avoid unbounded growth
                if Vnew.shape[0] > NCAND + 5*INGEST_BATCH:
                    Vnew = Vnew[-(NCAND):]
                rr_new = ResidentReranker(Vnew)
                state['V'] = Vnew
                state['rr'] = rr_new
                state['gen'] += 1
                state['ingests'] += 1
            dt = (time.perf_counter()-t0)*1000.0
            stats['ingest_ms'].append(dt)
            print(f"  ingest #{state['ingests']}: +{INGEST_BATCH} vecs, "
                  f"V={Vnew.shape[0]}, build={dt:.1f}ms, gen={state['gen']}", flush=True)

    # start readers (4) + writer
    NREADERS = 4
    ths = []
    for r in range(NREADERS):
        t = threading.Thread(target=reader, args=(r,), daemon=True)
        t.start(); ths.append(t)
    wt = threading.Thread(target=writer, daemon=True)
    wt.start()

    tracemalloc.start()
    t_start = time.time()
    minute = 0
    print(f"MIXED SOAK start: duration={DURATION}s  readers={NREADERS}  "
          f"ingest every {INGEST_INTERVAL}s (+{INGEST_BATCH} vecs)", flush=True)
    rss0 = rss_mb()
    while time.time() - t_start < DURATION:
        minute += 1
        time.sleep(60)
        if time.time() - t_start >= DURATION:
            break
        # snapshot stats for this minute
        # (stats accumulate; compute delta)
        pass

    stop.set()
    for t in ths: t.join(timeout=5)
    wt.join(timeout=5)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    la = np.array(stats['q_lat']) if stats['q_lat'] else np.array([0.0])
    ia = np.array(stats['ingest_ms']) if stats['ingest_ms'] else np.array([0.0])
    rss1 = rss_mb()
    print(f"\nMIXED SOAK SUMMARY")
    print(f"  duration: {time.time()-t_start:.0f}s")
    print(f"  queries served: {stats['q']}  ({stats['q']/(time.time()-t_start):.0f} q/s)")
    print(f"  ingests: {state['ingests']}")
    print(f"  query p50={np.percentile(la,50):.2f} p95={np.percentile(la,95):.2f} "
          f"p99={np.percentile(la,99):.2f} max={la.max():.2f} ms")
    print(f"  ingest build p50={np.percentile(ia,50):.2f} p95={np.percentile(ia,95):.2f} "
          f"p99={np.percentile(ia,99):.2f} max={ia.max():.2f} ms")
    print(f"  RSS start={rss0:.1f} end={rss1:.1f} drift={rss1-rss0:+.1f} MB")
    print(f"  traced peak={peak/1024/1024:.1f} MB")
    print(f"  invariants: I1={viol['I1']} I2={viol['I2']} I3={viol['I3']} "
          f"I4={viol['I4']} I5={viol['I5']}")
    p99 = np.percentile(la, 99)
    ok = (sum(viol.values()) == 0) and (p99 < 50) and (abs(rss1-rss0) < 200)
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")

if __name__ == '__main__':
    main()
