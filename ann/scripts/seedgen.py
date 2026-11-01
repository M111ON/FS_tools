"""seedgen.py - Python twin of include/chain_seedgen.h (seed_from_vector_ex).

Single source of truth for Python-side seed generation. Must stay bit-identical
to the C header; tests/test_seed_golden.{c,py} pin both to the same constant.
"""
import struct

FNV_OFF = 14695981039346656037   # true FNV-1a 64 offset basis (0xcbf29ce484222325)
FNV_PRIME = 1099511628211
M64 = 0xFFFFFFFFFFFFFFFF
SEED_LEVEL = 1


def fnv1a_64(data):
    h = FNV_OFF
    for b in data:
        h ^= b
        h = (h * FNV_PRIME) & M64
    return h


def mix64(x):
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111B9) & M64
    return (x ^ (x >> 31)) & M64


def space_hash32(model, version, dim):
    h = 0
    for c in model:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    h = (h ^ (version * 0x9E3779B9)) & 0xFFFFFFFF
    return (h ^ (dim * 0x517CC1B7)) & 0xFFFFFFFF


def seed_from_vec_bytes(vec_bytes, space_hash, level=SEED_LEVEL):
    h = fnv1a_64(vec_bytes)
    h ^= (space_hash * 0x9E3779B97F4A7C15) & M64
    h ^= (level * 0x517CC1B727220A95) & M64
    h = mix64(h)
    return (((h % 24) << 59) | ((level & 0xFFFF) << 43) |
            (((h >> 8) % 48) << 37) | (((h >> 11) & 0xFFF) << 25) |
            (((h >> 23) & 0xFFF) << 13) | ((h >> 35) & 0x1FFF))


def seed_from_floats(vec, space_hash, level=SEED_LEVEL):
    return seed_from_vec_bytes(struct.pack("<%df" % len(vec), *vec), space_hash, level)
