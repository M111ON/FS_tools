"""colab_embedder_headtohead.py — run the embedder comparison on a Colab GPU.

Self-contained: paste into a cell, or save it and `!python colab_embedder_headtohead.py`.
Needs a GPU runtime (Runtime -> Change runtime type -> T4 GPU).

WHY: measured locally on 2 cores, embeddinggemma-2 beat multilingual-e5-small on
SciFact by +0.216 recall@10 (0.9297 vs 0.7136), and even truncated to e5's own
384 dims it still won by +0.198. That was one English dataset on a slow box.
This script repeats it on a GPU over three BEIR datasets so the finding is not
a one-dataset, one-machine artifact.

WHAT IT REPORTS per (dataset, model, dim): recall@10, recall@100, MRR@10 and
embed seconds; for CQADupStack one line per sub-forum plus a macro average
(BEIR protocol: sub-forums are evaluated separately, never pooled).

FAIRNESS RULES BAKED IN
  - each model gets its own prompt format, and the script CLEARS the model's
    built-in prompts so the prefix comes from exactly one place (otherwise
    sentence-transformers adds its own on top and the comparison is meaningless);
  - Matryoshka arms (512/384/256) come from the same gemma-2 vectors,
    renormalized after truncation - no extra embedding work;
  - the same ASCII fold as the local receipt, so the two runs are comparable
    (it is also what the local e5 GGUF needs to survive some inputs);
  - corpus and qrels counts are asserted against the published sizes, so a
    truncated download fails loudly instead of reporting a number.

COST (T4, rough): scifact ~1 min/model, fiqa ~4 min/model, cqadupstack
~15-20 min for all-MiniLM and ~60-90 min for gemma-2. Embeddings are cached
under CACHE_DIR, so a rerun after a disconnect skips finished work. Trim
DATASETS / HF_MODELS below for a shorter first pass.
"""
import json
import os
import time
import unicodedata
import urllib.request
import zipfile

CACHE_DIR = "/content/embed_cache"
OUT_JSON = "/content/embedder_headtohead_gpu.json"
TOPK = 10
BATCH = 256
DOC_CHARS = 1000

# ---- what to run -----------------------------------------------------------
DATASETS = {
    # dataset -> models to embed. Every (dataset, model) pair is independent
    # and cached; trim freely.
    "scifact": ["all-MiniLM-L6-v2", "multilingual-e5-small", "embeddinggemma-2"],
    "fiqa": ["all-MiniLM-L6-v2", "multilingual-e5-small", "embeddinggemma-2"],
    "cqadupstack": ["multilingual-e5-small", "embeddinggemma-2"],
}

HF_MODELS = {
    # label -> (hf repo, doc template, query template)
    "all-MiniLM-L6-v2": ("sentence-transformers/all-MiniLM-L6-v2", "{x}", "{q}"),
    "multilingual-e5-small": ("intfloat/multilingual-e5-small", "passage: {x}", "query: {q}"),
    "embeddinggemma-2": ("google/embeddinggemma-2", "title: {t} | text: {x}",
                         "task: search result | query: {q}"),
}

URLS = {
    "scifact": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip",
    "fiqa": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/fiqa.zip",
    "cqadupstack": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/cqadupstack.zip",
}

# published corpus rows, and qrels queries where the dataset is a single split
EXPECTED_CORPUS = {"scifact": 5183, "fiqa": 57638, "cqadupstack": 457199}
EXPECTED_QRELS = {"scifact": 300, "fiqa": 648}
# ---------------------------------------------------------------------------

_FOLD = {"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2018": "'", "\u2019": "'",
         "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u2026": "...", "\u2032": "'"}


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    for k, v in _FOLD.items():
        s = s.replace(k, v)
    return s.encode("ascii", "ignore").decode("ascii")


def ensure_data(name):
    root = "/content/" + name
    if not os.path.isdir(root):
        zpath = "/content/%s.zip" % name
        if not os.path.exists(zpath):
            print("downloading %s ..." % name, flush=True)
            urllib.request.urlretrieve(URLS[name], zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall("/content")
    return root


def find_splits(root):
    """[(label, dir)] where dir holds corpus.jsonl + queries.jsonl + qrels/."""
    hits = []
    for base, _dirs, files in os.walk(root):
        if ("corpus.jsonl" in files and "queries.jsonl" in files
                and os.path.isdir(os.path.join(base, "qrels"))):
            hits.append(base)
    if not hits:
        raise RuntimeError("no BEIR split under %s" % root)
    hits.sort()
    keep = [h for h in hits if not any(h != o and o.startswith(h + os.sep) for o in hits)]
    return [(os.path.basename(h), h) for h in keep]


def load_split(path):
    ids, titles, texts = [], [], []
    with open(os.path.join(path, "corpus.jsonl"), encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            ids.append(o["_id"])
            titles.append(o.get("title") or "")
            texts.append(o.get("text") or "")
    qids, queries = [], []
    with open(os.path.join(path, "queries.jsonl"), encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            qids.append(o["_id"])
            queries.append(o.get("text") or "")
    qrels = {}
    qpath = os.path.join(path, "qrels", "test.tsv")
    if not os.path.exists(qpath):
        raise RuntimeError("missing qrels/test.tsv in %s" % path)
    with open(qpath, encoding="utf-8") as f:
        f.readline()
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and int(p[2]) > 0:
                qrels.setdefault(p[0], set()).add(p[1])
    return {"ids": ids, "titles": titles, "texts": texts,
            "qids": qids, "queries": queries, "qrels": qrels}


def encode(model, texts, cache, tag):
    import numpy as np
    if os.path.exists(cache):
        V = np.load(cache)["v"]
        assert len(V) == len(texts), "%s: cached %d rows for %d texts" % (tag, len(V), len(texts))
        print("   %s: %d rows (cached)" % (tag, len(V)), flush=True)
        return V
    t0 = time.time()
    V = model.encode(texts, batch_size=BATCH, normalize_embeddings=True,
                     show_progress_bar=True, convert_to_numpy=True).astype("float32")
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.savez(cache, v=V)
    print("   %s: %d rows, %.1f s (%.0f rows/s)" % (tag, len(V), time.time() - t0,
                                                    len(V) / max(1e-9, time.time() - t0)), flush=True)
    return V


def metrics(C, Q, d):
    import numpy as np
    ids, qids, qrels = d["ids"], d["qids"], d["qrels"]
    Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
    Qn = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)
    rec10 = rec100 = mrr = 0.0
    scored = 0
    for qi, qid in enumerate(qids):
        if qid not in qrels:
            continue
        truth = qrels[qid]
        rank = np.argsort(-(Cn @ Qn[qi]))
        rec10 += len({ids[i] for i in rank[:TOPK]} & truth) / min(TOPK, len(truth))
        rec100 += len({ids[i] for i in rank[:100]} & truth) / min(100, len(truth))
        hits = [r for r, i in enumerate(rank[:TOPK]) if ids[i] in truth]
        mrr += 1.0 / (hits[0] + 1) if hits else 0.0
        scored += 1
    if not scored:
        raise RuntimeError("no query had qrels - wrong split?")
    return {"recall_at_10": round(rec10 / scored, 4), "recall_at_100": round(rec100 / scored, 4),
            "mrr_at_10": round(mrr / scored, 4), "scored_queries": scored}


def main():
    try:
        import numpy as np  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        import subprocess
        import sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "sentence-transformers"])
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("device:", device, "|", torch.cuda.get_device_name(0) if device == "cuda" else "NO GPU - this will take hours", flush=True)

    receipt = {"device": device, "topk": TOPK, "doc_char_cap": DOC_CHARS,
               "ascii_fold": True, "prompts_applied_by": "this script only",
               "results": {}, "router_measured": False}

    for ds, model_names in DATASETS.items():
        splits = find_splits(ensure_data(ds))
        loaded = {label: load_split(path) for label, path in splits}
        total = sum(len(d["ids"]) for d in loaded.values())
        assert total == EXPECTED_CORPUS[ds], \
            "%s corpus rows %d != published %d (incomplete download?)" % (ds, total, EXPECTED_CORPUS[ds])
        print("\n=== %s: %d split(s), %d corpus rows%s" % (
            ds, len(splits), total,
            "" if len(splits) == 1 else " - one line per sub-forum plus macro, never a single pooled number"),
            flush=True)

        for mname in model_names:
            repo, dtpl, qtpl = HF_MODELS[mname]
            model = SentenceTransformer(repo, device=device)
            if hasattr(model, "prompts"):
                model.prompts = {}          # one source of prefixes: this script
                model.default_prompt_name = None
            dim = int(model.get_sentence_embedding_dimension())
            print("\n[%s / %s] dim %d" % (ds, mname, dim), flush=True)

            rows = {}
            for label, d in loaded.items():
                doc_txt = [dtpl.format(t=fold(t[:200]), x=fold((t + " " + x)[:DOC_CHARS]))
                           for t, x in zip(d["titles"], d["texts"])]
                qry_txt = [qtpl.format(q=fold(q)) for q in d["queries"]]
                C = encode(model, doc_txt, os.path.join(CACHE_DIR, "%s_%s__%s__doc.npz" % (ds, label, mname)), label + " docs")
                Q = encode(model, qry_txt, os.path.join(CACHE_DIR, "%s_%s__%s__qry.npz" % (ds, label, mname)), label + " queries")
                assert C.shape[1] == dim and Q.shape[1] == dim, \
                    "%s: %d dims, model says %d" % (mname, C.shape[1], dim)
                rows[label] = metrics(C, Q, d)
                print("   %-14s %s" % (label, rows[label]), flush=True)
                if dim > 384:               # Matryoshka: same vectors, narrower index
                    for cut in (512, 384, 256):
                        if cut < dim:
                            rows["%s-trunc%d" % (label, cut)] = metrics(
                                np.ascontiguousarray(C[:, :cut]), np.ascontiguousarray(Q[:, :cut]), d)
                            print("     trunc%-8d %s" % (cut, rows["%s-trunc%d" % (label, cut)]), flush=True)

                if ds in EXPECTED_QRELS and len(splits) == 1:
                    got = len(d["qrels"])
                    assert got == EXPECTED_QRELS[ds], \
                        "%s qrels queries %d != published %d" % (ds, got, EXPECTED_QRELS[ds])

            receipt["results"]["%s/%s" % (ds, mname)] = {
                "dim": dim, "splits": rows,
                "macro_recall_at_10": round(sum(r["recall_at_10"] for r in rows.values()) / len(rows), 4),
            }
            with open(OUT_JSON, "w", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2)
            del model
            torch.cuda.empty_cache()

    print("\n=== RECEIPT ===")
    print(json.dumps(receipt, indent=2))
    print("\nsaved:", OUT_JSON, "- download it into ann/experiments/")


if __name__ == "__main__":
    main()
