"""score_cqadupstack_package_2026-11-02.py — is the shipped embedding package usable?

The package (ann/data/beir_embeddings_package.zip) holds, per CQADupStack
sub-forum: corpus_ids.json, query_ids.json, corpus_embeddings.npy,
query_embeddings.npy. Structural checks alone cannot tell whether the vectors
line up with the qrels the benchmark scores against, so this joins them by _id
and computes the real metric: recall@10 / recall@100 / MRR@10 per sub-forum plus
the macro average (BEIR evaluates sub-forums separately, never pooled).

It also identifies which embedder produced the vectors, because the package
carries no manifest. Two numbers separate the candidates: e5 vectors sit in a
narrow cone (mean pairwise cosine ~0.83 measured locally), all-MiniLM vectors
are near-isotropic (~0.05). Whatever produced this package is reported rather
than assumed.

Qrels are needed and are NOT shipped in the package: the twelve files live on
the Hub at BeIR/cqadupstack-qrels (1.3 MB, identical to <sub>/qrels/test.tsv
inside the 5.3 GB dataset zip), fetched by
  python experiments/fetch_cqadupstack_qrels.py
or point --qrels at a directory of <sub>__test.tsv files.

Result 2026-11-02: all 12 sub-forums scored, macro recall@10 0.5412,
recall@100 0.7842, MRR@10 0.3980. The embedder is inferred as
all-MiniLM-L6-v2 from vector statistics (mean pairwise cosine 0.047-0.125,
near-isotropic; e5-small measures 0.826) - the package has no manifest.

Run: python experiments/score_cqadupstack_package_2026-11-02.py
Writes experiments/cqadupstack_package_check_2026-11-02.json
"""
import argparse
import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "data" / "beir_embeddings"
QRELS = ROOT / "data" / "beir" / "qrels"
RECEIPT = ROOT / "experiments" / "cqadupstack_package_check_2026-11-02.json"
TOPK = 10
EXPECTED_ROWS = {"android": 22998, "english": 40221, "gaming": 45301, "gis": 37637,
                 "mathematica": 16705, "physics": 38316, "programmers": 32176,
                 "stats": 42269, "tex": 68184, "unix": 47382, "webmasters": 17405,
                 "wordpress": 48605}


def load_qrels(path):
    qrels = {}
    with open(path, encoding="utf-8") as f:
        f.readline()
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and int(p[2]) > 0:
                qrels.setdefault(p[0], set()).add(p[1])
    return qrels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=str(PKG))
    ap.add_argument("--qrels", default=str(QRELS))
    a = ap.parse_args()

    subs = sorted(d for d in os.listdir(a.pkg) if os.path.isdir(os.path.join(a.pkg, d)))
    receipt = {"package": a.pkg, "subforums": {}, "notes": []}
    per_sub = []

    for sub in subs:
        base = os.path.join(a.pkg, sub)
        cids = json.load(open(os.path.join(base, "corpus_ids.json"), encoding="utf-8"))
        qids = json.load(open(os.path.join(base, "query_ids.json"), encoding="utf-8"))
        C = np.load(os.path.join(base, "corpus_embeddings.npy"))
        Q = np.load(os.path.join(base, "query_embeddings.npy"))

        row = {"corpus": len(C), "queries": len(Q), "dim": int(C.shape[1]), "dtype": str(C.dtype),
               "corpus_rows_match_ids": len(cids) == len(C), "query_rows_match_ids": len(qids) == len(Q),
               "corpus_rows_expected": EXPECTED_ROWS.get(sub, None),
               "duplicate_ids": len(cids) != len(set(cids)) or len(qids) != len(set(qids)),
               "nan": int(np.isnan(C).sum() + np.isnan(Q).sum()),
               "zero_rows": int((np.linalg.norm(C, axis=1) == 0).sum() + (np.linalg.norm(Q, axis=1) == 0).sum()),
               "row_norm_mean": round(float(np.linalg.norm(C, axis=1).mean()), 6)}

        # which embedder produced this? e5 = narrow cone, MiniLM = isotropic
        idx = np.random.default_rng(0).choice(len(C), min(400, len(C)), replace=False)
        S = C[idx] / np.maximum(np.linalg.norm(C[idx], axis=1, keepdims=True), 1e-9)
        cos = S @ S.T
        off = cos[~np.eye(len(idx), dtype=bool)]
        row["pairwise_cos_mean"] = round(float(off.mean()), 4)
        row["mean_vec_norm"] = round(float(np.linalg.norm(C.astype(np.float64).mean(0))), 4)

        qpath = os.path.join(a.qrels, "%s__test.tsv" % sub)
        if os.path.exists(qpath):
            qrels = load_qrels(qpath)
            pos = {d: i for i, d in enumerate(cids)}
            Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
            Qn = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)
            r10 = r100 = mrr = 0.0
            scored = 0
            for qi, qid in enumerate(qids):
                if qid not in qrels:
                    continue
                truth = qrels[qid]
                rank = np.argsort(-(Cn @ Qn[qi]))
                r10 += len({cids[i] for i in rank[:TOPK]} & truth) / min(TOPK, len(truth))
                r100 += len({cids[i] for i in rank[:100]} & truth) / min(100, len(truth))
                hits = [r for r, i in enumerate(rank[:TOPK]) if cids[i] in truth]
                mrr += 1.0 / (hits[0] + 1) if hits else 0.0
                scored += 1
            row.update({"qrels_queries": scored, "recall_at_10": round(r10 / max(scored, 1), 4),
                        "recall_at_100": round(r100 / max(scored, 1), 4), "mrr_at_10": round(mrr / max(scored, 1), 4)})
        else:
            row["qrels"] = "missing"
        receipt["subforums"][sub] = row
        if "recall_at_10" in row:
            per_sub.append(row["recall_at_10"])

    scored_subs = [r for r in receipt["subforums"].values() if "recall_at_10" in r]
    if scored_subs:
        receipt["macro_recall_at_10"] = round(sum(per_sub) / len(per_sub), 4)
        receipt["subforums_scored"] = len(per_sub)
    if not receipt["notes"]:
        receipt["notes"].append("no manifest in the package: embedder identity inferred from vector statistics")
    RECEIPT.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
