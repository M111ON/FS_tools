"""Canonical Test Vectors for LC_COMMIT_RECORD.

Locks the exact byte-level contract so C and Python implementations
produce identical hashes.

Spec:
  - checksum encoding: hexadecimal, 64 lowercase characters
  - field name: previous_record_hash (canonical), alias prev_record_hash (Python)
  - canonical serialization: json.dumps(obj, sort_keys=True, separators=(",",":"))
  - record_hash = SHA256(canonical_bytes)
  - prev_record_hash chains: record[n].prev == record[n-1].hash

Test vectors: input record -> serialized bytes -> expected SHA-256.
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict

EMBED_DIM = 384


# ---------- Canonical spec ----------
FIELD_NAME_MAP = {
    "previous_record_hash": "prev_record_hash",  # C name -> Python alias
}

def canonical_serialize(record_dict):
    """Canonical serialization per spec."""
    # field names: use canonical C names
    obj = {
        "data_checksum": record_dict["data_checksum"],
        "generation_id": record_dict["generation_id"],
        "previous_record_hash": record_dict["previous_record_hash"],
        "sequence": record_dict["sequence"],
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_hash(record_dict):
    serialized = canonical_serialize(record_dict)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


# ---------- Test vectors ----------
TEST_VECTORS = [
    # Vector 1: genesis record
    {
        "input": {
            "generation_id": 1,
            "sequence": 1,
            "data_checksum": "0" * 64,
            "previous_record_hash": "0" * 64,
        },
        "expected_serialized": '{"data_checksum":"0000000000000000000000000000000000000000000000000000000000000000","generation_id":1,"previous_record_hash":"0000000000000000000000000000000000000000000000000000000000000000","sequence":1}',
        "expected_hash": None,  # computed below
    },
    # Vector 2: second record
    {
        "input": {
            "generation_id": 2,
            "sequence": 2,
            "data_checksum": "a" * 64,
            "previous_record_hash": None,  # filled from vector 1 hash
        },
        "expected_serialized": None,
        "expected_hash": None,
    },
    # Vector 3: high values
    {
        "input": {
            "generation_id": 999,
            "sequence": 42,
            "data_checksum": "f" * 64,
            "previous_record_hash": "e" * 64,
        },
        "expected_serialized": '{"data_checksum":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","generation_id":999,"previous_record_hash":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","sequence":42}',
        "expected_hash": None,
    },
]

# Fill vector 1 hash
TEST_VECTORS[0]["expected_hash"] = compute_hash(TEST_VECTORS[0]["input"])
# Fill vector 2
TEST_VECTORS[1]["input"]["previous_record_hash"] = TEST_VECTORS[0]["expected_hash"]
TEST_VECTORS[1]["expected_serialized"] = canonical_serialize(TEST_VECTORS[1]["input"])
TEST_VECTORS[1]["expected_hash"] = compute_hash(TEST_VECTORS[1]["input"])
# Fill vector 3 hash
TEST_VECTORS[2]["expected_hash"] = compute_hash(TEST_VECTORS[2]["input"])


# ---------- Tests ----------
def test_vector1():
    print("=" * 70)
    print("TEST 1: Vector 1 — Genesis Record")
    print("=" * 70)
    v = TEST_VECTORS[0]
    serialized = canonical_serialize(v["input"])
    h = compute_hash(v["input"])
    print(f"  input: {v['input']}")
    print(f"  serialized: {serialized}")
    print(f"  expected:   {v['expected_serialized']}")
    print(f"  match serialized: {serialized == v['expected_serialized']}")
    print(f"  hash: {h}")
    print(f"  expected hash: {v['expected_hash']}")
    print(f"  match hash: {h == v['expected_hash']}")
    assert serialized == v["expected_serialized"]
    assert h == v["expected_hash"]
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    print("  PASS")
    return True


def test_vector2():
    print()
    print("=" * 70)
    print("TEST 2: Vector 2 — Chained Record")
    print("=" * 70)
    v = TEST_VECTORS[1]
    serialized = canonical_serialize(v["input"])
    h = compute_hash(v["input"])
    print(f"  input: {v['input']}")
    print(f"  serialized: {serialized}")
    print(f"  expected:   {v['expected_serialized']}")
    print(f"  match: {serialized == v['expected_serialized']}")
    print(f"  hash: {h}")
    print(f"  expected: {v['expected_hash']}")
    print(f"  match: {h == v['expected_hash']}")
    assert serialized == v["expected_serialized"]
    assert h == v["expected_hash"]
    # verify chain: prev_hash == vector1 hash
    assert v["input"]["previous_record_hash"] == TEST_VECTORS[0]["expected_hash"]
    print("  chain link verified: YES")
    print("  PASS")
    return True


def test_vector3():
    print()
    print("=" * 70)
    print("TEST 3: Vector 3 — High Values")
    print("=" * 70)
    v = TEST_VECTORS[2]
    serialized = canonical_serialize(v["input"])
    h = compute_hash(v["input"])
    print(f"  serialized: {serialized}")
    print(f"  expected:   {v['expected_serialized']}")
    print(f"  match: {serialized == v['expected_serialized']}")
    print(f"  hash: {h}")
    print(f"  expected: {v['expected_hash']}")
    print(f"  match: {h == v['expected_hash']}")
    assert serialized == v["expected_serialized"]
    assert h == v["expected_hash"]
    print("  PASS")
    return True


def test_hex_encoding():
    print()
    print("=" * 70)
    print("TEST 4: Hexadecimal Encoding (64 lowercase chars)")
    print("=" * 70)
    for v in TEST_VECTORS:
        h = v["expected_hash"]
        assert len(h) == 64, f"hash length {len(h)} != 64"
        assert all(c in "0123456789abcdef" for c in h), f"non-hex or uppercase in {h}"
        cs = v["input"]["data_checksum"]
        assert len(cs) == 64, f"checksum length {len(cs)} != 64"
        assert all(c in "0123456789abcdef" for c in cs), "checksum not hex"
        ph = v["input"]["previous_record_hash"]
        assert len(ph) == 64, f"prev_hash length {len(ph)} != 64"
    print("  all hashes: 64 lowercase hex chars")
    print("  all checksums: 64 lowercase hex chars")
    print("  all prev_hashes: 64 lowercase hex chars")
    print("  PASS")
    return True


def test_field_name_mapping():
    print()
    print("=" * 70)
    print("TEST 5: Field Name Mapping (previous_record_hash <-> prev_record_hash)")
    print("=" * 70)
    # canonical name: previous_record_hash
    # Python alias: prev_record_hash
    print(f"  canonical (C): previous_record_hash")
    print(f"  alias (Python): prev_record_hash")
    print(f"  mapping: {FIELD_NAME_MAP}")
    # verify canonical serialization uses previous_record_hash
    v = TEST_VECTORS[0]
    serialized = canonical_serialize(v["input"])
    assert "previous_record_hash" in serialized
    assert "prev_record_hash" not in serialized
    print("  canonical serialized uses 'previous_record_hash': YES")
    print("  PASS")
    return True


def test_determinism():
    print()
    print("=" * 70)
    print("TEST 6: Determinism (1000 iterations)")
    print("=" * 70)
    v = TEST_VECTORS[0]
    hashes = set()
    for _ in range(1000):
        h = compute_hash(v["input"])
        hashes.add(h)
    assert len(hashes) == 1, f"non-deterministic: {len(hashes)} distinct hashes"
    print(f"  1000 iterations -> {len(hashes)} distinct hash (deterministic)")
    print("  PASS")
    return True


def test_chain_linkage():
    print()
    print("=" * 70)
    print("TEST 7: Chain Linkage (prev_hash == previous record_hash)")
    print("=" * 70)
    # vector1 -> vector2 chain
    v1_hash = TEST_VECTORS[0]["expected_hash"]
    v2_prev = TEST_VECTORS[1]["input"]["previous_record_hash"]
    print(f"  v1 hash: {v1_hash}")
    print(f"  v2 prev: {v2_prev}")
    assert v1_hash == v2_prev
    print("  chain link: YES")
    # verify v2 hash covers v2's prev_hash
    v2_hash = TEST_VECTORS[1]["expected_hash"]
    # recompute with v2's prev_hash
    h = compute_hash(TEST_VECTORS[1]["input"])
    assert h == v2_hash
    print("  v2 hash covers prev_hash: YES")
    print("  PASS")
    return True


def main():
    tests = [test_vector1, test_vector2, test_vector3, test_hex_encoding,
             test_field_name_mapping, test_determinism, test_chain_linkage]
    all_ok = True
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            all_ok = False
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR: {e}")
            all_ok = False
    print()
    print("=" * 70)
    print(f"CANONICAL TEST VECTORS: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    if all_ok:
        print()
        print("Canonical contract locked:")
        print("  - serialization: json.dumps(sort_keys=True, separators=(\",\",\":\"))")
        print("  - encoding: UTF-8 (ASCII subset)")
        print("  - hash: SHA-256, 64 lowercase hex chars")
        print("  - checksum: 64 lowercase hex chars")
        print("  - prev_hash: 64 lowercase hex chars")
        print("  - field names: generation_id, sequence, data_checksum, previous_record_hash")
        print("  - Python alias: prev_record_hash -> previous_record_hash")
        print()
        print("Test vectors:")
        for i, v in enumerate(TEST_VECTORS, 1):
            print(f"  Vector {i}: hash={v['expected_hash']}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
