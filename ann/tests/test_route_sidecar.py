import json
import tempfile
import unittest
from pathlib import Path

from validate_route_sidecar import validate


class RouteSidecarTest(unittest.TestCase):
    def write(self, rows):
        f = tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonl", delete=False)
        for row in rows:
            f.write((json.dumps(row) + "\n").encode())
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return f.name

    def test_valid_canonical_rows(self):
        p = self.write([{
            "v": 1, "sid": "a", "intent": "relation",
            "value_routes": [1, 3], "shape_routes": [4], "junction_routes": [7],
            "relation_edges": [{"to": "b", "kind": "equals", "dir": "out"}],
            "producer_seq": 1,
        }])
        rows, digest = validate(p)
        self.assertEqual(rows, 1)
        self.assertEqual(len(digest), 64)

    def test_duplicate_sid_rejected(self):
        row = {"v": 1, "sid": "a", "intent": "unknown"}
        with self.assertRaises(ValueError):
            validate(self.write([row, row]))

    def test_unsorted_route_rejected(self):
        row = {"v": 1, "sid": "a", "intent": "value", "value_routes": [2, 1]}
        with self.assertRaises(ValueError):
            validate(self.write([row]))


if __name__ == "__main__":
    unittest.main()
