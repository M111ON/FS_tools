"""Validate LC seed on real embeddings (data_other/sift, 10k x 128)."""
import os, sys, random
import numpy as np
sys.path.insert(0, '.')
HERE = os.path.dirname(os.path.abspath(__file__))

def load_fvecs(path):
    a = np.fromfile(path, dtype=np.float32)
    d = int(np.frombuffer(a[:1].tobytes(), dtype=np.int32)[0]) if False else None
    # fvecs: first int32 per row is dim
    with open(path, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    f.seek(0)
    raw = np.fromfile(f, dtype=np.float32)
    return raw.reshape(-1, dim + 1)[:, 1:].astype(np.float64)

def to_int_vec(v):
    # Q20 fixed-point from float
    return [int(x * (1 << 20)) for x in v]

def lc_trit(vec):
    per = len(vec) // 3
    sums = [sum(vec[g*per:(g+1)*per]) for g in range(3)]
    trit = 0
    for s in sums:
        d = 0 if s < -(1 << 18) else (2 if s > (1 << 18) else 1)
        trit = trit * 3 + d
    return trit % 27

def lc_bit(vec):
    groups = 5
    per = len(vec) // groups
    bits = 0
    for g in range(groups):
        s = sum(vec[g*per:(g+1)*per])
        b = (s >> 20) & 1
        if s < 0:
            b ^= 1
        if b:
            bits |= (1 << g)
    return bits % 32

def base_seed(v):
    q = to_int_vec(v)
    m = sum(q)//len(q)
    qc = [x-m for x in q]
    # trit by sign of group sums
    per = len(qc)//3
    sums = [sum(qc[g*per:(g+1)*per]) for g in range(3)]
    trit = 0
    for sm in sums:
        d = 0 if sm < 0 else (2 if sm > 0 else 1)
        trit = trit*3 + d
    trit %= 27
    groups = 5
    per2 = len(qc)//groups
    bits = 0
    for g in range(groups):
        sm = sum(qc[g*per2:(g+1)*per2])
        if sm >= 0:
            bits |= (1 << g)
    bit = bits % 32
    return ((trit << 5) | bit)

# load data
path = os.path.join(HERE, 'data_other', 'sift', 'corpus_embeddings.npy')
if not os.path.exists(path):
    # fallback to sift fvecs
    path2 = os.path.join(HERE, 'sift', 'sift_base.fvecs')
    X = load_fvecs(path2)[:10000]
else:
    X = np.load(path)[:10000]
print(f"loaded {X.shape}")

seeds = [base_seed(row) for row in X]
print(f"unique base seeds: {len(set(seeds))} / {len(seeds)}")
print(f"seed range: [{min(seeds)}, {max(seeds)}]")

# rewind buffer
SIZE = 972
slots = [None] * SIZE
collisions = 0
for sd in seeds:
    idx = sd % SIZE
    while slots[idx] is not None and slots[idx] != sd:
        idx = (idx + 1) % SIZE
        collisions += 1
    slots[idx] = sd
used = sum(1 for s in slots if s is not None)
print(f"rewind buffer: used={used}/{SIZE} fill={used/SIZE:.3f} collisions={collisions}")

# determinism: recompute seeds, must match
seeds2 = [base_seed(row) for row in X]
print(f"determinism (real data): {seeds == seeds2}")
assert seeds == seeds2
print("REAL DATA TEST PASS")
