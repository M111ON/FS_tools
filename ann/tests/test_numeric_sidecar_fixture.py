import json
import tempfile
import unittest
from pathlib import Path

from build_numeric_sidecar import build
from validate_route_sidecar import validate


class NumericSidecarFixtureTest(unittest.TestCase):
    def test_build_is_valid_and_deterministic(self):
        rows = build(32)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            p = Path(f.name)
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        p.write_bytes(b"".join(json.dumps(x, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                              for x in rows))
        count, digest = validate(p)
        self.assertEqual(count, 32)
        self.assertEqual(len(digest), 64)
        self.assertEqual(rows, build(32))


if __name__ == "__main__":
    unittest.main()
