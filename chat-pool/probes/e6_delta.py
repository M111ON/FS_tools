"""E6 delta-merge vs fresh-regrow (jet_bridge-as-delta-index / ribcage-merge).

Read-only on log.jsonl; new code lives in probes/ only.
Lexical proxy = token Jaccard on lowercase alphanumeric tokens
(same as E1/E4/E5).

Corpus: kind CHAT, len(text)>=20, sorted by t asc, 10 sequential slices.
Triples s in 1..8: BASE = k-means k=64 (seed 7, 20 iters) on slice s
(vocab top-2000 of slice s); DELTA = slice s+1 as FLAT exact list
(no index, brute-force lexical at query time); QUERIES = 150 fixed-seed
lines (random.Random(9000+s)) from slice s+2.
GT per query = lexical top-10 among members of slices s AND s+1
(base+delta visible pool), brute force, tie-break (-sim, idx).
Arms (top-4 flat routing + exact Jaccard rerank, recall@10 vs GT):
  (a) BASE-ONLY (stale): top-4 base buckets, score base members only.
  (b) BASE+DELTA: top-4 base buckets + ALL delta lines, merged top-10.
  (c) FRESH-REGROW: k-means k=64 on slices s AND s+1 combined
      (vocab top-2000 of combined), top-4 routing, score members.
"""
import json
import random
import re
import time
from collections import Counter
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
LOG = BASE / "log.jsonl"
TOK_RE = re.compile(r"[a-z0-9]+")
NSLICE = 10
NQUERY = 150
TOPK = 10
VOCAB = 2000
K = 64
ITERS = 20
SEED = 7
NBUCK = 4


def toks(text):
    return TOK_RE.findall(text.lower())


recs = []  # (t, tok-list, tok-set)
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("kind") != "CHAT":
            continue
        t = o.get("text") or ""
        if len(t) < 20:
            continue
        tl = toks(t)
        recs.append((float(o["t"]), tl, frozenset(tl)))
recs.sort(key=lambda r: r[0])
M = len(recs)
print(f"corpus={M} (kind CHAT, len(text)>=20, sorted by t)")

base_n, rem = divmod(M, NSLICE)
bounds, off = [], 0
for s in range(NSLICE):
    n = base_n + (1 if s < rem else 0)
    bounds.append((off, off + n))
    off += n
print("slice_sizes=" + ",".join(str(b - a) for a, b in bounds))

slices = []
for s in range(NSLICE):
    a, b = bounds[s]
    chunk = recs[a:b]
    slices.append(dict(tl=[r[1] for r in chunk], st=[r[2] for r in chunk]))
    assert len(chunk) > 0


def build_vocab(sets_list):
    df = Counter()
    for s in sets_list:
        df.update(s)
    terms = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[:VOCAB]
    return {t: i for i, (t, _) in enumerate(terms)}


def mat(toks_lists, vidx):
    V = len(vidx)
    X = np.zeros((len(toks_lists), V), dtype=np.float64)
    for i, tl in enumerate(toks_lists):
        for t in tl:
            j = vidx.get(t)
            if j is not None:
                X[i, j] += 1.0
    nrm = np.linalg.norm(X, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    return X / nrm


def kmeans(Xd, k, seed, iters):
    rng = np.random.default_rng(seed)
    init = rng.choice(Xd.shape[0], size=k, replace=False)
    C = Xd[init].copy()
    cn = np.linalg.norm(C, axis=1, keepdims=True)
    cn[cn == 0] = 1.0
    C /= cn
    assign = np.zeros(Xd.shape[0], dtype=np.int64)
    t0 = time.perf_counter()
    for _ in range(iters):
        S = Xd @ C.T
        assign = np.argmax(S, axis=1)
        for c in range(k):
            m = assign == c
            if np.any(m):
                v = Xd[m].mean(axis=0)
                n = np.linalg.norm(v)
                C[c] = v / n if n > 0 else v
    dt = time.perf_counter() - t0
    S = Xd @ C.T
    assign = np.argmax(S, axis=1)
    return C, assign, dt


def top10_jaccard(qset, cand, pool_sets):
    ql = len(qset)
    sc = []
    for j in cand:
        j = int(j)
        js = pool_sets[j]
        inter = len(qset & js)
        sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
        sc.append((sim, j))
    sc.sort(key=lambda x: (-x[0], x[1]))
    return [j for _, j in sc[:TOPK]]


rows = []
for s in range(1, 9):  # triples 1..8; slices s,s+1,s+2 (1-indexed)
    bi, di, qi = s - 1, s, s + 1
    b, d, q = slices[bi], slices[di], slices[qi]
    nb, nd, nq = len(b["tl"]), len(d["tl"]), len(q["tl"])
    pool_sets = b["st"] + d["st"]  # global idx: base 0..nb-1, delta nb..
    npool = nb + nd

    # BASE index
    bvidx = build_vocab(b["st"])
    bX = mat(b["tl"], bvidx)
    bC, bassign, bbuild = kmeans(bX, K, SEED, ITERS)
    bbuckets = [np.where(bassign == c)[0] for c in range(K)]

    # DELTA flat list build (cheapest possible: materialize refs)
    t0 = time.perf_counter()
    delta_flat = list(zip(d["tl"], d["st"]))
    dbuild = time.perf_counter() - t0

    # FRESH regrow on combined
    ctl = b["tl"] + d["tl"]
    cst = b["st"] + d["st"]
    cvidx = build_vocab(cst)
    cX = mat(ctl, cvidx)
    cC, cassign, cbuild = kmeans(cX, K, SEED, ITERS)
    cbuckets = [np.where(cassign == c)[0] for c in range(K)]

    # QUERIES: 150 fixed-seed from slice s+2
    rng = random.Random(9000 + s)
    qidx = sorted(rng.sample(range(nq), min(NQUERY, nq)))
    Q = len(qidx)
    qsets = [q["st"][i] for i in qidx]

    # GT: brute-force Jaccard top-10 over base+delta pool
    GT = []
    for qs in qsets:
        ql = len(qs)
        sc = []
        for j in range(npool):
            js = pool_sets[j]
            inter = len(qs & js)
            sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
            sc.append((sim, j))
        sc.sort(key=lambda x: (-x[0], x[1]))
        GT.append([j for _, j in sc[:TOPK]])

    # query vectors in each space
    qtl = [q["tl"][i] for i in qidx]
    bQ = mat(qtl, bvidx)
    cQ = mat(qtl, cvidx)
    bS = bQ @ bC.T
    cS = cQ @ cC.T
    dall = np.arange(nb, npool)  # all delta global idx

    # (a) BASE-ONLY
    t0 = time.perf_counter()
    ra, sc_base_n = 0.0, 0
    for n in range(Q):
        top = np.argpartition(bS[n], -NBUCK)[-NBUCK:]
        cand = np.unique(np.concatenate([bbuckets[int(c)] for c in top]))
        sc_base_n += len(cand)
        ra += len(set(top10_jaccard(qsets[n], cand, pool_sets)) & set(GT[n])) / TOPK
    msa = (time.perf_counter() - t0) / Q * 1000.0
    ra /= Q

    # (b) BASE+DELTA
    t0 = time.perf_counter()
    rb, sc_b = 0.0, 0
    for n in range(Q):
        top = np.argpartition(bS[n], -NBUCK)[-NBUCK:]
        cand = np.unique(np.concatenate(
            [bbuckets[int(c)] for c in top] + [dall]))
        sc_b += len(cand)
        rb += len(set(top10_jaccard(qsets[n], cand, pool_sets)) & set(GT[n])) / TOPK
    msb = (time.perf_counter() - t0) / Q * 1000.0
    rb /= Q
    scanb = sc_b / Q / npool * 100.0

    # (c) FRESH-REGROW
    t0 = time.perf_counter()
    rc = 0.0
    for n in range(Q):
        top = np.argpartition(cS[n], -NBUCK)[-NBUCK:]
        cand = np.unique(np.concatenate([cbuckets[int(c)] for c in top]))
        rc += len(set(top10_jaccard(qsets[n], cand, pool_sets)) & set(GT[n])) / TOPK
    msc = (time.perf_counter() - t0) / Q * 1000.0
    rc /= Q

    rows.append(dict(s=s, nb=nb, nd=nd, ra=ra, rb=rb, rc=rc, scanb=scanb,
                     msa=msa, msb=msb, msc=msc,
                     bbuild=bbuild, dbuild=dbuild, cbuild=cbuild))
    print(f"triple {s}: nb={nb} nd={nd} nq={nq} Q={Q} "
          f"ra={ra:.4f} rb={rb:.4f} rc={rc:.4f} scanb={scanb:.2f}% "
          f"msa={msa:.2f} msb={msb:.2f} msc={msc:.2f} "
          f"build_b={bbuild:.2f}s d={dbuild:.4f}s fresh={cbuild:.2f}s",
          flush=True)

print("\ns | nb+nd | recall_a | recall_b | recall_c | scan_b% | ms_a | ms_b | ms_c | build_b | build_d | build_fresh")
for r in rows:
    print(f"{r['s']} | {r['nb'] + r['nd']} | {r['ra']:.4f} | {r['rb']:.4f} | "
          f"{r['rc']:.4f} | {r['scanb']:.2f}% | {r['msa']:.2f} | {r['msb']:.2f} | "
          f"{r['msc']:.2f} | {r['bbuild']:.2f}s | {r['dbuild']:.4f}s | {r['cbuild']:.2f}s")

mra = float(np.mean([r["ra"] for r in rows]))
mrb = float(np.mean([r["rb"] for r in rows]))
mrc = float(np.mean([r["rc"] for r in rows]))
mscan = float(np.mean([r["scanb"] for r in rows]))
mmsa = float(np.mean([r["msa"] for r in rows]))
mmsb = float(np.mean([r["msb"] for r in rows]))
mmsc = float(np.mean([r["msc"] for r in rows]))
mb = float(np.mean([r["bbuild"] for r in rows]))
md = float(np.mean([r["dbuild"] for r in rows]))
mf = float(np.mean([r["cbuild"] for r in rows]))
print(f"\nmeans: recall_a={mra:.4f} recall_b={mrb:.4f} recall_c={mrc:.4f} "
      f"scan_b={mscan:.2f}% ms_a={mmsa:.2f} ms_b={mmsb:.2f} ms_c={mmsc:.2f} "
      f"build_base={mb:.2f}s build_delta={md:.6f}s build_fresh={mf:.2f}s")
print(f"quality margin: recall_b - (recall_c - 0.03) = {mrb - (mrc - 0.03):+.4f}")
print(f"cost ratio: build_delta / build_fresh = {md / mf:.4f} "
      f"(gate needs <= 0.20)")
gate = (mrb >= mrc - 0.03) and (md <= 0.20 * mf)
print(f"GATE delta-merge WINS (recall_b>=recall_c-0.03 AND "
      f"delta<=20% fresh): {'PASS' if gate else 'FAIL'}")
