import json
import tempfile
import unittest
from pathlib import Path

from relation_sidecar import load


class RelationSidecarConsumerTest(unittest.TestCase):
    def make(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sidecar = Path(directory.name) / "routes.jsonl"
        sidecar.write_bytes(b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows))
        import hashlib
        digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        manifest = Path(str(sidecar) + ".manifest.json")
        manifest.write_text(json.dumps({"v": 1, "rows": len(rows), "sha256": digest}), encoding="utf-8")
        return sidecar, manifest

    def test_manifest_and_typed_relation_walk(self):
        rows = [
            {"v": 1, "sid": "a", "intent": "relation", "relation_edges": [
                {"to": "b", "kind": "equals", "dir": "out"}], "value_routes": [99]},
            {"v": 1, "sid": "b", "intent": "relation", "relation_edges": [
                {"to": "c", "kind": "same-form", "dir": "out"}]},
            {"v": 1, "sid": "c", "intent": "relation", "relation_edges": []},
        ]
        sidecar, manifest = self.make(rows)
        graph, _ = load(sidecar, manifest)
        self.assertEqual(graph.walk("a", depth=2), {"a", "b", "c"})
        self.assertEqual(graph.walk("a", depth=2, kind="equals"), {"a", "b"})

    def test_walk_is_bounded_and_deterministic(self):
        rows = [{"v": 1, "sid": x, "intent": "relation", "relation_edges": [
            {"to": y, "kind": "link", "dir": "out"} for y in sorted(targets)]}
                for x, targets in (("a", ["d", "b", "c"]), ("b", []), ("c", []), ("d", []))]
        sidecar, manifest = self.make(rows)
        graph, _ = load(sidecar, manifest)
        self.assertEqual(graph.walk("a", depth=1, cap=3), {"a", "b", "c"})
        self.assertEqual(graph.walk("a", depth=1, cap=3), graph.walk("a", depth=1, cap=3))

    def test_tampered_sidecar_rejected(self):
        sidecar, manifest = self.make([{"v": 1, "sid": "a", "intent": "relation"}])
        sidecar.write_bytes(sidecar.read_bytes().replace(b'"a"', b'"x"'))
        with self.assertRaises(ValueError):
            load(sidecar, manifest)


if __name__ == "__main__":
    unittest.main()
