"""embedder_headtohead_scifact_2026-11-02.py — same corpus, two embedders.

Question from the owner: does EmbeddingGemma-2 (unsloth GGUF) work here, and is
it worth switching? It runs, but running is not quality. This measures both on
the SAME 5,183-doc SciFact corpus with the SAME recall@10 protocol as the
recorded baseline (0.7699), so the comparison is apples to apples.

Fairness rules applied here:
  - each model gets its own prompt format (e5: "passage: "/"query: ";
    gemma-2: "title: ... | text: ..." / "task: search result | query: ...").
    Feeding raw text to gemma-2 flattens similarities to ~0.6 and would make it
    lose for the wrong reason.
  - the same corpus ids, same qrels, same top-k as the baseline. recall@10 is
    read off the top 10, not the top 100 (an earlier draft of this file got
    that wrong and reported a meaningless 0.033).
  - embeddinggemma-2 is Matryoshka-trained, so its 768-d vectors are also
    scored truncated to 384 and 256 (renormalized) — 384 is the dim the current
    index uses, which decides whether a switch needs a full rebuild.

Hard limit worth knowing: both servers run with -c 512, so a document with more
than ~512 tokens is rejected with HTTP 500 ("input ... is too large"). Docs are
capped and, on a 500, the offending batch is split and the item re-truncated
rather than silently dropped.

Not measured here: the ANN index/container/router. Brute-force cosine only.

Run: python experiments/embedder_headtohead_scifact_2026-11-02.py
"""
import json
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "beir" / "scifact" / "scifact"
RECEIPT = ROOT / "experiments" / "embedder_headtohead_scifact_2026-11-02.json"
TOPK = 10
BATCH = 32
DOC_CHARS = 1000   # same budget for both models: e5-small is capped at 512 tokens

MODELS = {
    "e5-small-q8": {"port": 18097, "doc": "\u0000", "query": "query: "},
    "embeddinggemma-2-q8": {"port": 18096, "doc": "\u0000", "query": "\u0000"},
}


_FOLD = {"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2018": "'", "\u2019": "'",
         "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u2026": "...", "\u2032": "'"}


def fold(s):
    """ASCII-only, applied to BOTH models so the comparison stays fair.

    Not cosmetic: the e5 q8_0 GGUF served on :18095 answers HTTP 500
    ("invalid unordered_map<K, T> key") for text containing U+00A0, and the live
    server is affected too (verified 2026-11-02). One SciFact doc also trips it
    on a sequence around U+2212. gemma-2 accepts both, but feeding the models
    different text would make the recall numbers incomparable."""
    s = unicodedata.normalize("NFKD", s)
    for k, v in _FOLD.items():
        s = s.replace(k, v)
    return s.encode("ascii", "ignore").decode("ascii")


def doc_text(model, title, text):
    """Model-specific document rendering. Kept as code, not a format string:
    'passage: '.format(...) silently produced 5,183 copies of 'passage: '."""
    body = fold((title + " " + text)[:DOC_CHARS])
    if model.startswith("e5"):
        return "passage: " + body
    return "title: " + fold(title[:200]) + " | text: " + body


def query_text(model, text):
    if model.startswith("e5"):
        return "query: " + fold(text)
    return "task: search result | query: " + fold(text)


def _post(port, texts):
    body = json.dumps({"model": "m", "input": texts}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/embeddings" % port,
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            rows = json.loads(r.read())["data"]
    except urllib.error.HTTPError as e:
        # name the port and the server's own message: "500" alone cost a debug
        # round trip, and the server explains itself in the body
        raise urllib.error.HTTPError(e.url, e.code,
                                     "port %d: %s" % (port, e.read()[:200].decode("utf-8", "replace")),
                                     e.hdrs, None) from None
    vecs = np.asarray([row["embedding"] for row in sorted(rows, key=lambda d: d["index"])],
                      dtype=np.float32)
    assert len(vecs) == len(texts), "asked %d, got %d" % (len(texts), len(vecs))
    return vecs


def embed(port, texts, tries=3):
    """A long input is a 500 ("batch too small") or a 400 ("over max context"),
    not a bad vector: split the batch and shorten the offender instead of
    dropping it (a dropped row leaves a garbage vector and quietly poisons the
    whole measurement)."""
    try:
        return _post(port, texts)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 500):
            raise
        if len(texts) > 1:
            mid = max(1, len(texts) // 2)
            return np.vstack([embed(port, texts[:mid]), embed(port, texts[mid:])])
        for cut in (500, 250, 100):
            try:
                return _post(port, [texts[0][:cut]])
            except urllib.error.HTTPError:
                continue
        raise
    except Exception:
        if tries == 0:
            raise
        time.sleep(2)
        return embed(port, texts, tries - 1)


def embed_all(port, texts):
    out = None
    t0 = time.time()
    for i in range(0, len(texts), BATCH):
        v = embed(port, texts[i:i + BATCH])
        if out is None:
            out = np.empty((len(texts), v.shape[1]), dtype=np.float32)
        out[i:i + len(v)] = v
    return out, time.time() - t0


def load():
    corpus, order = {}, []
    with open(BASE / "corpus.jsonl", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            order.append(o["_id"])
            corpus[o["_id"]] = (o.get("title") or "", o.get("text") or "")
    queries, qorder = {}, []
    with open(BASE / "queries.jsonl", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            qorder.append(o["_id"])
            queries[o["_id"]] = o["text"]
    qrels = {}
    with open(BASE / "qrels" / "test.tsv", encoding="utf-8") as f:
        f.readline()
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and int(p[2]) > 0:
                qrels.setdefault(p[0], set()).add(p[1])
    return corpus, order, queries, qorder, qrels


def score(C, Q, order, qorder, qrels):
    Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
    Qn = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)
    rec = rec100 = mrr = 0.0
    total = 0
    for qi, qid in enumerate(qorder):
        if qid not in qrels:
            continue
        truth = qrels[qid]
        sims = Cn @ Qn[qi]
        rank = np.argsort(-sims)
        got10 = {order[i] for i in rank[:TOPK]}
        got100 = {order[i] for i in rank[:100]}
        rec += len(got10 & truth) / min(TOPK, len(truth))
        rec100 += len(got100 & truth) / min(100, len(truth))
        hits = [r for r, i in enumerate(rank[:TOPK]) if order[i] in truth]
        mrr += 1.0 / (hits[0] + 1) if hits else 0.0
        total += 1
    return {"recall_at_10": round(rec / total, 4), "recall_at_100": round(rec100 / total, 4),
            "mrr_at_10": round(mrr / total, 4), "scored_queries": total}


def main():
    corpus, order, queries, qorder, qrels = load()
    print("corpus %d queries %d qrels_q %d" % (len(order), len(qorder), len(qrels)), flush=True)

    arms = {}
    for name, cfg in MODELS.items():
        dtxt = [doc_text(name, *corpus[d]) for d in order]
        qtxt = [query_text(name, queries[q]) for q in qorder]
        C, tc = embed_all(cfg["port"], dtxt)
        Q, tq = embed_all(cfg["port"], qtxt)
        arms[name] = {"dim": int(C.shape[1]), "embed_sec": round(tc + tq, 1),
                      **score(C, Q, order, qorder, qrels)}
        print(name, arms[name], flush=True)

        for d in (512, 384, 256):
            if d < C.shape[1]:
                key = "%s-trunc%d" % (name, d)
                arms[key] = {"dim": d, "embed_sec": 0.0,
                             **score(np.ascontiguousarray(C[:, :d]),
                                     np.ascontiguousarray(Q[:, :d]), order, qorder, qrels)}
                print(key, arms[key], flush=True)

    receipt = {
        "dataset": "scifact (BEIR), test qrels",
        "protocol": "recall@10 brute-force cosine, same ids/qrels as the 0.7699 baseline",
        "baseline_recall_at_10": 0.7699,
        "baseline_note": "multilingual-e5-small int8 ONNX, recorded in beir_scifact_baseline.json",
        "server": "llama-server b12032, --embedding, -c 512 (max ~512 tokens per input)",
        "prompt_formats": {"e5-small-q8": {"doc": "passage: {title} {text}", "query": "query: {q}"},
                           "embeddinggemma-2-q8": {"doc": "title: {title} | text: {text}",
                                                   "query": "task: search result | query: {q}"}},
        "doc_char_cap": DOC_CHARS,
        "arms": arms,
        "router_measured": False,
    }
    RECEIPT.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("\n=== RECEIPT ===")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
