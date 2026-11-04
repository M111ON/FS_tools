"""Read Path search() recall on SIFT128."""
import sys, os, tempfile, shutil, time
import numpy as np
sys.path.insert(0, '.')
from ingest_fingerprint import IngestionPipeline
from read_path import ReadPath


def load_fvecs(p, maxr):
    d = int(np.fromfile(p, dtype=np.int32, count=1)[0])
    raw = np.fromfile(p, dtype=np.float32, count=maxr * (d + 1))
    a = raw[:(len(raw) // (d + 1)) * (d + 1)].reshape(-1, d + 1)[:, 1:]
    return a.astype(np.float64)


def pad384(v):
    out = np.zeros(384, dtype=np.float64)
    out[:v.shape[-1]] = v
    return out


def cos(a, b):
    na = np.linalg.norm(a); nb = np.linalg.norm(b)
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def main():
    X = load_fvecs('I:/tools/ann/data/sift/siftsmall/siftsmall_base.fvecs', 2000)[:300]
    Q = load_fvecs('I:/tools/ann/data/sift/siftsmall/siftsmall_query.fvecs', 200)[:30]
    N = X.shape[0]; nq = Q.shape[0]
    d = tempfile.mkdtemp()
    try:
        pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                                storage_dir=d)
        ids = []
        for i in range(N):
            r = pipe.ingest(pad384(X[i]).tolist())
            ids.append(r.item_id)
        rp = ReadPath(d)

        # ground truth NN in base (128D)
        gt = []
        for qi in range(nq):
            best = None; bs = -2
            for i in range(N):
                s = cos(Q[qi], X[i])
                if s > bs:
                    bs = s; best = ids[i]
            gt.append(best)

        for K in [1, 2, 3, 5]:
            hits = 0; cands = 0; lats = []
            for qi in range(nq):
                t0 = time.perf_counter()
                cs = set()
                for b in range(3):
                    for r in rp.search(b, pad384(Q[qi]).tolist(), top_k=K):
                        cs.add(r['item_id'])
                lats.append((time.perf_counter() - t0) * 1000)
                cands += len(cs)
                if gt[qi] in cs:
                    hits += 1
            print(f'SIFT K={K}: recall={hits/nq:.3f} scan%={cands/nq/N*100:.2f} '
                  f'avgcand={cands/nq:.1f} p50={np.median(lats):.2f}ms')
    finally:
        shutil.rmtree(d)
    return 0


if __name__ == '__main__':
    import sys as _s
    _s.exit(main())
