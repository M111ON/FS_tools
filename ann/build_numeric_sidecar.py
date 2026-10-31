"""Build a deterministic numeric producer sidecar and manifest fixture."""
import argparse
import hashlib
import json
from pathlib import Path


def build(n):
    rows = []
    for i in range(n):
        a, b = i % 97 + 1, i % 31 + 1
        result = a + b
        sid = f"eq-{i:06d}"
        rows.append({
            "v": 1, "sid": sid, "intent": "relation",
            "value_routes": sorted({a, b, result}),
            "shape_routes": [0],
            "relation_edges": [{"to": f"eq-{(i + 1) % n:06d}",
                                 "kind": "same-form", "dir": "out"}],
            "junction_routes": [0], "producer_seq": i,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--rows", type=int, default=2000)
    a = ap.parse_args()
    rows = build(a.rows)
    canonical = bytearray()
    for row in rows:
        canonical.extend(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    out = Path(a.output)
    out.write_bytes(canonical)
    digest = hashlib.sha256(canonical).hexdigest()
    manifest = {"v": 1, "rows": len(rows), "producer_epoch": "numeric-fixture-v1", "sha256": digest}
    out.with_suffix(out.suffix + ".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote rows={len(rows)} sha256={digest} path={out}")


if __name__ == "__main__":
    main()
