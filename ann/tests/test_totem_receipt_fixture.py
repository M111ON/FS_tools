import json
import tempfile
import unittest
from pathlib import Path

from totem_shadow_probe import load_receipts


class ReceiptFixtureTest(unittest.TestCase):
    def test_loads_producer_owned_receipts_by_sid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "receipts.jsonl"
            p.write_text(json.dumps({"sid": "a", "totem_receipt": {
                "generation": 7, "totem_id": 1, "frustum_id": 4,
                "wang_edge": "L"}}) + "\n", encoding="utf-8")
            got = load_receipts(p)
            self.assertEqual(got["a"]["generation"], 7)

    def test_rejects_missing_receipt_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text('{"sid":"a"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_receipts(p)


if __name__ == "__main__":
    unittest.main()
