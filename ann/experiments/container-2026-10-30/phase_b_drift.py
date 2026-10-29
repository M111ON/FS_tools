"""phase_b_drift.py — how far can the frozen partition go before it must re-learn?

C (frozen k-means) passed the gate at 80/20. This sweeps the split: train on the
first `keep` fraction, append the rest, and watch recall. The number where recall
falls off is the "re-learn" point — the only time the design is allowed to pay.

Run: python phase_b_drift.py [n_rows] [n_queries]
"""
import sys

import numpy as np

from phase_b_gate import K, TOPK, centroids_of, kmeans, load, recall_at


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    n_q = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    rng = np.random.default_rng(3)
    qidx = rng.choice(N, min(n_q, N), replace=False).tolist()
    truth = {qi: np.argsort(-(X @ X[qi]))[:TOPK] for qi in qidx}

    print(f"{N} vectors, {len(qidx)} queries\n", flush=True)
    print(f"{'keep':>6} {'new':>6} {'recall@10':>10} {'scanned':>8} "
          f"{'largest':>8}  method", flush=True)
    print("-" * 62, flush=True)

    for keep_frac in (1.0, 0.9, 0.8, 0.6, 0.4):
        keep = int(N * keep_frac)
        C, _ = kmeans(X[:keep], K, seed=7)
        kb = (X @ C.T).argmax(axis=1)
        rec, frac = recall_at(X, kb, C, qidx, truth)
        occ = np.bincount(kb, minlength=K)
        tag = "frozen (no re-learn)" if keep_frac < 1.0 else "trained on all"
        print(f"{keep:>6} {N-keep:>6} {rec:>10.4f} {frac:>7.1%} "
              f"{occ.max()/N:>7.1%}  {tag}", flush=True)

    print("\nreference: retrained on the full set = 0.7750 "
          "(but 99% of addresses move)", flush=True)


if __name__ == "__main__":
    main()
