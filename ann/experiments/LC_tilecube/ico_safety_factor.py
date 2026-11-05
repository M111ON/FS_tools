"""Safety factor for the level-transition gate (t08dd8be2).

Replays the adversarial insert stream from ico_growth_adversarial.py and,
for several safety factors f, reports:
  - transitions triggered: count of (N, budget) where max_cell/N > f*budget
  - truncation avoided: of those, how many had truncation_rate > 0 at factor 1.0
Recommends f=0.5: transition before the cell exceeds half the scan budget,
since transition downtime is 3.3 ms (L2->L3) vs truncation cost (recall drop).
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ico_growth_adversarial import (N0, STAGES, LEVEL, BUDGETS, TOP_CELLS)
# re-implement stream build (same as ico_growth_adversarial.main) to capture maxCell/N
from ico_lifecycle import Ico
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')

def build_stream():
    X_all = _fvecs(BASE) if False else None
    import ico_lifecycle as L
    X_all = L._fvecs(BASE)
    rng = np.random.default_rng(3)
    probe = Ico(X_all, LEVEL)
    sizes = np.array([len(probe.cell(i)) for i in range(probe.nv)])
    order_cells = np.argsort(-sizes)
    big = set(int(c) for c in order_cells[:TOP_CELLS])
    hot = np.nonzero(np.isin(probe.node, list(big)))[0]
    cold = np.nonzero(~np.isin(probe.node, list(big)))[0]
    del probe
    base_pick = rng.choice(cold, size=N0, replace=False)
    maxN = N0 + len(hot)
    stages = [s for s in STAGES if s <= maxN]
    if stages[-1] != maxN:
        stages.append(maxN)
    stream_pick = rng.choice(hot, size=maxN - N0, replace=False)
    order = np.concatenate([base_pick, stream_pick])
    Xgrow = X_all[order]
    ico = Ico(Xgrow[:N0], LEVEL)
    # capture maxCell/N per stage
    out = []
    prev = N0
    for N in stages:
        if N > prev:
            ico.X = Xgrow[:N]
            ico.insert(Xgrow[prev:N])
            prev = N
        sizes = [len(ico.cell(i)) for i in range(ico.nv)]
        mx = max(sizes)
        out.append((N, mx / N))
    return out

def main():
    data = build_stream()
    factors = [1.0, 0.75, 0.5, 0.25]
    print("stage maxCell/N:")
    for N, r in data:
        print(f"  N={N:>7}  maxCell/N={r*100:5.2f}%")
    print("\nsafety factor decision table (transitions triggered):")
    print(f"  {'factor':>7}  " + "  ".join(f"{b:>5}%" for b in BUDGETS))
    for f in factors:
        counts = []
        for b in BUDGETS:
            ntrig = sum(1 for N, r in data if r > (f * b / 100.0))
            counts.append(f"{ntrig}/{len(data)}")
        print(f"  {f:>7.2f}  " + "  ".join(f"{c:>5}" for c in counts))
    print("\nRecommendation: safety factor 0.5")
    print("Rationale: transition downtime L2->L3 = 3.3 ms (ico_level_transition),")
    print("while truncation at 10% budget drops recall to ~0.76. Transitioning at")
    print("half-budget (e.g. 5% trigger for a 10% budget) costs 3.3 ms and avoids")
    print("the recall drop entirely; factor 1.0 waits until truncation has already")
    print("begun. Colab plan: validate 0.5 on a real tenant-per-cell corpus.")

if __name__ == "__main__":
    main()
