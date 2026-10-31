"""phase_g_f16_gate.py — D2 phase 1: does f16 change recall?

Owner gate (2026-10-31): f16 recall@10 must stay within 0.5% of f32.

This touches no production path. It converts the real index, reads it back
through the mmap sidecar, and compares three things:

  1. element error      — max / mean |x_f16 - x_f32| and the cosine drift
  2. neighbour overlap  — top-10 by f16 vs top-10 by f32, per query
  3. container recall   — the same kmeans K=26 / probes 1,2,4 path as phase B,
                          run once on f32 and once on f16

It also times the load, because the reason to do this at all is the reload
window (the loader is background now, so a shorter load is a real win).

Run: python phase_g_f16_gate.py [n_rows] [n_queries]
"""
import os
import sys
import tempfile
import time

import numpy as np

from phase_b_gate import kmeans, recall_at, TOPK

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from convert_jsonl_to_f16 import (read_jsonl, write_binary, read_binary,  # noqa: E402
                                  DTYPES)

INDEX = r"I:\tools\ann\data\memcore\ann_index.jsonl"
GATE = 0.005          # 0.5%
N_Q = 200
K = 26
PROBES = (1, 2, 4)


def topk(X, qidx, k=TOPK):
    Q = X[qidx]
    S = Q @ X.T
    out = [np.empty(0, dtype=np.int64)] * len(X)
    for i, qi in enumerate(qidx):
        out[qi] = np.argsort(-S[i])[:k]
    return out


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n_q = int(sys.argv[2]) if len(sys.argv) > 2 else N_Q

    t0 = time.time()
    n, dim, X32 = read_jsonl(INDEX, n_rows or None)
    t_jsonl = time.time() - t0
    print(f"corpus {n} rows, dim {dim}")
    print(f"load JSONL      {t_jsonl:6.2f}s   "
          f"{os.path.getsize(INDEX)/1048576:7.1f} MB")

    tmp = tempfile.mkdtemp(prefix="f16gate_")
    p16 = os.path.join(tmp, "idx.f16")
    p32 = os.path.join(tmp, "idx.f32")
    write_binary(p16, X32, "f16")
    write_binary(p32, X32, "f32")

    t0 = time.time()
    _, X16 = read_binary(p16)
    t_bin = time.time() - t0
    _, X32r = read_binary(p32)
    print(f"load f16 binary {t_bin:6.2f}s   "
          f"{os.path.getsize(p16)/1048576:7.1f} MB  "
          f"({t_jsonl/max(t_bin,1e-6):.0f}x faster)")

    # the f32 sidecar must be lossless, or nothing below means anything
    exact = np.array_equal(X32, X32r)
    print(f"\nf32 sidecar bit-identical to JSONL: {exact}")
    if not exact:
        sys.exit("f32 sidecar is not lossless - the container is broken")

    err = np.abs(X16 - X32)
    denom = np.linalg.norm(X32, axis=1) * np.linalg.norm(X16, axis=1) + 1e-12
    cos = (X16 * X32).sum(axis=1) / denom
    print(f"\n--- element error (f16 vs f32) ---")
    print(f"  max  |dx| {err.max():.3e}   mean |dx| {err.mean():.3e}")
    print(f"  cosine   min {cos.min():.6f}  mean {cos.mean():.6f}")
    print(f"  rows with cos < 0.9999: {int((cos < 0.9999).sum())}")

    rng = np.random.default_rng(3)
    qidx = rng.choice(n, min(n_q, n), replace=False).tolist()

    t32 = topk(X32, qidx)
    t16 = topk(X16, qidx)
    keep = np.mean([len(set(t32[qi].tolist()) & set(t16[qi].tolist())) / TOPK
                    for qi in qidx])
    print(f"\n--- neighbour overlap, {len(qidx)} queries ---")
    print(f"  top-{TOPK} f16 vs f32 = {keep:.4f} "
          f"({keep*TOPK:.2f} of {TOPK} neighbours identical)")
    print(f"  gate: recall may drop by at most {GATE:.1%} "
          f"=> overlap must be >= {1-GATE:.4f}")

    print(f"\n--- container recall, kmeans K={K}, truth = f32 brute force ---")
    res = {}
    for tag, X in (("f32", X32), ("f16", X16)):
        C, bucket_of = kmeans(X, K, seed=7)
        occ = np.bincount(bucket_of, minlength=K) / len(X)
        print(f"[{tag}] occupancy max {occ.max():.1%} min {occ.min():.1%} "
              f"used {(occ>0).sum()}/{K}")
        for p in PROBES:
            rec, scan = recall_at(X, bucket_of, C, qidx, t32, probes=p)
            res[(tag, p)] = rec
            print(f"  probes={p}  recall@{TOPK}={rec:.4f}  scan={scan:.1%}")

    print(f"\n--- verdict ---")
    worst = 0.0
    for p in PROBES:
        d = res[("f32", p)] - res[("f16", p)]
        worst = max(worst, d)
        verdict = "PASS" if d <= GATE else "FAIL"
        print(f"  probes={p}  drop {d:+.4f}  {verdict}")
    ov = "PASS" if keep >= 1 - GATE else "FAIL"
    print(f"  neighbour overlap {keep:.4f}  {ov}")
    print(f"  worst container drop {worst:+.4f} "
          f"(gate {GATE:.4f})  "
          f"{'PASS' if worst <= GATE else 'FAIL'}")


if __name__ == "__main__":
    main()
