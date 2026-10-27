import tempfile
import unittest
from pathlib import Path

from sift_receipt_probe import build_receipts, write_receipts


class SiftReceiptTest(unittest.TestCase):
    def test_subset_has_stable_structural_receipts(self):
        root = Path(r"I:\DWGLS-native-fs\build\sift1m_hier")
        if not (root / "_coarse_assign.npy").exists():
            self.skipTest("SIFT hierarchy unavailable")
        rows = build_receipts(root, 10_000)
        self.assertEqual(len(rows), 10_000)
        self.assertEqual(rows[0]["sid"], "sift-0000000")
        with tempfile.TemporaryDirectory() as d:
            a = write_receipts(Path(d) / "a.jsonl", rows)
            b = write_receipts(Path(d) / "b.jsonl", rows)
            self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
