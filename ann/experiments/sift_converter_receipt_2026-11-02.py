"""sift_converter_receipt_2026-11-02.py — does the converted index actually hold SIFT?

scripts/fvecs_to_index.py is only useful if the bytes survive the trip, and a
self-check on synthetic rows cannot show that for a 10k row file with int8-valued
floats. So this reads the REAL siftsmall base, converts it, reads the index back
and replays the shipped ground truth with exact numpy search:

    recall@10(index) vs sift_groundtruth.ivecs

If the converter dropped, reshaped or reordered a row, exact search over the
index cannot reproduce the benchmark's own answers — the number comes out low
and this receipt says so. sanity only, not a router measurement: no container,
no anchors, no pruning is under test here.

Run: python experiments/sift_converter_receipt_2026-11-02.py [--dir data/sift]
Writes experiments/sift_converter_receipt_2026-11-02.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

from fvecs_to_index import convert                      # noqa: E402
from sift_shadow_audit import read_fvecs, read_ivecs   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "data", "sift", "siftsmall"))
    ap.add_argument("--topk", type=int, default=10)
    a = ap.parse_args()

    base_path = os.path.join(a.dir, "siftsmall_base.fvecs")
    out = os.path.join(ROOT, "data", "sift", "siftsmall_ann_index.jsonl")
    n = convert(base_path, out, prefix="sift")
    base = read_fvecs(base_path)
    query = read_fvecs(os.path.join(a.dir, "siftsmall_query.fvecs"))
    gt = read_ivecs(os.path.join(a.dir, "siftsmall_groundtruth.ivecs"))

    # the index is the thing being judged: search it, never `base`
    rows = []
    with open(out, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line)["vec"])
    idx = np.asarray(rows, dtype=np.float32)

    recall = 0.0
    for qi, q in enumerate(query):
        scores = idx @ q
        got = set(int(x) for x in np.argpartition(-scores, a.topk)[:a.topk])
        truth = set(int(x) for x in gt[qi][:a.topk])
        recall += len(got & truth) / a.topk

    receipt = {
        "dataset": "siftsmall",
        "base_rows": int(len(base)), "index_rows": int(len(idx)),
        "dim": [int(base.shape[1]), int(idx.shape[1])],
        "bytes_bit_identical": bool(np.array_equal(idx, base.astype(np.float32))),
        "queries": int(len(query)), "topk": a.topk,
        "recall_at_k": recall / len(query) if len(query) else 0.0,
        "index_path": out,
        "router_measured": False,
    }
    Path(os.path.join(ROOT, "experiments",
                      "sift_converter_receipt_2026-11-02.json")).write_text(
        json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
