import unittest
from pathlib import Path

import numpy as np

from sift_receipt_probe import build_receipts
from sift_shadow_audit import audit_ids, read_fvecs, read_ivecs


class SiftExactUniverseTest(unittest.TestCase):
    def test_10k_receipt_coverage_is_exact(self):
        root = Path(r"I:\DWGLS-native-fs\build\sift1m\sift")
        hier = Path(r"I:\DWGLS-native-fs\build\sift1m_hier")
        if not (root / "sift_base.fvecs").exists():
            self.skipTest("SIFT artifacts unavailable")
        base = read_fvecs(root / "sift_base.fvecs")[:10_000]
        query = read_fvecs(root / "sift_query.fvecs", 20)
        gt = read_ivecs(root / "sift_groundtruth.ivecs", 20)
        receipts = {row["sid"]: row["totem_receipt"]
                    for row in build_receipts(hier, 10_000)}
        got = audit_ids(base, query, gt, receipts, topk=10)
        self.assertEqual(got["checked"], 200)
        self.assertEqual(got["valid_receipts"], 200)
        self.assertEqual(got["missing_receipts"], 0)
        self.assertEqual(got["receipt_coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
