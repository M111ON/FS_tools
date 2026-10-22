"""E5 marathon scoreboard: replay over time slices, recall(t) + rebuild cost.

Read-only on log.jsonl; new code lives in probes/ only.
Lexical proxy = token Jaccard on lowercase alphanumeric tokens
(same as E1/E4).

Corpus: kind CHAT, len(text)>=20, sorted by t asc, split into 10
sequential slices (~equal counts).
Per slice: vocab = top-2000 tokens of THAT slice by doc frequency
(tie-break term asc); vectors = L2-normalized token counts (dense
numpy, float64); deterministic k-means k=64, seed 7, 20 iters,
cosine (dot-product argmax); empty cluster keeps old centroid.
Queries per slice: 150 lines, random.Random(7000+s).sample, sorted.
GT per query = global Jaccard top-10 among SLICE s members excl. self,
brute force, tie-break (-sim, slice-local idx).
Arms (top-4 flat routing + exact Jaccard rerank, recall@10 vs GT):
  (a) FRESH: slice-s centroids, slice-s vocab vectors.
  (b) STALE-1 (s>=2): slice s-1 centroids; slice-s members projected
      onto vocab_{s-1} (tokens outside vocab ignored, renormalized),
      assigned to nearest s-1 centroid; queries routed in that space.
  (c) HNSW (faiss): IndexHNSWFlat on dense 2000-dim L2-normalized
      float32 slice vectors (M16, efConstruction 40, efSearch 64);
      search top-11, drop self, take 10; recall@10 vs same GT.
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


def toks(text):
    return TOK_RE.findall(text.lower())


# ---------- corpus sorted by t ----------
recs = []  # (t, text, token-list, token-set)
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
        recs.append((float(o["t"]), t, tl, frozenset(tl)))
recs.sort(key=lambda r: r[0])
M = len(recs)
print(f"corpus={M} (kind CHAT, len(text)>=20, sorted by t)")

# ---------- 10 sequential slices, ~equal counts ----------
base, rem = divmod(M, NSLICE)
bounds, off = [], 0
for s in range(NSLICE):
    n = base + (1 if s < rem else 0)
    bounds.append((off, off + n))
    off += n
print("slice_sizes=" + ",".join(str(b - a) for a, b in bounds))


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
    return C, assign, S, dt


def top10_jaccard(qi, cand, sets_list):
    qs = sets_list[qi]
    ql = len(qs)
    sc = []
    for j in cand:
        j = int(j)
        if j == qi:
            continue
        js = sets_list[j]
        inter = len(qs & js)
        sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
        sc.append((sim, j))
    sc.sort(key=lambda x: (-x[0], x[1]))
    return [j for _, j in sc[:TOPK]]


try:
    import faiss

    HAS_FAISS = True
except Exception as e:
    HAS_FAISS = False
    FAISS_ERR = str(e)[:120]
    print(f"HNSW arm SKIPPED: import faiss failed ({FAISS_ERR})")

slices = []
for s in range(NSLICE):
    a, b = bounds[s]
    chunk = recs[a:b]
    tl = [r[2] for r in chunk]
    st = [r[3] for r in chunk]
    vidx = build_vocab(st)
    X = mat(tl, vidx)
    C, assign, S, dt = kmeans(X, K, SEED, ITERS)
    buckets = [np.where(assign == c)[0] for c in range(K)]
    rng = random.Random(7000 + (s + 1))
    qidx = sorted(rng.sample(range(len(chunk)), min(NQUERY, len(chunk))))
    # GT: brute-force Jaccard top-10 within slice
    GT = []
    for qi in qidx:
        qs = st[qi]
        ql = len(qs)
        sc = []
        for j in range(len(chunk)):
            if j == qi:
                continue
            js = st[j]
            inter = len(qs & js)
            sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
            sc.append((sim, j))
        sc.sort(key=lambda x: (-x[0], x[1]))
        GT.append([j for _, j in sc[:TOPK]])
    slices.append(dict(tl=tl, st=st, vidx=vidx, X=X, C=C, assign=assign,
                       buckets=buckets, qidx=qidx, GT=GT, build=dt))
    print(f"slice {s + 1}: n={len(chunk)} vocab={len(vidx)} build_s={dt:.2f}", flush=True)

# ---------- arms ----------
fresh, stale, hnsw_r, hnsw_b = [], [], [], []
for s in range(NSLICE):
    d = slices[s]
    buckets, qidx, GT, st = d["buckets"], d["qidx"], d["GT"], d["st"]
    # (a) FRESH
    Sq = d["X"][qidx] @ d["C"].T
    fr = 0.0
    for n, qi in enumerate(qidx):
        top = np.argpartition(Sq[n], -4)[-4:]
        cand = np.unique(np.concatenate([buckets[int(c)] for c in top]))
        fr += len(set(top10_jaccard(qi, cand, st)) & set(GT[n])) / TOPK
    fresh.append(fr / len(qidx))
    # (b) STALE-1
    if s == 0:
        stale.append(None)
    else:
        p = slices[s - 1]
        Xp = mat(d["tl"], p["vidx"])  # slice-s members in vocab_{s-1}
        Ap = np.argmax(Xp @ p["C"].T, axis=1)  # into s-1 buckets
        Bp = [np.where(Ap == c)[0] for c in range(K)]
        Sqp = Xp[qidx] @ p["C"].T
        sr = 0.0
        for n, qi in enumerate(qidx):
            top = np.argpartition(Sqp[n], -4)[-4:]
            cand = np.unique(np.concatenate([Bp[int(c)] for c in top]))
            sr += len(set(top10_jaccard(qi, cand, st)) & set(GT[n])) / TOPK
        stale.append(sr / len(qidx))
    # (c) HNSW
    if HAS_FAISS:
        d_ = d["X"].shape[1]
        Xf = np.ascontiguousarray(d["X"], dtype=np.float32)
        idx = faiss.IndexHNSWFlat(d_, 16)
        idx.hnsw.efConstruction = 40
        idx.hnsw.efSearch = 64
        t0 = time.perf_counter()
        idx.add(Xf)
        bt = time.perf_counter() - t0
        D, I = idx.search(Xf[qidx], TOPK + 1)
        hr = 0.0
        for n, qi in enumerate(qidx):
            got = [int(j) for j in I[n] if int(j) != qi][:TOPK]
            hr += len(set(got) & set(GT[n])) / TOPK
        hnsw_r.append(hr / len(qidx))
        hnsw_b.append(bt)
    else:
        hnsw_r.append(None)
        hnsw_b.append(None)

print("\nslice | n | fresh | stale-1 | hnsw | our_build_s | hnsw_build_s")
for s in range(NSLICE):
    st_r = f"{stale[s]:.4f}" if stale[s] is not None else "n/a"
    h_r = f"{hnsw_r[s]:.4f}" if hnsw_r[s] is not None else "SKIP"
    h_b = f"{hnsw_b[s]:.2f}" if hnsw_b[s] is not None else "n/a"
    a, b = bounds[s]
    print(f"{s + 1:>5} | {b - a} | {fresh[s]:.4f} | {st_r} | {h_r} "
          f"| {slices[s]['build']:.2f} | {h_b}")

mf = float(np.mean(fresh))
ms = float(np.mean([v for v in stale if v is not None]))
tot = float(sum(d["build"] for d in slices))
print(f"\nmean_fresh={mf:.4f} mean_stale1={ms:.4f} gap(stale-fresh)={ms - mf:+.4f}")
print(f"total_our_rebuild_10slices={tot:.2f}s")
if HAS_FAISS:
    print(f"total_hnsw_build_10slices={sum(hnsw_b):.2f}s "
          f"(mean_hnsw_recall={np.mean(hnsw_r):.4f})")
else:
    print("total_hnsw_build_10slices=n/a (faiss unavailable; SIFT-scale 356s "
          "figure is a different-scale reference, NOT comparable)")
gate = (ms >= mf - 0.05) and (tot <= 60.0)
print(f"GATE marathon-VIABLE (stale>=fresh-0.05 AND total<=60s): "
      f"{'PASS' if gate else 'FAIL'}")
