import os, sys, time, numpy as np
sys.path.insert(0, '.')
from ann_bench import brute_nn

def load_sift(path, maxr):
    d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
    raw = np.fromfile(path, dtype=np.float32, count=maxr*(d+1))
    a = raw[:(len(raw)//(d+1))*(d+1)].reshape(-1, d+1)[:, 1:]
    return a.astype(np.float32)

rng = np.random.default_rng(7)
r3 = np.random.default_rng(3)

# Stage 1: easy - 3D synthetic (baseline already; re-run with v2 for consistency not needed)
# Stage 2: medium - lowrank128 with PCA subset per level
print('=== STAGE 2: lowrank128 (PCA non-overlap subsets per level) ===')
n, nq, D = 12000, 300, 128
B = r3.normal(size=(12, D))
Z = (r3.normal(size=(60, 12))*3)[r3.integers(60, size=n+nq)] @ B / np.sqrt(12)*3 + r3.normal(size=(n+nq, D))*.7
Z = Z.astype(np.float32); X, Q = Z[:n], Z[n:]; gt = brute_nn(Q, X)
import lc_label_bucket2 as V2
CFG = dict(V2.CFG, PROJ='pca', P=16, L=4)
res = list(V2.evaluate(X, Q, gt, np.random.default_rng(5), CFG, Ks=(4,8), Ls=(1,4)))
for L in (1,4):
    for K in (4,8):
        vs = [r for r in res if r[0]==L and r[1]==K]
        if vs:
            # best scan<=2%
            cands = [r for r in vs if r[4]*100 <= 2.0]
            if cands:
                r = max(cands, key=lambda x: x[3])
                print('  L=%d K=%d  recall=%.3f  scan=%.2f%% (S=%s)' % (L, K, r[3], r[4]*100, r[2]))

# Stage 3: hard - SIFT 128
print('\n=== STAGE 3: siftsmall 128 (PCA non-overlap subsets) ===')
Xs = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_base.fvecs', 8000)
Qs = load_sift('I:/tools/ann/data/sift/siftsmall/siftsmall_query.fvecs', 300)
gt = brute_nn(Qs, Xs)
res = list(V2.evaluate(Xs, Qs, gt, np.random.default_rng(5), CFG, Ks=(4,8,15), Ls=(1,4)))
for L in (1,4):
    for K in (4,8,15):
        vs = [r for r in res if r[0]==L and r[1]==K]
        cands = [r for r in vs if r[4]*100 <= 2.0]
        if cands:
            r = max(cands, key=lambda x: x[3])
            print('  L=%d K=%d  recall=%.3f  scan=%.2f%%' % (L, K, r[3], r[4]*100))

# Stage 4: hard - BEIR 384
print('\n=== STAGE 4: beir android 384 (PCA non-overlap subsets) ===')
Xb = np.load('I:/tools/ann/data/beir_embeddings/android/corpus_embeddings.npy', mmap_mode='r')[:8000].astype(np.float32)
Qb = np.load('I:/tools/ann/data/beir_embeddings/android/query_embeddings.npy', mmap_mode='r')[:300].astype(np.float32)
gt = brute_nn(Qb, Xb)
res = list(V2.evaluate(Xb, Qb, gt, np.random.default_rng(5), CFG, Ks=(4,8,15), Ls=(1,4)))
for L in (1,4):
    for K in (4,8,15):
        vs = [r for r in res if r[0]==L and r[1]==K]
        cands = [r for r in vs if r[4]*100 <= 2.0]
        if cands:
            r = max(cands, key=lambda x: x[3])
            print('  L=%d K=%d  recall=%.3f  scan=%.2f%%' % (L, K, r[3], r[4]*100))
print('\ndone')