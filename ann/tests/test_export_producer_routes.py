import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from export_producer_routes import export


class ProducerExportTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))
        self.index = self.d / "index.jsonl"
        self.index.write_text('{"sid":"a"}\n{"sid":"missing"}\n', encoding="utf-8")
        self.db = self.d / "routes.db"
        c = sqlite3.connect(self.db)
        c.executescript((Path(__file__).resolve().parents[1] / "route_metadata_schema.sql").read_text())
        c.execute("insert into route_metadata values (?,?,?,?,?,?)",
                  ("a", "relation", "[1,2]", "[3]", "[4]", 9))
        c.execute("insert into relation_edges values (?,?,?,?)", ("a", "b", "equals", "out"))
        c.commit(); c.close()

    def test_metadata_and_missing_safe_baseline(self):
        out = self.d / "routes.jsonl"
        n, digest = export(self.index, self.db, out)
        self.assertEqual((n, len(digest)), (2, 64))
        rows = [json.loads(x) for x in out.read_text().splitlines()]
        self.assertEqual(rows[0]["intent"], "relation")
        self.assertEqual(rows[0]["relation_edges"][0]["kind"], "equals")
        self.assertEqual(rows[1]["intent"], "unknown")


if __name__ == "__main__":
    unittest.main()
