import json
import tempfile
import unittest
from pathlib import Path

from export_unknown_routes import export


class UnknownExportTest(unittest.TestCase):
    def test_never_infers_semantics(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        index = d / "index.jsonl"
        out = d / "routes.jsonl"
        index.write_text(json.dumps({"sid": "s1", "kind": "relation", "term_hint": "x=y"}) + "\n")
        count, digest = export(index, out)
        self.assertEqual((count, len(digest)), (1, 64))
        row = json.loads(out.read_text())
        self.assertEqual(row["intent"], "unknown")
        self.assertEqual(row["relation_edges"], [])
        self.assertEqual(row["value_routes"], [])


if __name__ == "__main__":
    unittest.main()
