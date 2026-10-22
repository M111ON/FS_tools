"""E1 W-slices: at which window scale does the lexical answer cloud live?

Read-only on log.jsonl. Torus (wrap-around) windows: inside iff
torus_dx < side/2 and torus_dy < side/2 (side=144 -> full map).
Similarity = token Jaccard over lowercase alphanumeric tokens
(openly lexical proxy; no embeddings available).
"""
import json
import random
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG = BASE / "log.jsonl"
N = 144
SIDES = [12, 24, 48, 72, 144]
NQUERY = 500
SEED = 7
TOPK = 10
TOK_RE = re.compile(r"[a-z0-9]+")


def toks(text):
    return frozenset(TOK_RE.findall(text.lower()))


def cell(rkey):
    r = int(rkey) % (N * N)
    return (r % N, r // N)  # col, row


corpus = []  # (tokens, col, row)
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
        c, r = cell(o["rkey"])
        corpus.append((toks(t), c, r))

M = len(corpus)
print(f"corpus={M} (kind CHAT, len(text)>=20)")
HALF = [s / 2 if s < N else float("inf") for s in SIDES]  # 144 -> full map

rng = random.Random(SEED)
qidx = sorted(rng.sample(range(M), min(NQUERY, M)))
print(f"queries={len(qidx)} seed={SEED} windows={SIDES} torus=True")

cov_sum = [0.0] * len(SIDES)
mem_sum = [0] * len(SIDES)
per_q_cov = []

for qi in qidx:
    qt, qc, qr = corpus[qi]
    qlen = len(qt)
    scored = []  # (sim, idx, dx, dy)
    mem = [0] * len(SIDES)
    for j, (jt, jc, jr) in enumerate(corpus):
        if j == qi:
            continue
        dx = abs(jc - qc)
        if dx > N - dx:
            dx = N - dx
        dy = abs(jr - qr)
        if dy > N - dy:
            dy = N - dy
        for w, h in enumerate(HALF):
            if dx < h and dy < h:
                mem[w] += 1
        inter = len(qt & jt)
        sim = inter / (qlen + len(jt) - inter) if (qlen or jt) else 0.0
        if inter:
            scored.append((sim, j, dx, dy))
        elif len(scored) < 50000:
            scored.append((0.0, j, dx, dy))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:TOPK]
    qc_cov = []
    for w, h in enumerate(HALF):
        inside = sum(1 for _, _, dx, dy in top if dx < h and dy < h)
        c = inside / TOPK
        cov_sum[w] += c
        mem_sum[w] += mem[w]
        qc_cov.append(c)
    per_q_cov.append(qc_cov)

Q = len(qidx)
print("\nside | coverage(mean top-10 inside) | members(mean corpus in window) | frac_corpus")
for w, s in enumerate(SIDES):
    cov = cov_sum[w] / Q
    mm = mem_sum[w] / Q
    print(f"{s:>4} | {cov:.4f} | {mm:8.1f} | {mm/M:.4f}")

smallest08 = next((s for w, s in enumerate(SIDES) if cov_sum[w] / Q >= 0.8), None)
smallest10 = next((s for w, s in enumerate(SIDES) if cov_sum[w] / Q >= 1.0), None)
print(f"\nsmallest_window_cov>=0.8: {smallest08}")
print(f"smallest_window_cov>=1.0: {smallest10}")
ok = next((s for w, s in enumerate(SIDES)
           if s < 144 and cov_sum[w] / Q >= 0.8 and (mem_sum[w] / Q) / M <= 0.25), None)
print(f"GATE (window<144, cov>=0.8, frac<=0.25): "
      f"{'CONFIRMED @side=' + str(ok) if ok is not None else 'NEGATIVE — answers are map-global'}")
