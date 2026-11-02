#!/usr/bin/env python3
"""fvecs_to_index.py — SIFT-style .fvecs -> ann_index.jsonl (what ann_search parses).

The SIFT track had receipts but no way to feed the raw benchmark into the
server: .fvecs is [int32 dim][dim * float32] per row, the index is one JSON
object per row. This is that join, and nothing else — no embedding, no anchors,
no sorting. Row i of the index IS row i of the .fvecs, because SIFT ground
truth is expressed in row ordinals.

    python fvecs_to_index.py --base sift_base.fvecs --out data/sift/ann_index.jsonl
    python fvecs_to_index.py --self-check      # proves the parser, no data needed

Rows whose declared dim disagrees with the first row are refused with the row
number: a silent reshape would produce a file that parses and is wrong.
"""
import argparse
import json
import os
import sys
import tempfile

import numpy as np


def iter_fvecs(path, limit=0, batch=4096):
    """Yield (dim, rows) batches of float32, strided exactly like the file."""
    with open(path, "rb") as f:
        head = f.read(4)
        if len(head) < 4:
            return
        dim = int(np.frombuffer(head, dtype="<i4")[0])
        if dim <= 0:
            raise ValueError("%s: first row declares dim %d" % (path, dim))
        stride = 4 + dim * 4
        f.seek(0)
        seen = 0
        while True:
            want = batch if not limit else min(batch, limit - seen)
            if want <= 0:
                return
            raw = f.read(want * stride)
            if not raw:
                return
            rows = len(raw) // stride
            if rows == 0 or len(raw) % stride:
                raise ValueError("%s: truncated record at row %d (dim %d, %d stray bytes)"
                                 % (path, seen + rows, dim, len(raw) % stride))
            buf = np.frombuffer(raw[:rows * stride], dtype=np.uint8)
            buf = buf.reshape(rows, stride)
            declared = np.frombuffer(buf[:, :4].tobytes(), dtype="<i4")
            bad = np.nonzero(declared != dim)[0]
            if bad.size:
                raise ValueError("%s: row %d declares dim %d, first row says %d"
                                 % (path, seen + int(bad[0]), int(declared[bad[0]]), dim))
            yield dim, buf[:, 4:].copy().view("<f4")
            seen += rows


def convert(base, out, prefix="sift", limit=0):
    tmp = out + ".part"
    n = 0
    with open(tmp, "w", encoding="utf-8") as w:
        for dim, rows in iter_fvecs(base, limit):
            for row in rows:
                w.write('{"sid": "%s-%07d", "file": "%s/%d", "npast": 0, "dim": %d, "vec": %s}\n'
                        % (prefix, n, prefix, n, dim,
                           json.dumps([float(x) for x in row])))
                n += 1
    os.replace(tmp, out)
    return n


def self_check():
    """Synthetic .fvecs in a temp dir: exact roundtrip, and a bad row is refused."""
    rng = np.random.default_rng(7)
    vecs = rng.standard_normal((5, 6)).astype("<f4")
    d = tempfile.mkdtemp(prefix="fvecs-")
    src, out = os.path.join(d, "base.fvecs"), os.path.join(d, "index.jsonl")
    with open(src, "wb") as f:
        for v in vecs:
            f.write(np.array([6], dtype="<i4").tobytes())
            f.write(v.tobytes())
    n = convert(src, out, prefix="chk")
    assert n == 5, "every row lands in the index"
    rows = [json.loads(l) for l in open(out, encoding="utf-8")]
    assert [r["sid"] for r in rows][:2] == ["chk-0000000", "chk-0000001"], "sid is the row ordinal"
    assert all(r["dim"] == 6 for r in rows), "dim comes from the record, not a guess"
    back = np.array([r["vec"] for r in rows], dtype=np.float32)
    assert np.allclose(back, vecs, atol=1e-6), "vectors roundtrip"
    assert np.array_equal(back, vecs.astype(np.float32)), "roundtrip is bit-exact at index precision"
    # a row that lies about its dim must stop the run, not reshape silently
    bad = os.path.join(d, "bad.fvecs")
    raw = open(src, "rb").read()
    open(bad, "wb").write(raw + np.array([5], dtype="<i4").tobytes() + b"\0" * 20)
    try:
        convert(bad, os.path.join(d, "bad.jsonl"))
    except ValueError as e:
        assert "dim" in str(e), "the refusal names the disagreement"
    else:
        raise AssertionError("a short trailing row must be refused")
    print("fvecs_to_index self-check: PASS (5 rows roundtrip bit-exact, bad row refused)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--out")
    ap.add_argument("--prefix", default="sift")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
        return
    if not (a.base and a.out):
        ap.error("--base and --out are required (or use --self-check)")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    n = convert(a.base, a.out, a.prefix, a.limit)
    print("%d rows -> %s" % (n, a.out))


if __name__ == "__main__":
    main()
