"""Recall boost experiments on real data: measure trade-offs."""
import sys, time, numpy as np
sys.path.insert(0, '.')
from lc_label_bucket import Label, Bucket, pack, SCALES, CFG, evaluate
from ann_bench import brute_nn, kmeans, assign
import itertools

def load_sift(path, maxr):
    d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
    raw = np.fromfile(path, dtype=np.float32, count=maxr*(d+1))
    a = raw[:(len(raw)//(d+1))*(d+1)].reshape(-1, d+1)[:, 1:]
    return a.astype(np.float32)

rng = np.random.default_rng(7)
r3 = np.random.default_rng(3)

# datasets
datasets = {}
Xs = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_base.fvecs', 5000)
Qs = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_query.fvecs', 200)
datasets['sift128'] = (Xs[:4000], Qs[:100])
Xb = np.load('I:/tools/ann/data/beir_embeddings/android/corpus_embeddings.npy', mmap_mode='r')[:4000].astype(np.float32)
Qb = np.load('I:/tools/ann/data/beir_embeddings/android/query_embeddings.npy', mmap_mode='r')[:100].astype(np.float32)
datasets['beir384'] = (Xb, Qb)

DOORS = __import__('lc_label_bucket').DOORS

def eval_method(name, X, Q, gt, method_fn):
    """method_fn(X,Q) -> (candidate_sets list of sets, scan_count)"""
    t0 = time.perf_counter()
    cands, nprobe = method_fn(X, Q)
    t_probe = (time.perf_counter() - t0) / len(Q) * 1000
    rec = float(np.mean([gt[i] in cands[i] for i in range(len(Q))]))
    scan = float(np.mean([len(c) for c in cands]) / len(X))
    return rec, scan, t_probe, nprobe

for dsname, (X, Q) in datasets.items():
    gt = brute_nn(Q, X)
    n, nq = len(X), len(Q)
    print(f"\n===== {dsname}  n={n} dim={X.shape[1]} nq={nq} =====")
    print(f"{'method':<30} {'recall':>8} {'scan%':>7} {'probe_ms/q':>11} {'lift':>6}")

    # M0: baseline PCA 3D, L=1, K=4
    def m0(X, Q):
        c = dict(CFG, PROJ='pca', S=0.3)
        lab = Label(X, np.random.default_rng(5), c)
        u, x = lab(Q)
        pts = u[:, None, :] + DOORS[None]
        dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
        rk = np.argsort(np.argsort(dist, 1), 1)
        keys = pack(pts, c)
        bk = Bucket(pack(lab(X)[0], c))
        gk = pack(lab(X)[gt])[0] if False else None
        cands = [set() for _ in range(len(Q))]
        sel = rk < 4
        cnt_keys = keys  # (nq, 15)
        for qi in range(len(Q)):
            for j in range(15):
                if sel[qi, j]:
                    # fetch ids for this key
                    ids = bk.ids(int(keys[qi, j]))
                    cands[qi].update(ids.tolist())
        return cands, 4
    rec, scan, tp, np_ = eval_method('M0 PCA3D L1 K4', X, Q, gt, m0)
    print(f"{'M0 PCA3D L1 K4':<30} {rec:>8.3f} {scan*100:>7.2f} {tp:>11.4f} {rec/scan if scan>0 else 0:>6.1f}")

    # M1: multi-proj union (rand + pca) L=1, K=4 each
    def m1(X, Q):
        cands = [set() for _ in range(len(Q))]
        for proj in ('rand', 'pca'):
            c = dict(CFG, PROJ=proj, S=0.3)
            lab = Label(X, np.random.default_rng(5), c)
            u, x = lab(Q)
            pts = u[:, None, :] + DOORS[None]
            dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
            rk = np.argsort(np.argsort(dist, 1), 1)
            keys = pack(pts, c)
            bk = Bucket(pack(lab(X)[0], c))
            sel = rk < 4
            for qi in range(len(Q)):
                for j in range(15):
                    if sel[qi, j]:
                        ids = bk.ids(int(keys[qi, j]))
                        cands[qi].update(ids.tolist())
        return cands, 8
    rec, scan, tp, np_ = eval_method('M1 multi-proj union', X, Q, gt, m1)
    print(f"{'M1 multi-proj union':<30} {rec:>8.3f} {scan*100:>7.2f} {tp:>11.4f} {rec/scan if scan>0 else 0:>6.1f}")

    # M2: IVF (k-means) nlist~sqrt(n), nprobe=4,8,16
    for nprobe in (4, 8, 16):
        def m2(X, Q, nprobe=nprobe):
            nlist = max(16, int(1.0 * np.sqrt(len(X))))
            C = kmeans(X, nlist, np.random.default_rng(5), iters=10)
            cell = assign(X, C)
            sizes = np.bincount(cell, minlength=len(C))
            cq = (C ** 2).sum(1)[None] - 2 * Q @ C.T
            order = np.argsort(cq, 1)
            # probe nprobe nearest cells per query
            cands = [set() for _ in range(len(Q))]
            for qi in range(len(Q)):
                for ci in order[qi, :nprobe]:
                    ids = np.where(cell == ci)[0]
                    cands[qi].update(ids.tolist())
            return cands, nprobe
        rec, scan, tp, np_ = eval_method(f'M2 IVF nprobe={nprobe}', X, Q, gt, m2)
        print(f"{'M2 IVF nprobe='+str(nprobe):<30} {rec:>8.3f} {scan*100:>7.2f} {tp:>11.4f} {rec/scan if scan>0 else 0:>6.1f}")

    # M3: PCA 3D, L=4, K=4 (multi-level union)
    def m3(X, Q):
        cands = [set() for _ in range(len(Q))]
        rng_l = np.random.default_rng(5)
        for lvl in range(4):
            c = dict(CFG, PROJ='pca', S=0.3)
            lab = Label(X, rng_l, c)
            u, x = lab(Q)
            pts = u[:, None, :] + DOORS[None]
            dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
            rk = np.argsort(np.argsort(dist, 1), 1)
            keys = pack(pts, c)
            bk = Bucket(pack(lab(X)[0], c))
            sel = rk < 4
            for qi in range(len(Q)):
                for j in range(15):
                    if sel[qi, j]:
                        ids = bk.ids(int(keys[qi, j]))
                        cands[qi].update(ids.tolist())
        return cands, 16
    rec, scan, tp, np_ = eval_method('M3 PCA3D L4 K4', X, Q, gt, m3)
    print(f"{'M3 PCA3D L4 K4':<30} {rec:>8.3f} {scan*100:>7.2f} {tp:>11.4f} {rec/scan if scan>0 else 0:>6.1f}")

    # M4: Adaptive K (distance threshold 1.5x nearest door)
    def m4(X, Q):
        c = dict(CFG, PROJ='pca', S=0.3)
        lab = Label(X, np.random.default_rng(5), c)
        u, x = lab(Q)
        pts = u[:, None, :] + DOORS[None]
        dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
        dmin = dist.min(1, keepdims=True)
        sel = dist <= (dmin * 1.5 + 1e-9)
        keys = pack(pts, c)
        bk = Bucket(pack(lab(X)[0], c))
        cands = [set() for _ in range(len(Q))]
        for qi in range(len(Q)):
            for j in range(15):
                if sel[qi, j]:
                    ids = bk.ids(int(keys[qi, j]))
                    cands[qi].update(ids.tolist())
        return cands, int(sel.sum(1).mean())
    rec, scan, tp, np_ = eval_method('M4 adaptive K', X, Q, gt, m4)
    print(f"{'M4 adaptive K':<30} {rec:>8.3f} {scan*100:>7.2f} {tp:>11.4f} {rec/scan if scan>0 else 0:>6.1f}  (avg K={np_})")

print("\ndone")