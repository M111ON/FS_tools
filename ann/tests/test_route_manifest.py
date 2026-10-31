import json
import tempfile
import unittest
from pathlib import Path

from route_manifest import make_manifest, verify_manifest


class RouteManifestTest(unittest.TestCase):
    def files(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        side, index = d / "routes.jsonl", d / "index.jsonl"
        side.write_text('{"v":1,"sid":"a","intent":"unknown"}\n', encoding="utf-8")
        index.write_text('{"sid":"a"}\n', encoding="utf-8")
        return d, side, index

    def test_bound_manifest(self):
        d, side, index = self.files()
        manifest = make_manifest(side, index, "e1", 1)
        p = d / "manifest.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        got = verify_manifest(p, side, index, 1)
        self.assertEqual(got["producer_epoch"], "e1")

    def test_index_mismatch_rejected(self):
        d, side, index = self.files()
        manifest = make_manifest(side, index, "e1", 1)
        p = d / "manifest.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        index.write_text('{"sid":"changed"}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_manifest(p, side, index, 1)


if __name__ == "__main__":
    unittest.main()
