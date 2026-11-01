"""Golden vector cross-language test for chain seed generation.
Verifies that Python produces the same seed as C for a known vector.
"""
import struct
import numpy as np

FNV_OFF = 14695981039346656037
FNV_PRIME = 1099511628211
M64 = 0xFFFFFFFFFFFFFFFF


def fnv1a(data_bytes):
    h = FNV_OFF
    for b in data_bytes:
        h ^= b
        h = (h * FNV_PRIME) & M64
    return h


def mix64(x):
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111B9) & M64
    return (x ^ (x >> 31)) & M64


def seed_from_vector(vec, dim, level, space_hash):
    vec_bytes = np.array(vec, dtype=np.float32).tobytes()
    h = fnv1a(vec_bytes)
    h ^= (space_hash * 0x9E3779B97F4A7C15) & M64
    h ^= (level * 0x517CC1B727220A95) & M64
    h = mix64(h)
    major_a = h % 24
    cube_route = (h >> 8) % 48
    inner_ori = (h >> 11) & 0xFFF
    outer_ori = (h >> 23) & 0xFFF
    checksum = (h >> 35) & 0x1FFF
    seed = 0
    seed |= (major_a & 0x1F) << 59
    seed |= (level & 0xFFFF) << 43
    seed |= (cube_route & 0x3F) << 37
    seed |= (inner_ori & 0xFFF) << 25
    seed |= (outer_ori & 0xFFF) << 13
    seed |= (checksum & 0x1FFF)
    return seed


# Golden vector (same as C test)
GOLDEN_VEC = [1.0, 0.5, -0.3, 0.8]
GOLDEN_SPACE_HASH = 0xdbec345f
GOLDEN_SEED = 0x980008911527afe0

def test_golden_vector():
    seed = seed_from_vector(GOLDEN_VEC, 4, 1, GOLDEN_SPACE_HASH)
    assert seed == GOLDEN_SEED, f"Expected 0x{GOLDEN_SEED:016x}, got 0x{seed:016x}"
    print(f"GOLDEN PASS: 0x{seed:016x} (Python == C)")


if __name__ == "__main__":
    test_golden_vector()
