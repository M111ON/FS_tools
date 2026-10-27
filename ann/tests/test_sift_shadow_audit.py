import unittest
from pathlib import Path

from sift_shadow_audit import audit


class SiftShadowAuditTest(unittest.TestCase):
    def test_sift_receipts_join_same_universe(self):
        root = Path(r"I:\DWGLS-native-fs\build\sift1m\sift")
        receipt = Path(r"ann/data/memcore/sift10k_receipts.jsonl")
        if not (root / "sift_base.fvecs").exists() or not receipt.exists():
            self.skipTest("SIFT artifacts unavailable")
        result = audit(root / "sift_base.fvecs", root / "sift_query.fvecs",
                       root / "sift_groundtruth.ivecs", receipt, queries=100, topk=10)
        self.assertEqual(result["checked"], 1000)
        # The receipt fixture intentionally covers only rows 0..9999; SIFT
        # nearest neighbours may land outside that subset.  The join must
        # report that coverage honestly rather than treating missing rows as
        # valid.
        self.assertGreater(result["valid_receipts"], 0)
        self.assertGreater(result["missing_receipts"], 0)


if __name__ == "__main__":
    unittest.main()
