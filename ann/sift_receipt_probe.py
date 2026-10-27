"""Create and audit structural receipts from the existing SIFT1M hierarchy.

SIFT assignments are structural test data, not semantic truth.
"""
import argparse
import hashlib
import json
from pathlib import Path


def build_receipts(hier, count=10_000, generation=1):
    import numpy as np
    root = Path(hier)
    coarse = np.load(root / "_coarse_assign.npy", mmap_mode="r")[:count]
    fine = np.load(root / "_fine_assign.npy", mmap_mode="r")[:count]
    l3 = np.load(root / "_l3_assign.npy", mmap_mode="r")[:count]
    rows = []
    for i, (c, f, leaf) in enumerate(zip(coarse, fine, l3)):
        rows.append({"sid": f"sift-{i:07d}", "totem_receipt": {
            "generation": generation, "totem_id": int(c),
            "frustum_id": int(f), "wang_edge": f"L{int(leaf)}"}})
    return rows


def write_receipts(path, rows):
    canonical = bytearray()
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            raw = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            stream.write(raw.decode() + "\n")
            canonical.extend(raw + b"\n")
    return hashlib.sha256(canonical).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hier")
    ap.add_argument("out")
    ap.add_argument("--count", type=int, default=10_000)
    args = ap.parse_args()
    rows = build_receipts(args.hier, args.count)
    digest = write_receipts(args.out, rows)
    print(json.dumps({"rows": len(rows), "sha256": digest, "semantic": False}))


if __name__ == "__main__":
    main()
