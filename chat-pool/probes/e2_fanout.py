"""E2 fan-out retrieval experiment (read-only over log.jsonl; no tuning).

GT choice: prev graph is one global chain (branching=0), so per spec
GT = time cohorts (same app, |dt| <= 900s). NOTE: CHAT time span is only
~28s, so each cohort ~= all same-app CHAT lines (documented degeneracy).

Arms per standing point q (corpus = kind CHAT, self excluded everywhere):
  spatial(d): corpus lines in cells with torus-Chebyshev dist <= d from q's cell
              (col=rkey%144, row=rkey//144; torus wrap on 144x144)
  lexical(K): top-K corpus lines by token-Jaccard (lowercase alphanumeric
              tokens), K = len(spatial(d)) of the same query, ties by file order
  union:      spatial(d) | lexical(K)
Metric: recall of GT-other-members; overlap = Jaccard(spatial, lexical).
"""
import json, re

LOG = "log.jsonl"
N, W, DT = 144, 144, 900.0
DS = (1, 2, 4)
TOK = re.compile(r"[a-z0-9]+")

rows = [json.loads(l) for l in open(LOG, encoding="utf-8")]
h2i = {l["hash"]: i for i, l in enumerate(rows)}
refs = [l.get("prev", "") for l in rows]
from collections import Counter
refc = Counter(p for p in refs if p)
branch = sum(1 for c in refc.values() if c > 1)
print(f"prev-graph: n={len(rows)} nonempty_prev={sum(1 for p in refs if p)} "
      f"dangling={sum(1 for p in refs if p and p not in h2i)} "
      f"distinct_prev={len(refc)} branching(>1 referrer)={branch} "
      f"max_fanin={max(refc.values(), default=0)}")
print(f"chat_span_s={max(l['t'] for l in rows if l.get('kind')=='CHAT') - min(l['t'] for l in rows if l.get('kind')=='CHAT'):.1f}")

chat = [l for l in rows if l.get("kind") == "CHAT"]
ci = [i for i, l in enumerate(rows) if l.get("kind") == "CHAT"]
C = len(chat)
cell = [(l.get("rkey", 0) % N, (l.get("rkey", 0) // N) % W) for l in chat]
toks = [set(TOK.findall((l.get("text", "") or "").lower())) for l in chat]
apps = [l.get("app") for l in chat]
ts = [l["t"] for l in chat]

M = min(200, C)
qs = sorted(set(round(i * (C - 1) / (M - 1)) for i in range(M))) if M > 1 else [0]

def cheb(a, b):
    dx = abs(a[0] - b[0]); dx = min(dx, N - dx)
    dy = abs(a[1] - b[1]); dy = min(dy, W - dy)
    return max(dx, dy)

print(f"C={C} standing_points_sampled={len(qs)}")
res = {d: {"sp": [], "lx": [], "un": [], "ov": [], "K": []} for d in DS}
used = skipped = 0
for q in qs:
    gt = {j for j in range(C) if j != q and apps[j] == apps[q] and abs(ts[j] - ts[q]) <= DT}
    if not gt:
        skipped += 1
        continue
    used += 1
    jq = toks[q]
    for d in DS:
        sp = {j for j in range(C) if j != q and cheb(cell[j], cell[q]) <= d}
        K = len(sp)
        ju = jq | set()
        scored = []
        for j in range(C):
            if j == q:
                continue
            tj = toks[j]
            inter = len(jq & tj); union = len(jq | tj)
            scored.append((inter / union if union else 0.0, j))
        scored.sort(key=lambda x: (-x[0], x[1]))
        lx = {j for _, j in scored[:K]}
        un = sp | lx
        r = res[d]
        r["sp"].append(len(sp & gt) / len(gt))
        r["lx"].append(len(lx & gt) / len(gt))
        r["un"].append(len(un & gt) / len(gt))
        r["ov"].append(len(sp & lx) / len(sp | lx) if (sp | lx) else 0.0)
        r["K"].append(K)

print(f"effective_points={used} skipped_empty_GT={skipped}")
print(f"{'d/K~':>6} {'n':>4} {'meanK':>7} {'sp_rec':>7} {'lx_rec':>7} {'un_rec':>7} {'overlap':>8}")
for d in DS:
    r = res[d]
    n = len(r["sp"])
    print(f"{d:>6} {n:>4} {sum(r['K'])/n:>7.1f} "
          f"{sum(r['sp'])/n:>7.4f} {sum(r['lx'])/n:>7.4f} "
          f"{sum(r['un'])/n:>7.4f} {sum(r['ov'])/n:>8.4f}")
