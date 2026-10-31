"""convert_jsonl_to_f16.py — ANN index: JSONL (source of truth) -> binary sidecar.

The JSONL stays the source of truth. This writes a mmap-friendly binary that the
load path can read without parsing JSON, which is what the reload window is
spent on (247 MB text, ~50 s of json.loads).

Header (little-endian, 28 bytes):

    offset size field
      0     8   magic   "ANNIDX\\0\\0"
      8     4   version u32  (1)
     12     4   dtype   u32  (0 = f32, 1 = f16)
     16     4   dim     u32
     20     8   n       u64
     28     ..  data    n * dim * itemsize, row-major, little-endian

28 is a multiple of 2 and of 4, so both dtypes land aligned for numpy.

Row i of the binary IS row i of the JSONL. Nothing else is stored: no sid, no
text, no offsets. If the JSONL is regenerated in a different order the binary
must be regenerated too — the file carries no way to detect that, which is why
the JSONL remains the truth and this is only a cache.

Run: python convert_jsonl_to_f16.py --in ann_index.jsonl --out ann_index.f16
     python convert_jsonl_to_f16.py ... --dtype f32 --limit 1000
"""
import argparse
import json
import os
import struct
import sys
import time

import numpy as np

MAGIC = b"ANNIDX\0\0"
VERSION = 1
DTYPES = {"f32": (0, np.float32, 4), "f16": (1, np.float16, 2)}
HEADER = struct.Struct("<8sIIIQ")


def read_jsonl(path, limit=None, dim=None):
    """Return (n, dim, float32 array) in file order.

    dim=None infers it from the first usable row, so this works for any embedder
    size (384 for e5-small, 768 for mpnet, ...).
    """
    vecs = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if limit is not None and len(vecs) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            v = o.get("vec")
            if not v:
                continue
            if dim is None:
                dim = len(v)
            if len(v) != dim:
                continue
            vecs.append(v)
    if not vecs:
        raise SystemExit("no usable rows")
    return len(vecs), dim, np.asarray(vecs, dtype=np.float32)


def write_binary(path, X, dtype="f16"):
    code, np_dt, itemsize = DTYPES[dtype]
    n, dim = X.shape
    with open(path, "wb") as f:
        f.write(HEADER.pack(MAGIC, VERSION, code, dim, n))
        X.astype(np_dt, copy=False).tofile(f)
    return n, dim, itemsize


def read_binary(path):
    """mmap a sidecar -> (dtype name, float32 array). Raises on a bad header."""
    with open(path, "rb") as f:
        head = f.read(HEADER.size)
    magic, version, code, dim, n = HEADER.unpack(head)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported version {version}")
    name = {v[0]: k for k, v in DTYPES.items()}[code]
    np_dt = DTYPES[name][1]
    mm = np.memmap(path, dtype=np_dt, mode="r", offset=HEADER.size,
                   shape=(n, dim))
    return name, np.asarray(mm, dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--dtype", default="f16", choices=sorted(DTYPES))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if os.path.exists(a.dst) and not a.force:
        raise SystemExit(f"exists, use --force: {a.dst}")

    t0 = time.time()
    n, dim, X = read_jsonl(a.src, a.limit)
    t1 = time.time()
    n2, dim2, itemsize = write_binary(a.dst, X, a.dtype)
    t2 = time.time()

    src_mb = os.path.getsize(a.src) / 1048576
    dst_mb = os.path.getsize(a.dst) / 1048576
    print(f"rows {n}  dim {dim}  dtype {a.dtype}")
    print(f"read   {t1-t0:6.1f}s  {src_mb:8.1f} MB  {a.src}")
    print(f"write  {t2-t1:6.1f}s  {dst_mb:8.1f} MB  {a.dst}")
    print(f"ratio  {src_mb/dst_mb:6.1f}x smaller")
    if (n, dim) != (n2, dim2):
        sys.exit("header disagrees with the data that was written")


if __name__ == "__main__":
    main()
