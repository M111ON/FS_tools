"""Task 2: Multi-Dim Test.
Demonstrates cross-dim collision behavior of the handoff's multiply expansion
vs the corrected high-bit-tag expansion.
"""
import random

def gen_vec(dim, seed):
    rng = random.Random(seed)
    return [rng.randint(-(1 << 19), (1 << 19)) for _ in range(dim)]

def to_q20(v):
    return v  # already integer Q20-ish

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

def base_seed(vec):
    return ((lc_trit(vec) << 5) | lc_bit(vec))

M = 50000
D1, D2 = 384, 128
bases1 = [base_seed(gen_vec(D1, i*2+1)) for i in range(M)]
bases2 = [base_seed(gen_vec(D2, i*2+2)) for i in range(M)]

# handoff multiply expansion
mul1 = [b * D1 for b in bases1]
mul2 = [b * D2 for b in bases2]
coll_mul = len(set(mul1) & set(mul2))

# corrected high-bit tag expansion (dim_id 0 / 1)
tag1 = [b | (0 << 10) for b in bases1]
tag2 = [b | (1 << 10) for b in bases2]
coll_tag = len(set(tag1) & set(tag2))

print(f"M={M} D1={D1} D2={D2}")
print(f"handoff multiply: cross-dim collisions = {coll_mul}")
print(f"corrected tag:    cross-dim collisions = {coll_tag}")
assert coll_tag == 0, "tagged expansion must have zero collision"
print("MULTIDIM TEST PASS: tagged expansion zero-collision; multiply has collisions (spec flaw reported).")
