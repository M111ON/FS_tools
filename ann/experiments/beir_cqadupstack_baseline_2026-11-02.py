"""beir_cqadupstack_baseline_2026-11-02.py — BEIR track: CQADupStack recall@10.

Third dataset of the BEIR track (task-t78c67779). The earlier attempts failed on
the download only: the local zip was a truncated 2.10 GB of a 5.34 GB file, and
Colab could not finish the transfer. The zip is complete now and CRC-verified
(61 entries, 12 sub-forums).

Official protocol = 12 separate sub-forum evaluations, never one pooled number
(owner policy: tracks stay separate). Each sub-forum gets its own corpus,
queries and qrels; the receipt reports every one plus the macro average.

Embedder is the project's own multilingual-e5-small q8_0, served by the llama
sidecar on :18095 — NOT the all-MiniLM used for the Colab runs, so this receipt
records `embedder` and is not comparable across those runs (#epoch rule).

Brute-force cosine, no ANN index: this is the track-2 ground truth the router
will later be scored against, not a router measurement.

Run: python experiments/beir_cqadupstack_baseline_2026-11-02.py [--sub unix]
Resumable: a finished sub-forum leaves an .npz and is skipped next time.
"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "beir" / "cqadupstack"
EMB = ROOT / "data" / "beir" / "cqadupstack_emb"
RECEIPT = ROOT / "experiments" / "beir_cqadupstack_baseline_2026-11-02.json"
EMBED_URL = "http://127.0.0.1:18095/v1/embeddings"
EMBEDDER = "multilingual-e5-small-q8_0 (llama-server :18095)"
BATCH = 32
TOPK = 10
MAX_CHARS = 2000


def embed(texts, tries=3):
    """One batch through the sidecar; retries because a dropped socket mid-corpus
    would otherwise cost the whole sub-forum."""
    body = json.dumps({"model": "e5", "input": texts}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                EMBED_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                rows = json.loads(r.read())["data"]
            vecs = [row["embedding"] for row in sorted(rows, key=lambda d: d["index"])]
            if len(vecs) != len(texts):
                raise ValueError("asked %d, got %d" % (len(texts), len(vecs)))
            return np.asarray(vecs, dtype=np.float32)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def embed_all(texts, label):
    out = np.empty((len(texts), 384), dtype=np.float32)
    t0 = time.time()
    for i in range(0, len(texts), BATCH):
        out[i:i + BATCH] = embed([t[:MAX_CHARS] for t in texts[i:i + BATCH]])
        if i and i % (BATCH * 20) == 0:
            done = i + BATCH
            rate = done / max(1e-9, time.time() - t0)
            print("   %s %d/%d  %.1f docs/s  eta %.0f min"
                  % (label, done, len(texts), rate, (len(texts) - done) / max(rate, 1e-9) / 60),
                  flush=True)
    return out, time.time() - t0


def load_jsonl(path, kind):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if kind == "corpus":
                rows.append((o["_id"], (o.get("title") or "") + " " + (o.get("text") or "")))
            else:
                rows.append((o["_id"], o.get("text") or ""))
    return rows


def load_qrels(path):
    qrels = {}
    with open(path, encoding="utf-8") as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            qid, did, rel = parts[0], parts[1], int(parts[2])
            if rel > 0:
                qrels.setdefault(qid, set()).add(did)
    return qrels


def run_sub(sub):
    print("[%s] start" % sub, flush=True)
    corpus = load_jsonl(DATA / sub / "corpus.jsonl", "corpus")
    queries = load_jsonl(DATA / sub / "queries.jsonl", "queries")
    qrels = load_qrels(DATA / sub / "qrels" / "test.tsv")
    C, tc = embed_all(["passage: " + t for _, t in corpus], "corpus")
    Q, tq = embed_all(["query: " + t for _, t in queries], "queries")
    Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
    Qn = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)
    hits = total = scored = 0
    for qi, (qid, _) in enumerate(queries):
        if qid not in qrels:
            continue
        truth = qrels[qid]
        top = np.argpartition(-(Cn @ Qn[qi]), TOPK)[:TOPK]
        got = {corpus[i][0] for i in top}
        hits += len(got & truth)
        total += min(TOPK, len(truth))
        scored += 1
    rec = hits / total if total else 0.0
    np.savez(EMB / (sub + ".npz"), C=C, Q=Q,
             order=np.array([d for d, _ in corpus]),
             qorder=np.array([q for q, _ in queries]))
    return {"sub": sub, "corpus": len(corpus), "queries": len(queries),
            "qrels_queries": scored, "recall_at_10": round(float(rec), 4),
            "embed_sec": [round(tc, 1), round(tq, 1)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="", help="one sub-forum only (debug)")
    a = ap.parse_args()
    EMB.mkdir(parents=True, exist_ok=True)
    subs = [a.sub] if a.sub else sorted(
        d.name for d in DATA.iterdir() if (d / "corpus.jsonl").exists())

    receipt = []
    if RECEIPT.exists() and not a.sub:
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8")).get("subforums", [])
    done = {r["sub"] for r in receipt}

    for sub in subs:
        if sub in done:
            continue
        row = run_sub(sub)
        receipt = [r for r in receipt if r["sub"] != sub] + [row]
        payload = {
            "dataset": "cqadupstack (BEIR), 12 sub-forums",
            "embedder": EMBEDDER,
            "topk": TOPK,
            "metric": "recall@10 brute-force cosine (not the BEIR nDCG@10)",
            "semantics_note": "one number per sub-forum by protocol; the macro average is a summary, never a substitute",
            "subforums": sorted(receipt, key=lambda r: r["sub"]),
            "macro_recall_at_10": round(
                float(np.mean([r["recall_at_10"] for r in receipt])), 4) if receipt else None,
            "router_measured": False,
        }
        RECEIPT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(row), flush=True)

    print(json.dumps({"subforums_done": len(receipt),
                      "receipt": str(RECEIPT)}))


if __name__ == "__main__":
    main()
