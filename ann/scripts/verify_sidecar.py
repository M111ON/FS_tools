"""verify_sidecar.py — strong check of a sidecar against its JSONL. Off the hot path.

The in-band guard (index_sidecar.check_fresh) reads 8 KB and compares
size + mtime_ns + a first/last-4 KB fingerprint. That is enough to catch a
rewrite, but it cannot catch a change that keeps the size and both windows
intact — a restored file, or an edit in the middle with the timestamp put back.
This script is the slow, honest answer: parse the whole JSONL and compare every
vector against the sidecar.

Run in CI or on a schedule, never in a load path.

    python verify_sidecar.py --jsonl ann_index.jsonl --bin ann_index.f16
    python verify_sidecar.py ... --tolerance 1e-3     # f16 default: exact f32 compare
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_jsonl_to_f16 import read_jsonl          # noqa: E402
from index_sidecar import check_fresh, read_index    # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--bin", dest="binary", required=True)
    ap.add_argument("--tolerance", type=float, default=None,
                    help="max |dx| allowed; default 0 for f32, 1e-3 for f16")
    a = ap.parse_args()

    t0 = time.time()
    info, Y = read_index(a.binary)
    t1 = time.time()
    n, dim, X = read_jsonl(a.jsonl)
    t2 = time.time()

    tol = a.tolerance
    if tol is None:
        tol = 0.0 if info["dtype"] == "f32" else 1e-3

    print(f"sidecar {info['dtype']} {info['n_rows']}x{info['dim']} "
          f"({os.path.getsize(a.binary)/1048576:.1f} MB, {t1-t0:.2f}s to open)")
    print(f"jsonl   f32     {n}x{dim} "
          f"({os.path.getsize(a.jsonl)/1048576:.1f} MB, {t2-t1:.2f}s to parse)")

    bad = 0
    if (n, dim) != (info["n_rows"], info["dim"]):
        print(f"FAIL shape: jsonl {n}x{dim} vs sidecar "
              f"{info['n_rows']}x{info['dim']}")
        bad += 1
    else:
        d = np.abs(Y - X)
        worst = float(d.max())
        i, j = np.unravel_index(int(d.argmax()), d.shape)
        print(f"max |dx| {worst:.3e} at row {i} col {j}  (tolerance {tol:g})")
        if worst > tol:
            print("FAIL: vectors differ beyond tolerance")
            bad += 1
        if info["guarded"]:
            try:
                check_fresh(info, a.jsonl)
                print("guard: fresh")
            except Exception as e:
                print(f"guard: {e}")
                bad += 1
        else:
            print("guard: none (v1 header)")

    print(f"\n{'OK' if not bad else 'FAILED'}  ({time.time()-t0:.1f}s)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
