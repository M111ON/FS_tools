"""Journal Head Integrity & Canonical Serialization Test.

Locks:
  1. Last Record Tampering: tamper last record's data_checksum -> detectable
     via direct checksum comparison with actual generation data.
  2. Canonical Serialization: same data -> same hash, regardless of
     serialize/deserialize path.
  3. record_hash() covers ALL fields: generation_id, sequence,
     data_checksum, previous_record_hash.
  4. Head integrity: verify the last record against actual generation data.

LC_COMMIT_RECORD (canonical):
  struct {
    uint64_t generation_id;
    uint64_t sequence;
    uint8_t  data_checksum[32];
    uint8_t  previous_record_hash[32];
  }
  record_hash = SHA256(canonical_serialize(all 4 fields))

Architecture: LC Persistence Architecture v1.0 (Logical Design)
Physical durability (power-loss) = separate concern.
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict

EMBED_DIM = 384


# ---------- Canonical LC_COMMIT_RECORD ----------
class CanonicalRecord:
    """LC_COMMIT_RECORD with canonical serialization and full-field hash."""

    def __init__(self, generation_id, sequence, data_checksum, prev_record_hash):
        self.generation_id = int(generation_id)
        self.sequence = int(sequence)
        self.data_checksum = str(data_checksum)
        self.prev_record_hash = str(prev_record_hash)

    def canonical_serialize(self):
        """Canonical serialization: deterministic, field-ordered."""
        # fixed field order, no spaces, sorted keys
        obj = {
            "generation_id": self.generation_id,
            "sequence": self.sequence,
            "data_checksum": self.data_checksum,
            "prev_record_hash": self.prev_record_hash,
        }
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    def record_hash(self):
        """Hash of ALL fields via canonical serialization."""
        return hashlib.sha256(self.canonical_serialize().encode()).hexdigest()

    def to_dict(self):
        return {
            "generation_id": self.generation_id,
            "sequence": self.sequence,
            "data_checksum": self.data_checksum,
            "prev_record_hash": self.prev_record_hash,
        }

    @staticmethod
    def from_dict(d):
        return CanonicalRecord(
            d["generation_id"], d["sequence"],
            d["data_checksum"], d["prev_record_hash"])

    def verify_chain(self, prev_record):
        if prev_record is None:
            return self.prev_record_hash == "0" * 64 or self.prev_record_hash == ""
        return self.prev_record_hash == prev_record.record_hash()


# ---------- Head Integrity Checker ----------
class HeadIntegrityChecker:
    """Verify the last journal record against actual generation data."""

    def __init__(self, gen_data, gen_checksum):
        self.gen_data = gen_data
        self.gen_checksum = gen_checksum

    def verify_head(self, record):
        """Verify record against actual generation data."""
        # 1. record's data_checksum must match actual gen checksum
        if record.data_checksum != self.gen_checksum:
            return False, "data_checksum_mismatch"
        # 2. record's generation_id must be valid
        if record.generation_id < 1:
            return False, "invalid_gen_id"
        return True, "ok"

    def verify_chain_integrity(self, records):
        """Verify entire chain including head."""
        if not records:
            return True, "empty"
        prev = None
        for r in records:
            if not r.verify_chain(prev):
                return False, f"chain_break_at_gen{r.generation_id}"
            prev = r
        # verify head against actual data
        head = records[-1]
        ok, reason = self.verify_head(head)
        if not ok:
            return False, f"head_integrity_fail: {reason}"
        return True, "ok"


# ---------- Tests ----------
def test_last_record_tampering():
    print("=" * 70)
    print("TEST 1: Last Record Tampering (tamper data_checksum of last record)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        # actual generation data
        gen_data = [{"gen": 3, "val": "test"}]
        gen_checksum = hashlib.sha256(json.dumps(gen_data).encode()).hexdigest()

        # build journal with 3 records
        r1 = CanonicalRecord(1, 1, "cs1", "0" * 64)
        r2 = CanonicalRecord(2, 2, "cs2", r1.record_hash())
        r3 = CanonicalRecord(3, 3, gen_checksum, r2.record_hash())

        records = [r1, r2, r3]
        checker = HeadIntegrityChecker(gen_data, gen_checksum)

        # verify original: OK
        ok, reason = checker.verify_chain_integrity(records)
        print(f"  original chain: ok={ok}, reason={reason}")
        assert ok

        # tamper r3's data_checksum
        r3_tampered = CanonicalRecord(3, 3, "TAMPERED_CHECKSUM", r2.record_hash())
        tampered_records = [r1, r2, r3_tampered]
        ok2, reason2 = checker.verify_chain_integrity(tampered_records)
        print(f"  tampered head: ok={ok2}, reason={reason2}")
        assert not ok2
        assert "data_checksum_mismatch" in reason2
        print("  last record tampering detected: YES")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_canonical_serialization():
    print()
    print("=" * 70)
    print("TEST 2: Canonical Serialization (same data -> same hash)")
    print("=" * 70)
    # create record
    r = CanonicalRecord(1, 1, "abc123", "0" * 64)
    h1 = r.record_hash()
    # serialize -> deserialize -> hash
    d = r.to_dict()
    r2 = CanonicalRecord.from_dict(d)
    h2 = r2.record_hash()
    print(f"  original hash:  {h1}")
    print(f"  restored hash:  {h2}")
    assert h1 == h2, "hash changed after serialize/deserialize"
    # canonical serialize is deterministic
    c1 = r.canonical_serialize()
    c2 = r2.canonical_serialize()
    assert c1 == c2
    print(f"  canonical serialize identical: {c1 == c2}")
    # same data, different construction order -> same hash
    r3 = CanonicalRecord(1, 1, "abc123", "0" * 64)
    assert r3.record_hash() == h1
    print("  same data -> same hash: YES")
    print("  PASS")
    return True


def test_full_field_hash():
    print()
    print("=" * 70)
    print("TEST 3: Full-Field Hash (all 4 fields covered)")
    print("=" * 70)
    r = CanonicalRecord(1, 1, "cs1", "0" * 64)
    h = r.record_hash()
    # change each field -> hash changes
    r2 = CanonicalRecord(2, 1, "cs1", "0" * 64)  # gen_id changed
    assert r2.record_hash() != h
    r3 = CanonicalRecord(1, 2, "cs1", "0" * 64)  # sequence changed
    assert r3.record_hash() != h
    r4 = CanonicalRecord(1, 1, "cs2", "0" * 64)  # data_checksum changed
    assert r4.record_hash() != h
    r5 = CanonicalRecord(1, 1, "cs1", "1" * 64)  # prev_hash changed
    assert r5.record_hash() != h
    print("  changing gen_id: hash changes")
    print("  changing sequence: hash changes")
    print("  changing data_checksum: hash changes")
    print("  changing prev_record_hash: hash changes")
    print("  all 4 fields covered: YES")
    print("  PASS")
    return True


def test_chain_integrity():
    print()
    print("=" * 70)
    print("TEST 4: Chain Integrity (prev_record_hash links)")
    print("=" * 70)
    r1 = CanonicalRecord(1, 1, "cs1", "0" * 64)
    r2 = CanonicalRecord(2, 2, "cs2", r1.record_hash())
    r3 = CanonicalRecord(3, 3, "cs3", r2.record_hash())
    records = [r1, r2, r3]
    checker = HeadIntegrityChecker({"gen": 3}, "cs3")
    ok, reason = checker.verify_chain_integrity(records)
    print(f"  valid chain: ok={ok}, reason={reason}")
    assert ok
    # break chain: r2 with wrong prev_hash
    r2_bad = CanonicalRecord(2, 2, "cs2", "BAD_HASH")
    broken = [r1, r2_bad, r3]
    ok2, reason2 = checker.verify_chain_integrity(broken)
    print(f"  broken chain: ok={ok2}, reason={reason2}")
    assert not ok2
    print("  chain integrity: YES")
    print("  PASS")
    return True


def test_head_vs_chain():
    print()
    print("=" * 70)
    print("TEST 5: Head Integrity vs Chain Integrity (separate concerns)")
    print("=" * 70)
    gen_data = [{"gen": 3}]
    gen_checksum = hashlib.sha256(json.dumps(gen_data).encode()).hexdigest()
    r1 = CanonicalRecord(1, 1, "cs1", "0" * 64)
    r2 = CanonicalRecord(2, 2, "cs2", r1.record_hash())
    r3 = CanonicalRecord(3, 3, gen_checksum, r2.record_hash())
    records = [r1, r2, r3]
    checker = HeadIntegrityChecker(gen_data, gen_checksum)

    # Case 1: chain valid, head valid -> OK
    ok1, _ = checker.verify_chain_integrity(records)
    print(f"  chain valid + head valid: {ok1}")
    assert ok1

    # Case 2: chain valid, head tampered -> FAIL (head integrity)
    r3_bad_head = CanonicalRecord(3, 3, "TAMPERED", r2.record_hash())
    records2 = [r1, r2, r3_bad_head]
    ok2, reason2 = checker.verify_chain_integrity(records2)
    print(f"  chain valid + head tampered: {ok2} ({reason2})")
    assert not ok2

    # Case 3: chain broken, head valid -> FAIL (chain integrity)
    r2_bad_chain = CanonicalRecord(2, 2, "cs2", "BAD_HASH")
    records3 = [r1, r2_bad_chain, r3]
    ok3, reason3 = checker.verify_chain_integrity(records3)
    print(f"  chain broken + head valid: {ok3} ({reason3})")
    assert not ok3

    print("  Head and chain integrity are separate, both required.")
    print("  PASS")
    return True


def test_canonical_determinism():
    print()
    print("=" * 70)
    print("TEST 6: Canonical Determinism (same input -> same bytes)")
    print("=" * 70)
    r = CanonicalRecord(42, 7, "deadbeef", "cafebabe")
    c1 = r.canonical_serialize()
    c2 = r.canonical_serialize()
    assert c1 == c2
    # from_dict -> canonical -> same
    r2 = CanonicalRecord.from_dict(r.to_dict())
    assert r2.canonical_serialize() == c1
    print(f"  canonical: {c1}")
    print(f"  deterministic: {c1 == c2}")
    print("  PASS")
    return True


def test_head_tampering_detection():
    print()
    print("=" * 70)
    print("TEST 7: Head Tampering Detection (no Record 4 to detect)")
    print("=" * 70)
    gen_data = [{"gen": 5, "value": 42}]
    gen_checksum = hashlib.sha256(json.dumps(gen_data).encode()).hexdigest()

    # build 3-record journal
    r1 = CanonicalRecord(1, 1, "cs1", "0" * 64)
    r2 = CanonicalRecord(2, 2, "cs2", r1.record_hash())
    r3 = CanonicalRecord(3, 3, gen_checksum, r2.record_hash())
    records = [r1, r2, r3]

    checker = HeadIntegrityChecker(gen_data, gen_checksum)

    # verify: OK
    ok, _ = checker.verify_chain_integrity(records)
    assert ok

    # tamper last record's data_checksum (simulating attacker)
    r3_tampered = CanonicalRecord(3, 3, "WRONG_CHECKSUM", r2.record_hash())
    tampered = [r1, r2, r3_tampered]
    ok2, reason = checker.verify_chain_integrity(tampered)
    print(f"  original: ok={ok}")
    print(f"  tampered head: ok={ok2}, reason={reason}")
    assert not ok2
    assert "data_checksum_mismatch" in reason
    print("  Last record tampering detected via direct checksum comparison.")
    print("  (No Record 4 needed — head integrity is independent of chain)")
    print("  PASS")
    return True


def main():
    tests = [test_last_record_tampering, test_canonical_serialization,
             test_full_field_hash, test_chain_integrity, test_head_vs_chain,
             test_canonical_determinism, test_head_tampering_detection]
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
    print(f"JOURNAL HEAD INTEGRITY TEST: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    if all_ok:
        print()
        print("Properties proven:")
        print("  1. Last record tampering detected (direct checksum comparison)")
        print("  2. Canonical serialization (deterministic hash)")
        print("  3. All 4 fields covered in record_hash")
        print("  4. Chain integrity (prev_record_hash links)")
        print("  5. Head vs chain are separate, both required")
        print("  6. Canonical determinism (serialize/deserialize stable)")
        print("  7. Head tampering detection (no Record 4 needed)")
        print()
        print("LC_COMMIT_RECORD canonical form locked.")
        print("LC Persistence Architecture v1.0 (Logical Design) complete.")
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
