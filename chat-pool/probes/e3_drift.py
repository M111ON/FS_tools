"""E3 drift / climate-slide measurement. Read-only on log.jsonl."""
import json, math, statistics
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG = BASE / "log.jsonl"
N = 144
NSLICES = 10
HOTK = 16

def cell(rkey):
    r = int(rkey) % (N * N)
    return (r % N, r // N)  # col, row

def cheb_torus(a, b):
    dx = abs(a[0] - b[0]); dx = min(dx, N - dx)
    dy = abs(a[1] - b[1]); dy = min(dy, N - dy)
    return max(dx, dy)

rows = []
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        rows.append((float(o["t"]), int(o["rkey"])))
rows.sort(key=lambda x: x[0])
total = len(rows)
print(f"total={total} t_min={rows[0][0]:.1f} t_max={rows[-1][0]:.1f} span_days={(rows[-1][0]-rows[0][0])/86400:.2f}")

size = total // NSLICES
slices = []
for i in range(NSLICES):
    lo = i * size
    hi = (i + 1) * size if i < NSLICES - 1 else total
    slices.append(rows[lo:hi])
print(f"slice_sizes={[len(s) for s in slices]}")

occ, hist, ent, hot = [], [], [], []
for s in slices:
    h = {}
    for _, r in s:
        c = cell(r)
        h[c] = h.get(c, 0) + 1
    o = set(h)
    tot = len(s)
    e = -sum((v / tot) * math.log2(v / tot) for v in h.values())
    hk = sorted(h, key=lambda c: (-h[c], c))[:HOTK]
    occ.append(o); hist.append(h); ent.append(e); hot.append(hk)

print("\nentropy=" + ",".join(f"{e:.3f}" for e in ent))
print(f"occupied={[len(o) for o in occ]}")
for i, hk in enumerate(hot):
    print(f"hot{i+1}=" + ",".join(f"{c[0]}+{c[1]*N}({hist[i][c]})" for c in hk))

print("\nchurn t->t+1: jaccard, hot_remain(/16), hot_turnover, med_cheb_drop->nearest")
churn = []
for i in range(NSLICES - 1):
    a, b = occ[i], occ[i + 1]
    j = len(a & b) / len(a | b)
    sa, sb = set(hot[i]), set(hot[i + 1])
    remain = len(sa & sb)
    dropped = list(sa - sb)
    dists = [min(cheb_torus(d, n) for n in sb) for d in dropped] if dropped else [0]
    med = statistics.median(dists)
    churn.append((j, remain, med))
    print(f"{i+1}->{i+2}: jacc={j:.4f} remain={remain}/16 turnover={HOTK-remain} med_cheb={med:.1f} dists={sorted(dists)}")

# staleness: train hot-16 on slice 1
s1 = set(hot[0])
print("\ndecay slice1-hot16 coverage:")
cov = []
for i in range(NSLICES):
    c = sum(1 for _, r in slices[i] if cell(r) in s1) / len(slices[i])
    cov.append(c)
    print(f"slice{i+1}: {c:.4f} ({c*len(slices[i]):.0f}/{len(slices[i])})")
gate = cov[-1] / cov[1] if cov[1] else float("nan")
print(f"GATE: cov10/cov2 = {cov[-1]:.4f}/{cov[1]:.4f} = {gate:.4f} (<0.5 => drift MATTERS)")

# train on slices 1-5 vs slice 10
h = {}
for i in range(5):
    for _, r in slices[i]:
        c = cell(r)
        h[c] = h.get(c, 0) + 1
hot15 = set(sorted(h, key=lambda c: (-h[c], c))[:HOTK])
c10 = sum(1 for _, r in slices[9] if cell(r) in hot15) / len(slices[9])
c6 = sum(1 for _, r in slices[5] if cell(r) in hot15) / len(slices[5])
print(f"\nhalf-history(1-5) hot16 on slice10: {c10:.4f} ; on slice6: {c6:.4f}")
print("hot15=" + ",".join(f"{c[0]}+{c[1]*N}" for c in sorted(hot15)))
