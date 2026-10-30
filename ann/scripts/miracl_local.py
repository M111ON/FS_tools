"""MIRACL Thai raw files -> the --source local jsonl pair that prep_miracl_th.py reads.

Writes into WORK:
    queries.jsonl  {"query_id","query","positive_docids":[...]}
    corpus.jsonl   {"docid","title","text"}   positives + every STRIDE-th doc

No sanitize here: prep_miracl_th.py applies its own (same code as build_index.py).
"""
import gzip, json, os, sys

WORK = r"C:\Users\Administrator.AVENTADOR\AppData\Local\Temp\opencode\miracl"
RAW = os.path.join(WORK, "raw")
STRIDE = 20          # every 20th corpus doc becomes a distractor candidate
DISTRACTORS = 20000  # stop after this many (plus all positives)

topics_p = os.path.join(RAW, "topics.miracl-v1.0-th-dev.tsv")
qrels_p = os.path.join(RAW, "qrels.miracl-v1.0-th-dev.tsv")
corpus_ps = [os.path.join(RAW, f) for f in ("docs-0.jsonl.gz", "docs-1.jsonl.gz")]

# ---- topics + qrels ----
topics = {}
with open(topics_p, encoding="utf-8") as f:
    for line in f:
        qid, _, text = line.rstrip("\n").partition("\t")
        if text:
            topics[qid] = text

pos = {}
with open(qrels_p, encoding="utf-8") as f:
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or parts[3] != "1":
            continue
        pos.setdefault(parts[0], []).append(parts[2])

qpath = os.path.join(WORK, "queries.jsonl")
nq = 0
with open(qpath, "w", encoding="utf-8") as f:
    for qid, text in topics.items():
        if qid in pos:
            f.write(json.dumps({"query_id": qid, "query": text,
                                "positive_docids": pos[qid]}, ensure_ascii=False) + "\n")
            nq += 1
print("queries with positives: %d -> %s" % (nq, qpath))

# ---- corpus: all positives, then every STRIDE-th doc ----
need = {d for v in pos.values() for d in v}
cpath = os.path.join(WORK, "corpus.jsonl")
seen = scanned = kept_pos = kept_dis = 0
with open(cpath, "w", encoding="utf-8") as out:
    for cp in corpus_ps:
        with gzip.open(cp, "rt", encoding="utf-8") as f:
            for line in f:
                scanned += 1
                r = json.loads(line)
                did = r["docid"]
                if did in need:
                    keep = True
                    kept_pos += 1
                elif scanned % STRIDE == 0 and kept_dis < DISTRACTORS:
                    keep = True
                    kept_dis += 1
                else:
                    keep = False
                if keep:
                    out.write(json.dumps({"docid": did, "title": r.get("title") or "",
                                          "text": r.get("text") or ""},
                                         ensure_ascii=False) + "\n")
                    seen += 1
print("scanned %d docs; kept %d (positives %d / %.1f%%, distractors %d) -> %s"
      % (scanned, seen, kept_pos, 100.0 * kept_pos / max(seen, 1), kept_dis, cpath))
missing = len(need) - kept_pos
print("positives not found in the corpus: %d of %d" % (missing, len(need)))
