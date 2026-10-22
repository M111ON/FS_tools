"""E4 four-survivor transfer test (SIFT pieces -> real chat corpus).

Read-only on log.jsonl; new code lives in probes/ only.
Lexical similarity = token Jaccard on lowercase alphanumeric tokens
(same proxy as E1 e1_scales.py — required for comparability).

Corpus: kind CHAT, len(text)>=20. Vocab = top-2000 tokens by doc frequency.
Line vectors = L2-normalized token-count dicts (math; implemented dense in
numpy for speed — identical dot products). Voronoi buckets = deterministic
k-means k=64, seed 7, 20 iterations, cosine distance.
Queries = same 500 lines seed 7 as E1 (corpus order identical to E1:
log.jsonl order filtered by kind/len — recoverable, reused exactly).
GT per query = global lexical (Jaccard) top-10 excluding itself, brute force,
tie-break (-sim, idx) exactly as E1.
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
SEED = 7
NQUERY = 500
TOPK = 10
VOCAB = 2000
K = 64
KCOARSE = 8
ITERS = 20


def toks(text):
    return TOK_RE.findall(text.lower())


# ---------- corpus (same order/filter as E1) ----------
texts = []
toks_list = []   # list of token lists (counts)
sets_list = []   # frozensets for Jaccard
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
        texts.append(t)
        tl = toks(t)
        toks_list.append(tl)
        sets_list.append(frozenset(tl))

M = len(texts)
print(f"corpus={M} (kind CHAT, len(text)>=20)")

# ---------- vocab: top-2000 by doc frequency ----------
df = Counter()
for s in sets_list:
    df.update(s)
vocab_terms = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[:VOCAB]
vidx = {t: i for i, (t, _) in enumerate(vocab_terms)}
V = len(vidx)
print(f"vocab={V} (top-{VOCAB} by doc frequency)")

# ---------- vectors: L2-normalized count dicts (dense impl) ----------
X = np.zeros((M, V), dtype=np.float64)
for i, tl in enumerate(toks_list):
    for t in tl:
        j = vidx.get(t)
        if j is not None:
            X[i, j] += 1.0
nrm = np.linalg.norm(X, axis=1, keepdims=True)
nrm[nrm == 0] = 1.0
X /= nrm


def kmeans(Xd, k, seed, iters):
    rng = np.random.default_rng(seed)
    init = rng.choice(Xd.shape[0], size=k, replace=False)
    C = Xd[init].copy()
    cn = np.linalg.norm(C, axis=1, keepdims=True)
    cn[cn == 0] = 1.0
    C /= cn
    assign = np.zeros(Xd.shape[0], dtype=np.int64)
    S = None
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
            # empty cluster: keep old centroid (deterministic)
    dt = time.perf_counter() - t0
    S = Xd @ C.T
    assign = np.argmax(S, axis=1)
    return C, assign, S, dt


t0 = time.perf_counter()
C, assign, S, km_dt = kmeans(X, K, SEED, ITERS)
sizes = np.bincount(assign, minlength=K)
print(f"kmeans k={K} seed={SEED} iters={ITERS} build_s={km_dt:.2f} "
      f"mean_size={sizes.mean():.1f} min={sizes.min()} max={sizes.max()}")

# ---------- coarse k-means k=8 over the 64 centroids ----------
Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-12)
Cc, cassign, _, coarse_dt = kmeans(Cn, KCOARSE, SEED, ITERS)
fine_parent = cassign  # fine bucket -> coarse id
print(f"coarse kmeans k={KCOARSE} over centroids build_s={coarse_dt:.2f}")

# bucket member lists (primary)
buckets = [np.where(assign == c)[0] for c in range(K)]
# overlap: top-2 buckets per line (double lists)
top2 = np.argpartition(S, -2, axis=1)[:, -2:]
# fix order not needed; ensure both present
buckets2 = [[] for _ in range(K)]
for i in range(M):
    for c in top2[i]:
        buckets2[int(c)].append(i)
buckets2 = [np.array(sorted(b), dtype=np.int64) for b in buckets2]
dup_factor = sum(len(b) for b in buckets2) / M
print(f"overlap assign: total_postings={sum(len(b) for b in buckets2)} "
      f"(x{dup_factor:.2f})")

# ---------- queries: same 500 seed-7 as E1 ----------
rng = random.Random(SEED)
qidx = sorted(rng.sample(range(M), min(NQUERY, M)))
Q = len(qidx)
print(f"queries={Q} seed={SEED} (same sampling as E1: sorted rng.sample, "
      f"corpus order identical -> recoverable)")

# ---------- GT: global Jaccard top-10 ----------
print("scoring GT (brute force)...", flush=True)
t0 = time.perf_counter()
GT = []
for qi in qidx:
    qs = sets_list[qi]
    ql = len(qs)
    sc = []
    for j in range(M):
        if j == qi:
            continue
        js = sets_list[j]
        inter = len(qs & js)
        sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
        sc.append((sim, j))
    sc.sort(key=lambda x: (-x[0], x[1]))
    GT.append([j for _, j in sc[:TOPK]])
gt_dt = time.perf_counter() - t0
print(f"GT done in {gt_dt:.1f}s")


def top10_jaccard(qi, cand):
    qs = sets_list[qi]
    ql = len(qs)
    sc = []
    for j in cand:
        if j == qi:
            continue
        js = sets_list[j]
        inter = len(qs & js)
        sim = inter / (ql + len(js) - inter) if (ql or js) else 0.0
        sc.append((sim, int(j)))
    sc.sort(key=lambda x: (-x[0], x[1]))
    return [j for _, j in sc[:TOPK]]


def run_arm(name, route_fn):
    cov = 0.0
    scanned = 0
    t0 = time.perf_counter()
    for n, qi in enumerate(qidx):
        cand = route_fn(qi)
        got = top10_jaccard(qi, cand)
        gt = set(GT[n])
        cov += len(set(got) & gt) / TOPK
        scanned += len(cand)
    dt = time.perf_counter() - t0
    return cov / Q, scanned / Q / M * 100.0, dt / Q * 1000.0


def flat_route(Cn_, qi, nbuckets):
    s = X[qi] @ C.T
    top = np.argpartition(s, -nbuckets)[-nbuckets:]
    cand = np.unique(np.concatenate([buckets[int(c)] for c in top]))
    # exclude self at score time; keep scan count as visited-minus-self-safe:
    return cand


def flat2_route(qi, nbuckets):
    s = X[qi] @ C.T
    top = np.argpartition(s, -nbuckets)[-nbuckets:]
    cand = np.unique(np.concatenate([buckets2[int(c)] for c in top]))
    return cand


results = {}

# (a) FLAT C in {4,8,16}
for Cb in (4, 8, 16):
    cov, scan, msq = run_arm(f"a{ Cb}",
                             lambda qi, Cb=Cb: flat_route(C, qi, Cb))
    results[f"(a) FLAT C={Cb}"] = (cov, scan, msq)

# (b) FLAT+OVERLAP route top-C/2 over double lists
for Cb, Rb in ((4, 2), (8, 4), (16, 8)):
    cov, scan, msq = run_arm(f"b{Cb}",
                             lambda qi, Rb=Rb: flat2_route(qi, Rb))
    results[f"(b) OVERLAP C={Cb}->top{Rb}"] = (cov, scan, msq)

# (c) FLAT+CUTOFF: C=16 then top 25% visited by centroid distance, single round
def cutoff_route(qi):
    s = X[qi] @ C.T
    top = np.argpartition(s, -16)[-16:]
    top = set(int(c) for c in top)
    cand = np.unique(np.concatenate([buckets[c] for c in top]))
    # proxy score per member = its primary bucket centroid cosine
    prox = np.array([s[assign[int(j)]] for j in cand])
    order = np.argsort(-prox, kind="stable")
    keep = max(1, int(len(cand) * 0.25))
    return np.sort(cand[order[:keep]])

cov, scan, msq = run_arm("c", cutoff_route)
results["(c) CUTOFF C=16 top25%"] = (cov, scan, msq)

# (d) HIERARCHICAL: top-2 coarse -> top-C fine within
def hier_route(qi, Cf):
    sc = X[qi] @ Cc.T
    topc = set(int(c) for c in np.argpartition(sc, -2)[-2:])
    fine_ids = [c for c in range(K) if int(fine_parent[c]) in topc]
    sf = np.array([float(X[qi] @ C[c]) for c in fine_ids])
    Cf_eff = min(Cf, len(fine_ids))
    order = np.argpartition(sf, -Cf_eff)[-Cf_eff:]
    chosen = [fine_ids[int(o)] for o in order]
    return np.unique(np.concatenate([buckets[c] for c in chosen]))

for Cf in (8, 16):
    cov, scan, msq = run_arm(f"d{Cf}",
                             lambda qi, Cf=Cf: hier_route(qi, Cf))
    results[f"(d) HIER top2coarse top{Cf}fine"] = (cov, scan, msq)

print("\narm | coverage(GT top-10) | scan% | ms/q")
for k, (cov, scan, msq) in results.items():
    print(f"{k} | {cov:.4f} | {scan:.2f}% | {msq:.2f}ms")
print(f"\nkmeans build_s={km_dt:.2f} coarse_build_s={coarse_dt:.2f} "
      f"mean/min/max={sizes.mean():.1f}/{sizes.min()}/{sizes.max()} iters={ITERS}")
print("E1 baseline: 0.078@0.95 / 0.170@3.8 / 0.330@13.4 / 0.466@27.4")
print("GATE: any arm coverage>=0.8 at scan<=25% ?",
      "PASS" if any(c >= 0.8 and s <= 25 for c, s, _ in results.values())
      else "NEGATIVE")
