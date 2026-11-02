"""colab_beir_cqadupstack.py — CQADupStack recall@10 on a Colab GPU (BEIR track).

Paste this file into a Colab cell (or `!python` it) — it is self-contained: it
installs what it needs, downloads the corpus, embeds and prints the receipt.

WHY COLAB: the local box has 2 cores and the sidecar serves ~0.5 docs/s, so 457k
docs would take days. The scifact and fiqa baselines were measured on a T4 with
all-MiniLM-L6-v2 (18.0 s / 117.4 s embed), so this run uses the SAME model to
stay comparable with them. The project's own e5-small numbers are a separate
epoch — never mix the two in one table.

PROTOCOL: 12 sub-forums, one number each (BEIR evaluates them separately). The
macro average is a summary, not a substitute. Metric here is recall@10, matching
the earlier receipts, not BEIR's nDCG@10.

Deliverable: prints one JSON. Save it as
ann/experiments/beir_cqadupstack_gpu_2026-11-02.json on the machine.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/cqadupstack.zip"
ZIP = "/content/cqadupstack.zip"
ROOT = "/content/cqadupstack"
PKG_DIR = "/content/beir_embeddings"
MODEL = "all-MiniLM-L6-v2"
TOPK = 10
MAX_CHARS = 2000
BATCH = 256


def ensure_data():
    if not os.path.isdir(ROOT):
        if not os.path.exists(ZIP):
            print("downloading corpus (~5.3 GB)...", flush=True)
            urllib.request.urlretrieve(URL, ZIP)
        with zipfile.ZipFile(ZIP) as z:
            z.extractall("/content")
    return sorted(d for d in os.listdir(ROOT)
                  if os.path.isfile(os.path.join(ROOT, d, "corpus.jsonl")))


def load_jsonl(path, corpus):
    ids, texts = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            ids.append(o["_id"])
            texts.append(((o.get("title") or "") + " " + (o.get("text") or ""))[:MAX_CHARS]
                         if corpus else (o.get("text") or ""))
    return ids, texts


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


def dump_package(sub, cids, qids, C, Q, qrels_path):
    """Same layout the local box reads: <sub>/{corpus,query}_ids.json + .npy,
    plus the qrels, so the package scores on its own with no external files."""
    import numpy as np
    d = os.path.join(PKG_DIR, sub)
    os.makedirs(d, exist_ok=True)
    json.dump(cids, open(os.path.join(d, "corpus_ids.json"), "w", encoding="utf-8"))
    json.dump(qids, open(os.path.join(d, "query_ids.json"), "w", encoding="utf-8"))
    np.save(os.path.join(d, "corpus_embeddings.npy"), C)
    np.save(os.path.join(d, "query_embeddings.npy"), Q)
    if os.path.exists(qrels_path):
        shutil.copyfile(qrels_path, os.path.join(d, "test.tsv"))


def main():
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "sentence-transformers"])

    import numpy as np
    from sentence_transformers import SentenceTransformer

    subs = ensure_data()
    model = SentenceTransformer(MODEL, device="cuda")
    dim = int(model.get_sentence_embedding_dimension())
    print("subforums:", subs, "| model:", MODEL, "| dim:", dim, flush=True)

    rows = []
    for sub in subs:
        base = os.path.join(ROOT, sub)
        cid, ctext = load_jsonl(os.path.join(base, "corpus.jsonl"), True)
        qid, qtext = load_jsonl(os.path.join(base, "queries.jsonl"), False)
        qrels = load_qrels(os.path.join(base, "qrels", "test.tsv"))
        t0 = time.time()
        C = model.encode(ctext, batch_size=BATCH, normalize_embeddings=True,
                         show_progress_bar=False).astype("float32")
        Q = model.encode(qtext, batch_size=BATCH, normalize_embeddings=True,
                         show_progress_bar=False).astype("float32")
        dt = time.time() - t0
        hits = total = scored = 0
        for qi, q in enumerate(qid):
            if q not in qrels:
                continue
            scores = C @ Q[qi]
            top = np.argpartition(-scores, TOPK)[:TOPK]
            got = {cid[i] for i in top}
            hits += len(got & qrels[q])
            total += min(TOPK, len(qrels[q]))
            scored += 1
        row = {"sub": sub, "corpus": len(cid), "queries": len(qid),
               "qrels_queries": scored, "recall_at_10": round(hits / total, 4),
               "embed_sec": round(dt, 1)}
        rows.append(row)
        print(json.dumps(row), flush=True)
        dump_package(sub, cid, qid, C, Q, os.path.join(base, "qrels", "test.tsv"))

    macro = round(sum(r["recall_at_10"] for r in rows) / len(rows), 4)
    receipt = {
        "dataset": "cqadupstack (BEIR), 12 sub-forums",
        "embedder": MODEL + " (Colab GPU)",
        "topk": TOPK,
        "metric": "recall@10 brute-force cosine (not BEIR nDCG@10)",
        "protocol": "one number per sub-forum; macro average is a summary only",
        "subforums": rows,
        "macro_recall_at_10": macro,
        "router_measured": False,
    }
    # The embedding package is what the local box scores and re-uses; without a
    # manifest nobody can tell which model produced the .npy files, so this run
    # writes one (the earlier package had none and the embedder had to be
    # guessed from vector statistics).
    with open(os.path.join(PKG_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "produced_by": "colab_beir_cqadupstack.py",
            "embedder": MODEL,
            "dim": dim,
            "doc_char_cap": MAX_CHARS,
            "pooling": "sentence-transformers default (mean), normalize_embeddings=True",
            "prompt_format": "doc = title + ' ' + text (no prefix); query = text (no prefix)",
            "corpus_rows": sum(r["corpus"] for r in rows),
            "subforums": [r["sub"] for r in rows],
            "qrels": "copied in-package as <sub>/test.tsv; identical to BeIR/cqadupstack-qrels on the Hub",
            "macro_recall_at_10": macro,
        }, f, indent=2)

    sha = shutil.make_archive("/content/beir_embeddings_package", "zip", PKG_DIR)
    print("\ndownload:", sha)
    path = "/content/beir_cqadupstack_gpu_2026-11-02.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
    print("\n=== RECEIPT ===")
    print(json.dumps(receipt, indent=2))
    print("\nsaved:", path, "- download it into ann/experiments/")


if __name__ == "__main__":
    main()
