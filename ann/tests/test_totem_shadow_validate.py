import unittest

from totem_shadow_validate import ValidationContext, shadow_validate


class TotemShadowTest(unittest.TestCase):
    def setUp(self):
        self.ctx = ValidationContext(7, frozenset({1, 2}), 4,
                                     frozenset({"L", "R"}), "relation")

    def test_shadow_never_drops_candidates(self):
        candidates = [
            {"sid": "ok", "totem_receipt": {"generation": 7, "totem_id": 1,
              "frustum_id": 4, "wang_edge": "L", "relation_valid": True}},
            {"sid": "bad", "totem_receipt": {"generation": 6, "totem_id": 9,
              "frustum_id": 3, "wang_edge": "X", "relation_valid": False}},
            {"sid": "old"},
        ]
        report = shadow_validate(candidates, self.ctx)
        self.assertEqual(report["counts"], {
            "checked": 3, "valid": 1, "invalid": 1, "missing_receipt": 1})
        self.assertEqual(len(report["findings"]), len(candidates))

    def test_unknown_intent_does_not_require_relation_receipt(self):
        ctx = ValidationContext(7, frozenset({1}), 4, frozenset({"L"}))
        report = shadow_validate([{"sid": "x", "totem_receipt": {
            "generation": 7, "totem_id": 1, "frustum_id": 4, "wang_edge": "L"}}], ctx)
        self.assertEqual(report["counts"]["valid"], 1)


if __name__ == "__main__":
    unittest.main()
