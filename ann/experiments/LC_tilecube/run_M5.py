"""M5: IVF + LC hybrid — dedup union + exact rerank. Measure LC's unique contribution."""
import sys, time, numpy as np
sys.path.insert(0, '.')
from lc_label_bucket import Label, Bucket, pack, SCALES, CFG, DOORS
from ann_bench import brute_nn, kmeans, assign

def load_sift(path, maxr):
    d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
    raw = np.fromfile(path, dtype=np.float32, count=maxr*(d+1))
    a = raw[:(len(raw)//(d+1))*(d+1)].reshape(-1, d+1)[:, 1:]
    return a.astype(np.float32)

rng = np.random.default_rng(7)
X = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_base.fvecs', 4000)
Q = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_query.fvecs', 100)
X = X[:4000]; Q = Q[:100]
gt = brute_nn(Q, X)
n, nq = len(X), len(Q)

# precompute IVF cells
nlist = max(16, int(1.0 * np.sqrt(n)))
C = kmeans(X, nlist, np.random.default_rng(5), iters=10)
cell = assign(X, C)
cq = (C ** 2).sum(1)[None] - 2 * Q @ C.T
ivf_order = np.argsort(cq, 1)  # (nq, nlist)

def ivf_candidates(nprobe):
    cands = [set() for _ in range(nq)]
    for qi in range(nq):
        for ci in ivf_order[qi, :nprobe]:
            ids = np.where(cell == ci)[0]
            cands[qi].update(ids.tolist())
    return cands

def lc_candidates(L):
    cands = [set() for _ in range(nq)]
    rng_l = np.random.default_rng(5)
    for lvl in range(L):
        c = dict(CFG, PROJ='pca', S=0.3)
        lab = Label(X, rng_l, c)
        u, x = lab(Q)
        pts = u[:, None, :] + DOORS[None]
        dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
        rk = np.argsort(np.argsort(dist, 1), 1)
        keys = pack(pts, c)
        bk = Bucket(pack(lab(X)[0], c))
        sel = rk < 4
        for qi in range(nq):
            for j in range(15):
                if sel[qi, j]:
                    ids = bk.ids(int(keys[qi, j]))
                    cands[qi].update(ids.tolist())
    return cands

def rerank_recall(union_cands):
    """exact rerank: for each query sort union by L2 distance, check top1 == gt"""
    hits = 0
    for qi in range(nq):
        ids = list(union_cands[qi])
        if not ids:
            continue
        dists = np.linalg.norm(X[ids] - Q[qi], axis=1)
        top = ids[int(np.argmin(dists))]
        if top == gt[qi]:
            hits += 1
    return hits / nq

combos = [
    ('M5-A', 1, 1),
    ('M5-B', 2, 1),
    ('M5-C', 2, 4),
    ('M5-D', 4, 1),
]

print(f"{'combo':<8} {'ivf_np':>7} {'LC_L':>5} {'ivf_rec':>9} {'lc_rec':>8} {'union_rec':>10} {'rerank_rec':>11} {'scan%':>7} {'LC_unique':>10} {'ivf_size':>9} {'lc_size':>8} {'union_size':>10}")
for name, np_, L in combos:
    ivf_c = ivf_candidates(np_)
    lc_c = lc_candidates(L)
    union_c = [ivf_c[qi] | lc_c[qi] for qi in range(nq)]
    ivf_rec = float(np.mean([gt[i] in ivf_c[i] for i in range(nq)]))
    lc_rec = float(np.mean([gt[i] in lc_c[i] for i in range(nq)]))
    union_rec = float(np.mean([gt[i] in union_c[i] for i in range(nq)]))
    rerank_rec = rerank_recall(union_c)
    scan = float(np.mean([len(u) for u in union_c]) / n)
    # LC unique contribution: LC found gt but IVF didn't
    lc_unique_hits = sum(1 for i in range(nq) if (gt[i] in lc_c[i]) and (gt[i] not in ivf_c[i]))
    ivf_sz = float(np.mean([len(c) for c in ivf_c]))
    lc_sz = float(np.mean([len(c) for c in lc_c]))
    union_sz = float(np.mean([len(c) for c in union_c]))
    print(f"{name:<8} {np_:>7} {L:>5} {ivf_rec:>9.3f} {lc_rec:>8.3f} {union_rec:>10.3f} {rerank_rec:>11.3f} {scan*100:>7.2f} {lc_unique_hits:>10} {ivf_sz:>9.1f} {lc_sz:>8.1f} {union_sz:>10.1f}")

# Baseline comparison
print("\n--- Baseline ---")
ivf4 = ivf_candidates(4)
ivf4_rec = float(np.mean([gt[i] in ivf4[i] for i in range(nq)]))
ivf4_scan = float(np.mean([len(c) for c in ivf4]) / n)
print(f"IVF nprobe=4 alone: recall={ivf4_rec:.3f} scan={ivf4_scan*100:.2f}%  (no LC)")
lc4 = lc_candidates(4)
lc4_rec = float(np.mean([gt[i] in lc4[i] for i in range(nq)]))
lc4_scan = float(np.mean([len(c) for c in lc4]) / n)
print(f"LC L4 alone:        recall={lc4_rec:.3f} scan={lc4_scan*100:.2f}%  (no IVF)")

print("\ndone")