#!/usr/bin/env python3
"""test_vec_format.py - can the index store vectors as binary instead of JSON text?

Answers the question with measurements on the REAL index, and pins the result so
the claim cannot drift.

Findings (2026-10-14, 59,967 x 384):
  * the served index stores vec as JSON TEXT round(x, 6) - 247.0 MB
  * text round-6 is NOT byte-identical to the store's float32, but the error is
    ~5e-7 max and cosine >= 0.99999988; top-10 overlap vs the store oracle 99.93%
  * binary float32 (92.1 MB) holds the SAME float32 values -> 0.0 diff, and is
    byte-round-trip lossless. Cheaper than text with no data change.
  * int8 per-vector scale (23.0 MB) is LOSSY: max|diff| ~9e-4, cosine min
    0.99995, top-10 overlap 99.33% - ~0.6% of top-10 slots change.

So: binary float32 = no loss (recommended). int8 = real, small loss (a choice).

Run:  python tests/test_vec_format.py
"""
import json
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ANN = os.path.dirname(HERE)
IDX = os.path.join(ANN, "data", "memcore", "ann_index.jsonl")
STORE = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
NQ, TOPK = 300, 10

FAILED = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name,
                         "" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(name)


def main():
    if not (os.path.exists(IDX) and os.path.exists(STORE)):
        print("  skip  index or store missing")
        return 0

    sids, txt = [], []
    with open(IDX, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            sids.append(o["sid"])
            txt.append(o["vec"])
    A = np.asarray(txt, dtype=np.float32)
    N, D = A.shape

    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True, timeout=60)
    blob = dict(con.execute("SELECT entry_id, vec FROM embeddings").fetchall())
    con.close()
    S = np.asarray([np.frombuffer(blob[s], dtype=np.float32) for s in sids], dtype=np.float32)

    # 1. the current text format already differs from the store float32
    d_txt = float(np.abs(A - S).max())
    cos_txt = float(((A * S).sum(1) / (np.linalg.norm(A, axis=1) *
                                       np.linalg.norm(S, axis=1))).min())
    print(f"  current text round-6: max|diff|={d_txt:.2e} cosine_min={cos_txt:.9f}")
    check("text round-6 error is tiny (<1e-5)", d_txt < 1e-5, d_txt)
    check("text round-6 cosine ~1 (>0.99999)", cos_txt > 0.99999, cos_txt)

    # 2. binary float32 is lossless w.r.t. what we would write
    b = A.tobytes()
    B = np.frombuffer(b, dtype=np.float32).reshape(N, D)
    check("binary float32 round-trips byte-exact", np.array_equal(A, B))
    check("binary float32 changes NO value vs current text",
          float(np.abs(B - A).max()) == 0.0)

    # 3. int8 is lossy - measure it, do not assume
    mx = np.abs(A).max(axis=1, keepdims=True)
    q = np.round(A / mx * 127.0).clip(-127, 127).astype(np.int8)
    C = q.astype(np.float32) * mx / 127.0
    d8 = float(np.abs(C - A).max())
    print(f"  int8: max|diff|={d8:.2e}")
    check("int8 is measurably lossy (>1e-4)", d8 > 1e-4, d8)

    # 4. ranking impact, oracle = store float32
    rng = np.random.default_rng(7)
    qi = rng.choice(N, size=min(NQ, N), replace=False)
    Qn = S[qi] / (np.linalg.norm(S[qi], axis=1, keepdims=True) + 1e-12)

    def topk(X):
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
        sims = Qn @ Xn.T
        out = []
        for r in range(sims.shape[0]):
            i = np.argpartition(-sims[r], TOPK - 1)[:TOPK]
            out.append(set(i.tolist()))
        return out

    base = topk(S)

    def ov(X):
        return float(np.mean([len(a & c) / TOPK for a, c in zip(base, topk(X))]))

    o_txt, o_bin, o_i8 = ov(A), ov(B), ov(C)
    print(f"  top-{TOPK} overlap: text={o_txt:.4f} float32={o_bin:.4f} int8={o_i8:.4f}")
    check("float32 ranking == text ranking (no loss)",
          abs(o_bin - o_txt) < 1e-9, f"{o_bin} vs {o_txt}")
    check("int8 keeps >=99% of top-10 (small, real loss)", o_i8 >= 0.99, o_i8)

    # 5. the size argument
    n_txt = os.path.getsize(IDX)
    n_bin = N * D * 4
    n_i8 = N * D * 1
    print(f"  size: text={n_txt/1e6:.1f} MB  float32={n_bin/1e6:.1f} MB  int8={n_i8/1e6:.1f} MB")
    check("float32 is ~2.7x smaller than text", n_bin < n_txt / 2.5, f"{n_bin} vs {n_txt}")

    print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %s" % FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
