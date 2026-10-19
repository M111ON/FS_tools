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

Live index (2026-10-19): 61,722 x 384, text 254.3 MB, float32 94.8 MB = 2.68x.
The ratio is a property of the DATA (how many characters each float prints as),
not of the format, so it drifts as the corpus grows - it measured 2.44x earlier
the same day on a different generation of the same index. The floor asserted
below is therefore a floor, not a constant, and the measured ratio is printed on
every run so the drift stays visible.

Memory: the original built a Python list of 61k lists of 384 floats (~760 MB)
plus a blob dict, and OOMed an 8 GB box. This version streams the index into
float32 arrays and does every matrix op in row blocks - peak ~200 MB.

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
CHUNK = 4096          # rows per block: bounds peak RAM in every matrix op

FAILED = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("ok  " if cond else "FAIL", name,
                         "" if cond else "  <- " + str(detail)))
    if not cond:
        FAILED.append(name)


def load_index():
    """Stream the JSONL index into a float32 matrix.

    Holding 61k lists of 384 Python floats costs ~760 MB (8x the matrix) and
    OOMs an 8 GB box. Parse row by row into float32 arrays and stack once:
    peak ~200 MB, steady 95 MB.

    The live server rewrites this file, so tolerate it changing under us - a
    truncated tail row is dropped, and the byte count is what we actually
    consumed, so the size ratio stays consistent with the rows we measured.
    """
    rows, sids, nbytes = [], [], 0
    with open(IDX, "rb") as f:
        for line in f:
            nbytes += len(line)
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                break                      # truncated tail mid-rewrite
            rows.append(np.asarray(o["vec"], dtype=np.float32))
            sids.append(o["sid"])
    A = np.stack(rows)
    del rows
    return A, sids, nbytes


def load_store(sids, dim):
    """Fill S from the store's float32 blobs, one row at a time (no blob dict)."""
    pos = {s: i for i, s in enumerate(sids)}
    S = np.zeros((len(sids), dim), dtype=np.float32)
    found = np.zeros(len(sids), dtype=bool)
    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True, timeout=60)
    for sid, blob in con.execute("SELECT entry_id, vec FROM embeddings"):
        i = pos.get(sid)
        if i is None:
            continue
        S[i] = np.frombuffer(blob, dtype=np.float32)
        found[i] = True
    con.close()
    return S, found


def blocks(X, mask=None):
    for i in range(0, len(X), CHUNK):
        blk = X[i:i + CHUNK]
        yield blk if mask is None else blk[mask[i:i + CHUNK]]


def binary_blocks(X):
    """The bytes we would actually write, read back - values must be identical."""
    for blk in blocks(X):
        yield np.frombuffer(blk.tobytes(), dtype=np.float32).reshape(blk.shape)


def int8_blocks(X):
    """Dequantise per block, so a full 95 MB float32 copy of C never exists."""
    for blk in blocks(X):
        mx = np.abs(blk).max(axis=1, keepdims=True)
        q = np.round(blk / mx * 127.0).clip(-127, 127).astype(np.int8)
        yield q.astype(np.float32) * mx / 127.0


def topk_sets(chunks, Qn, topk=TOPK):
    """Top-k index set per query, computed in corpus blocks (bounded RAM).

    Per-block argpartition, then merge the block winners and keep the global
    top-k - same result as one full-width argpartition for distinct sims.
    """
    cands = [[] for _ in range(len(Qn))]
    base = 0
    for blk in chunks:
        nb = np.linalg.norm(blk, axis=1, keepdims=True) + 1e-12
        sims = (blk / nb) @ Qn.T
        k = min(topk, sims.shape[0])
        for r in range(sims.shape[1]):
            part = np.argpartition(-sims[:, r], k - 1)[:k]
            merged = cands[r] + list(zip(sims[part, r].tolist(),
                                        (part + base).tolist()))
            merged.sort(key=lambda t: -t[0])
            cands[r] = merged[:topk]
        base += len(blk)
    return [set(i for _, i in c) for c in cands]


def main():
    if not (os.path.exists(IDX) and os.path.exists(STORE)):
        print("  skip  index or store missing")
        return 0

    A, sids, n_txt = load_index()
    N, D = A.shape
    S, found = load_store(sids, D)
    missing = int((~found).sum())
    check("every index sid is present in the store", missing == 0, missing)

    # 1. the current text format already differs from the store float32
    d_txt, cos_txt = 0.0, 1.0
    for a, s in zip(blocks(A, found), blocks(S, found)):
        if not len(a):
            continue
        d_txt = max(d_txt, float(np.abs(a - s).max()))
        c = (a * s).sum(1) / (np.linalg.norm(a, axis=1) *
                              np.linalg.norm(s, axis=1) + 1e-12)
        cos_txt = min(cos_txt, float(c.min()))
    print(f"  current text round-6: max|diff|={d_txt:.2e} cosine_min={cos_txt:.9f}")
    check("text round-6 error is tiny (<1e-5)", d_txt < 1e-5, d_txt)
    check("text round-6 cosine ~1 (>0.99999)", cos_txt > 0.99999, cos_txt)

    # 2. binary float32 is lossless w.r.t. what we would write
    rt = all(np.array_equal(blk, b) for blk, b in
             zip(blocks(A), binary_blocks(A)))
    check("binary float32 round-trips byte-exact", rt)
    check("binary float32 changes NO value vs current text",
          all(float(np.abs(b - a).max()) == 0.0
              for a, b in zip(blocks(A), binary_blocks(A))))

    # 3. int8 is lossy - measure it, do not assume
    d8 = 0.0
    for a, c in zip(blocks(A), int8_blocks(A)):
        d8 = max(d8, float(np.abs(c - a).max()))
    print(f"  int8: max|diff|={d8:.2e}")
    check("int8 is measurably lossy (>1e-4)", d8 > 1e-4, d8)

    # 4. ranking impact, oracle = store float32
    rng = np.random.default_rng(7)
    qi = rng.choice(N, size=min(NQ, N), replace=False)
    Qn = S[qi] / (np.linalg.norm(S[qi], axis=1, keepdims=True) + 1e-12)
    base = topk_sets(blocks(S, found), Qn)

    def ov(chunks):
        got = topk_sets(chunks, Qn)
        return float(np.mean([len(a & c) / TOPK for a, c in zip(base, got)]))

    o_txt = ov(blocks(A, found))
    o_bin = ov(binary_blocks(A))
    o_i8 = ov(int8_blocks(A))
    print(f"  top-{TOPK} overlap: text={o_txt:.4f} float32={o_bin:.4f} int8={o_i8:.4f}")
    check("float32 ranking == text ranking (no loss)",
          abs(o_bin - o_txt) < 1e-9, f"{o_bin} vs {o_txt}")
    check("float32 ranking == store oracle (no loss)",
          abs(o_bin - 1.0) < 1e-9, o_bin)
    # int8 loss is real and it GROWS with the corpus: 0.9910 @ 15,430 rows,
    # 0.9863 @ 30,861, 0.9873 @ 46,291, 0.9887 @ 61,722 (2026-10-19). A pin on
    # a single constant would be a pin on one corpus generation, so pin the
    # FLOOR and the CHARACTER of the loss instead: measurably lossy, strictly
    # worse than the exact formats, and still under 2% of the top-10 slots.
    check("int8 keeps >=98% of top-10 (small, real loss)", o_i8 >= 0.98, o_i8)
    check("int8 is strictly lossier than float32 (which is exact)",
          o_i8 < o_bin, f"int8={o_i8} float32={o_bin}")

    # 5. the size argument
    n_bin = N * D * 4
    n_i8 = N * D * 1
    print(f"  size: text={n_txt/1e6:.1f} MB  float32={n_bin/1e6:.1f} MB  "
          f"int8={n_i8/1e6:.1f} MB  ratio={n_txt/n_bin:.2f}x")
    check("float32 is smaller than the text form", n_bin < n_txt,
          f"{n_bin} vs {n_txt}")
    # Floor, not a constant: each float prints a variable number of characters,
    # so the ratio moves with the corpus (2.44x-2.68x on 2026-10-19).
    check("float32 is at least 2x smaller than text", n_bin * 2 < n_txt,
          f"{n_txt/n_bin:.2f}x")

    print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %s" % FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
