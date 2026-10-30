#!/usr/bin/env python3
"""prep_miracl_th.py — labelled test set for the ANN router (queries + qrels + corpus).

Builds, in --out-dir:
    corpus.npy        float32 [N,384]  unit vectors, "passage: " prefix
    queries.npy       float32 [Q,384]  unit vectors, "query: "  prefix
    meta.json         docids, query ids, qrels (query idx -> corpus idx list), settings
    ann_index.jsonl   same rows in ann/ format {"sid","file","npast","dim","vec"}
                      (anchors are added later by POST /v1/state/regrow, as with build_index.py)

Embedder = the SAME package as your daemon/index build: ONNX int8 (EMBEDDERS.md #1).
Never mix with the GGUF sidecar. Text goes through the same sanitize() as build_index.py.

Sources
  --source hf      MIRACL Thai from Hugging Face. BROKEN on datasets>=3: both repos are
                   script-based (miracl.py) with no parquet, so load_dataset raises
                   "Dataset scripts are no longer supported". Use --source local with
                   scripts/miracl_local.py, which downloads the raw files and writes both
                   jsonl inputs.
  --source local   --queries-jsonl  {"query_id","query","positive_docids":[...]}
                   --corpus-jsonl   {"docid","title"(opt),"text"}

Keeps ALL positives of the chosen queries (never sampled away), then adds distractors.

Usage
  python prep_miracl_th.py --source hf --onnx model_int8.onnx --tokenizer tokenizer.json \
         --n-queries 200 --distractors 20000 --out-dir data/miracl_th
  python prep_miracl_th.py --source local --queries-jsonl q.jsonl --corpus-jsonl c.jsonl --fake-embed ...
        (--fake-embed = hashed bag-of-words vectors, for testing the plumbing ONLY)
"""
import argparse, hashlib, json, os, random, sys, time, unicodedata
import numpy as np

# ---------------- CONFIG ----------------
DIM            = 384
MAX_CHARS      = 400          # same clip as build_index.py
MAX_TOKENS     = 256
BATCH          = 32
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX   = "query: "
HF_QRELS       = ("miracl/miracl", "th", "dev")
HF_CORPUS      = ("miracl/miracl-corpus", "th", "train")
DUP_COS        = 0.995        # near-duplicate threshold for the sanity report
# ----------------------------------------


def sanitize(t: str, max_chars: int) -> str:
    """Copy of scripts/build_index.py:sanitize (NFKC + ascii folds + clip)."""
    t = unicodedata.normalize("NFKC", t)
    for bad, good in (("∉", " not in "), ("∋", " contains "), ("∖", " minus "), ("∝", " proportional to "),
                      ("∤", " does not divide "), ("≅", " ~= "), ("⋀", " AND "), ("⋁", " OR "),
                      ("⏭", " next "), ("⏸", " pause "), ("⟺", " <=> "), ("⬠", " pentagon ")):
        t = t.replace(bad, good)
    return t[:max_chars]


def clean_field(s, cap):
    """Copy of build_index.py:clean_field (regrow writes fields with raw fprintf)."""
    s = str(s).replace("\\", "/").replace('"', "'").replace("\r", " ").replace("\n", " ")
    return "".join(c if 32 <= ord(c) < 0x10000 else " " for c in s)[:cap]


# ---------------- embedders ----------------
class OnnxEmbedder:
    def __init__(self, onnx_path, tok_path):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(tok_path)
        self.tok.enable_truncation(MAX_TOKENS)
        self.tok.enable_padding()
        self.sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        self.names = {i.name for i in self.sess.get_inputs()}

    def __call__(self, texts):
        out = []
        for i in range(0, len(texts), BATCH):
            enc = self.tok.encode_batch(texts[i:i + BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.names:
                feed["token_type_ids"] = np.zeros_like(ids)
            h = self.sess.run(None, feed)[0]                       # [B,T,384]
            m = mask[..., None].astype(np.float32)
            v = (h * m).sum(1) / np.clip(m.sum(1), 1e-9, None)     # mean pool
            v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
            out.append(v.astype(np.float32))
            if (i // BATCH) % 50 == 0:
                print(f"  embedded {min(i + BATCH, len(texts))}/{len(texts)}", flush=True)
        return np.vstack(out)


class FakeEmbedder:
    """Hashed bag-of-words -> unit vector. Plumbing tests only, NOT a real embedding."""
    def __call__(self, texts):
        out = np.zeros((len(texts), DIM), np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                seed = int.from_bytes(hashlib.sha1(w.encode("utf-8")).digest()[:8], "little")
                out[i] += np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
        out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-12
        return out


# ---------------- sources ----------------
def _doc_text(r):
    return ((r.get("title") or "") + " " + (r.get("text") or "")).strip()


def source_hf(n_q, neg_per_q, distractors, stride, rng):
    from datasets import load_dataset
    ds = load_dataset(HF_QRELS[0], HF_QRELS[1], split=HF_QRELS[2], trust_remote_code=True)
    rows = [r for r in ds if r["positive_passages"]]
    rng.shuffle(rows)
    rows = rows[:n_q]
    docs, qs = {}, []
    for r in rows:
        pos = []
        for p in r["positive_passages"]:
            docs[p["docid"]] = _doc_text(p)
            pos.append(p["docid"])
        negs = r["negative_passages"]
        for p in rng.sample(negs, min(neg_per_q, len(negs))):
            docs.setdefault(p["docid"], _doc_text(p))
        qs.append((r["query_id"], r["query"], pos))
    if distractors:
        cds = load_dataset(HF_CORPUS[0], HF_CORPUS[1], split=HF_CORPUS[2], streaming=True, trust_remote_code=True)
        added = 0
        for i, r in enumerate(cds):
            if i % stride or r["docid"] in docs:
                continue
            docs[r["docid"]] = _doc_text(r)
            added += 1
            if added >= distractors:
                break
    return qs, docs


def source_local(qpath, cpath, n_q, distractors, rng):
    allq = [json.loads(l) for l in open(qpath, encoding="utf-8")]
    allq = [q for q in allq if q.get("positive_docids")]
    rng.shuffle(allq)
    allq = allq[:n_q]
    corpus = {}
    for l in open(cpath, encoding="utf-8"):
        r = json.loads(l)
        corpus[r["docid"]] = _doc_text(r)
    need = {d for q in allq for d in q["positive_docids"] if d in corpus}
    docs = {d: corpus[d] for d in need}
    rest = [d for d in corpus if d not in docs]
    rng.shuffle(rest)
    for d in rest[:distractors]:
        docs[d] = corpus[d]
    qs = [(q["query_id"], q["query"], [d for d in q["positive_docids"] if d in corpus]) for q in allq]
    return qs, docs


# ---------------- sanity ----------------
def sanity(C, Q, qrels, rng):
    print("\n== sanity ==")
    print(f"corpus {C.shape}  queries {Q.shape}")
    for name, M in (("corpus", C), ("queries", Q)):
        n = np.linalg.norm(M, axis=1)
        bad = int((~np.isfinite(M)).any(1).sum() + (n < 0.5).sum())
        print(f"  {name}: norm min/max {n.min():.3f}/{n.max():.3f}  bad rows (NaN or ~zero): {bad}")
    sub = C if len(C) <= 5000 else C[rng.choice(len(C), 5000, replace=False)]
    S = sub @ sub.T
    np.fill_diagonal(S, 0)
    iu = np.triu_indices(len(sub), 1)
    print(f"  pairs cos>{DUP_COS} (near-duplicates, {len(sub)}-row sample): {int((S[iu] > DUP_COS).sum())}")
    print(f"  corpus cosine: median {np.median(S[iu]):.3f}  99th {np.percentile(S[iu], 99):.3f}")
    npos = [len(v) for v in qrels.values()]
    print(f"  positives/query: min {min(npos)} median {int(np.median(npos))} max {max(npos)}")
    print("  (next: run eval_router.py -- brute-force vs qrels must look sane BEFORE trusting router numbers)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("hf", "local"), default="hf")
    ap.add_argument("--queries-jsonl"); ap.add_argument("--corpus-jsonl")
    ap.add_argument("--onnx"); ap.add_argument("--tokenizer")
    ap.add_argument("--fake-embed", action="store_true", help="plumbing test only")
    ap.add_argument("--n-queries", type=int, default=200)
    ap.add_argument("--neg-per-query", type=int, default=5, help="hf: hard negatives kept per query")
    ap.add_argument("--distractors", type=int, default=20000)
    ap.add_argument("--corpus-stride", type=int, default=20, help="hf: take every k-th corpus doc (NOT uniform over the corpus)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out-dir", default="data/miracl_th")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    nrng = np.random.default_rng(a.seed)
    if a.fake_embed:
        emb, emb_name = FakeEmbedder(), "FAKE"
    else:
        if not (a.onnx and a.tokenizer):
            sys.exit("need --onnx and --tokenizer (or --fake-embed for a plumbing test)")
        emb, emb_name = OnnxEmbedder(a.onnx, a.tokenizer), "onnx-int8"

    t0 = time.time()
    if a.source == "hf":
        qs, docs = source_hf(a.n_queries, a.neg_per_query, a.distractors, a.corpus_stride, rng)
    else:
        if not (a.queries_jsonl and a.corpus_jsonl):
            sys.exit("--source local needs --queries-jsonl and --corpus-jsonl")
        qs, docs = source_local(a.queries_jsonl, a.corpus_jsonl, a.n_queries, a.distractors, rng)
    print(f"loaded {len(qs)} queries, {len(docs)} docs ({time.time()-t0:.1f}s)")

    # sanitize + exact-text dedup (a dup docid maps to the first kept index)
    ids, texts, first = [], [], {}
    docidx = {}
    for did, t in docs.items():
        s = sanitize(t, MAX_CHARS)
        if not s.strip():
            continue
        if s in first:
            docidx[did] = first[s]
            continue
        first[s] = len(ids)
        docidx[did] = len(ids)
        ids.append(did)
        texts.append(s)
    print(f"after sanitize+dedup: {len(texts)} docs ({len(docs) - len(texts)} dropped)")

    qrels, qtexts, qids = {}, [], []
    for qid, qt, pos in qs:
        p = sorted({docidx[d] for d in pos if d in docidx})
        if not p:
            continue
        qrels[len(qtexts)] = p
        qtexts.append(sanitize(qt, MAX_CHARS))
        qids.append(qid)
    print(f"queries with >=1 kept positive: {len(qtexts)}")

    print("embedding corpus ...")
    C = emb([PASSAGE_PREFIX + t for t in texts])
    print("embedding queries ...")
    Q = emb([QUERY_PREFIX + t for t in qtexts])

    os.makedirs(a.out_dir, exist_ok=True)
    np.save(os.path.join(a.out_dir, "corpus.npy"), C)
    np.save(os.path.join(a.out_dir, "queries.npy"), Q)
    meta = {"embedder": emb_name, "docids": ids, "query_ids": qids,
            "qrels": {str(k): v for k, v in qrels.items()},
            "passage_prefix": PASSAGE_PREFIX, "query_prefix": QUERY_PREFIX,
            "max_chars": MAX_CHARS, "source": a.source, "seed": a.seed}
    json.dump(meta, open(os.path.join(a.out_dir, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)
    with open(os.path.join(a.out_dir, "ann_index.jsonl"), "w", encoding="utf-8") as f:
        for did, t, v in zip(ids, texts, C):
            f.write(json.dumps({"sid": clean_field(did, 56), "file": "miracl-th", "npast": len(t), "dim": DIM,
                                "vec": [round(float(x), 6) for x in v]}, ensure_ascii=False) + "\n")
    print(f"wrote {a.out_dir}/ (corpus.npy, queries.npy, meta.json, ann_index.jsonl)  embedder={emb_name}")
    sanity(C, Q, qrels, nrng)


if __name__ == "__main__":
    main()
