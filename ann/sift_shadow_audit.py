"""Offline SIFT1M receipt audit using one vector/receipt SID universe."""
import argparse
import json
from pathlib import Path

import numpy as np


def read_fvecs(path, limit=None):
    raw = np.fromfile(path, dtype=np.uint8)
    dim = int(np.frombuffer(raw[:4], dtype="<i4")[0])
    stride = 4 + dim * 4
    count = len(raw) // stride
    if limit:
        count = min(count, limit)
    return raw[:count * stride].reshape(count, stride)[:, 4:].view("<f4")


def read_ivecs(path, limit=None):
    raw = np.fromfile(path, dtype=np.uint8)
    dim = int(np.frombuffer(raw[:4], dtype="<i4")[0])
    stride = 4 + dim * 4
    count = len(raw) // stride
    if limit:
        count = min(count, limit)
    return raw[:count * stride].reshape(count, stride)[:, 4:].view("<i4")


def audit(base_path, query_path, gt_path, receipt_path, queries=100, topk=10):
    base = read_fvecs(base_path)
    query = read_fvecs(query_path, queries)
    gt = read_ivecs(gt_path, queries)
    receipts = {}
    for line in Path(receipt_path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        receipts[row["sid"]] = row["totem_receipt"]
    checked = valid = missing = 0
    recall = 0.0
    for qi, q in enumerate(query):
        scores = base @ q
        ids = np.argpartition(-scores, topk)[:topk]
        found = set(int(x) for x in ids)
        truth = set(int(x) for x in gt[qi][:topk])
        recall += len(found & truth) / topk
        for idx in ids:
            checked += 1
            if f"sift-{int(idx):07d}" in receipts:
                valid += 1
            else:
                missing += 1
    return {"queries": len(query), "topk": topk, "checked": checked,
            "valid_receipts": valid, "missing_receipts": missing,
            "receipt_coverage": valid / checked if checked else 0.0,
            "recall_at_k": recall / len(query) if len(query) else 0.0,
            "semantic": False}


def audit_ids(base, query, gt, receipts, topk=10):
    """Audit an exact row universe where every candidate has a receipt."""
    checked = valid = 0
    recall = 0.0
    for qi, q in enumerate(query):
        scores = base @ q
        ids = np.argpartition(-scores, topk)[:topk]
        truth = set(int(x) for x in gt[qi][:topk])
        recall += len(set(int(x) for x in ids) & truth) / topk
        for idx in ids:
            checked += 1
            if f"sift-{int(idx):07d}" in receipts:
                valid += 1
    return {"checked": checked, "valid_receipts": valid,
            "missing_receipts": checked - valid,
            "receipt_coverage": valid / checked if checked else 0.0,
            "recall_at_k": recall / len(query) if len(query) else 0.0,
            "semantic": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sift_dir")
    ap.add_argument("receipts")
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--topk", type=int, default=10)
    a = ap.parse_args()
    root = Path(a.sift_dir)
    print(json.dumps(audit(root / "sift_base.fvecs", root / "sift_query.fvecs",
                           root / "sift_groundtruth.ivecs", a.receipts,
                           a.queries, a.topk)))


if __name__ == "__main__":
    main()
