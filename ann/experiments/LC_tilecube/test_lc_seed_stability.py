"""Task 3: Stability (Determinism) test.
LC seed address (integer-only, deterministic) vs pca24-style k-means address.
Run 100 rounds; LC must be 100% identical, k-means must differ.
"""
import numpy as np
from sklearn.cluster import KMeans

RNG = np.random.default_rng(42)
N = 300
D = 32
X = RNG.standard_normal((N, D)).astype(np.float64)

def to_q20(v):
    return (v * (1 << 20)).astype(np.int64)

def lc_trit(vec_q20):
    per = len(vec_q20) // 3
    sums = []
    for g in range(3):
        s = int(vec_q20[g*per:(g+1)*per].sum())
        sums.append(s)
    trit = 0
    for s in sums:
        if s < -(1 << 18):
            d = 0
        elif s > (1 << 18):
            d = 2
        else:
            d = 1
        trit = trit * 3 + d
    return trit % 27

def lc_bit(vec_q20):
    groups = 5
    per = len(vec_q20) // groups
    bits = 0
    for g in range(groups):
        s = int(vec_q20[g*per:(g+1)*per].sum())
        b = (s >> 20) & 1
        if s < 0:
            b ^= 1
        if b:
            bits |= (1 << g)
    return bits % 32

def lc_seed(vec):
    vq = to_q20(vec)
    trit = lc_trit(vq)
    bit = lc_bit(vq)
    return ((trit << 5) | bit)

def lc_address(vecs):
    # address = seed per vector (deterministic)
    return np.array([lc_seed(v) for v in vecs], dtype=np.int64)

# k-means pca24-style address: cluster id per vector
def km_address(vecs, seed):
    km = KMeans(n_clusters=8, random_state=seed, n_init=1)
    return km.fit_predict(vecs)

# Baseline addresses
base_lc = lc_address(X)
# 100 rounds LC
lc_identical = 0
for r in range(100):
    a = lc_address(X)
    if np.array_equal(a, base_lc):
        lc_identical += 1

# 100 rounds k-means with different seeds
km_base = km_address(X, 0)
km_identical = 0
km_diffs = []
for r in range(1, 101):
    a = km_address(X, r)
    if np.array_equal(a, km_base):
        km_identical += 1
    km_diffs.append(int((a != km_base).sum()))

print(f"LC seed address: {lc_identical}/100 rounds identical (expect 100)")
print(f"k-means address: {km_identical}/100 rounds identical (expect <100)")
print(f"k-means avg changed labels per round: {np.mean(km_diffs):.1f} / {N}")
print(f"k-means min/max changed: {min(km_diffs)}/{max(km_diffs)}")
assert lc_identical == 100, "LC not deterministic!"
print("STABILITY TEST PASS: LC deterministic, k-means not.")
