import unittest

from totem_shadow_validate import ValidationContext, validate_receipt


class SiftReceiptNegativeTest(unittest.TestCase):
    def setUp(self):
        self.ctx = ValidationContext(1, frozenset({0, 1, 2}), 3,
                                     frozenset({"L0", "L1", "L2", "L3"}),
                                     "unknown")
        self.good = {"generation": 1, "totem_id": 1, "frustum_id": 3,
                     "wang_edge": "L2"}

    def test_good_receipt_passes(self):
        self.assertEqual(validate_receipt(self.good, self.ctx), [])

    def test_stale_and_wrong_receipts_are_reported(self):
        bad = dict(self.good, generation=2, frustum_id=9, wang_edge="bad")
        self.assertEqual(set(validate_receipt(bad, self.ctx)), {
            "stale_generation", "frustum_mismatch", "wang_edge_mismatch"})


if __name__ == "__main__":
    unittest.main()
