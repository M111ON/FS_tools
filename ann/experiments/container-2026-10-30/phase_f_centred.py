"""phase_f_centred.py — D1: address and routing on the CENTRED vector.

Owner decision 2026-10-31 (D1 = B): subtract a FROZEN mean before deriving the
address, because e5 points ~91.5% in one direction and the raw address space
collapses (measured 5.79% unique).

Two questions this answers with real receipts:

  1. ADDRESS  — how many letters does a unique address need, raw vs centred?
  2. ROUTING  — does centring help or hurt the k-means partition, and how much
                does it move "who is near whom" (the cost of the metric change)?

The mean is frozen on the FIRST 40% of rows, never recomputed, because that is
what production does (freeze at epoch start, append after). Recomputing it is
measured separately as the cost of getting that wrong.

Run: python phase_f_centred.py [n_rows]
"""
import sys

import numpy as np

from phase_b_gate import load, kmeans, centroids_of, recall_at, TOPK

BITS = 6              # 2^6 = 64 codes folded onto 52 letters
ADDR_LEVELS = 12
FREEZE_FRAC = 0.40
SEED_ADDR = 11
SEED_KM = 7
SEED_Q = 3
N_Q = 200
PROBE_SET = (1, 2, 4)


def build_addresses(X, levels=ADDR_LEVELS, seed=SEED_ADDR):
    """12 letters per row from fixed seeded projections (pure, data-independent)."""
    rng = np.random.default_rng(seed)
    A = np.zeros((len(X), levels), dtype=np.int32)
    for L in range(levels):
        P = rng.standard_normal((BITS, X.shape[1])).astype(np.float32)
        S = (X @ P.T) > 0
        code = np.zeros(len(X), dtype=np.int32)
        for b in range(BITS):
            code |= S[:, b].astype(np.int32) << b
        A[:, L] = 1 + (code % 52)
    return A


def compact(A, m):
    return [tuple(a[:m]) + (a[-1],) for a in A]


def addr_report(tag, A):
    N = len(A)
    full = [tuple(a) for a in A]
    counts = {}
    for k in full:
        counts[k] = counts.get(k, 0) + 1
    uniq = len(counts)
    biggest = max(counts.values())
    collided = N - uniq

    # ladder: smallest step at which each address becomes unique
    needed = np.full(N, ADDR_LEVELS, dtype=np.int32)
    remaining = np.arange(N)
    for m in range(1, ADDR_LEVELS):
        keys = compact(A, m)
        c = {}
        for k in keys:
            c[k] = c.get(k, 0) + 1
        still = np.array([c[keys[i]] > 1 for i in remaining])
        newly = remaining[~still]
        needed[newly] = m
        remaining = remaining[still]
        if len(remaining) == 0:
            break
    b1 = int(np.sum(needed == 1)) / N
    print(f"\n[{tag}] {N} rows, {ADDR_LEVELS} letters")
    print(f"  unique full addresses : {uniq:6d}  ({uniq/N:6.2%})")
    print(f"  collided rows         : {collided:6d}")
    print(f"  largest group         : {biggest:6d}")
    print(f"  mean letters/lookup   : {needed.mean()+1:6.2f} of {ADDR_LEVELS}")
    print(f"  answered by b1N4      : {b1:6.2%}")
    return dict(unique=uniq, collided=collided, biggest=biggest,
                mean_letters=float(needed.mean() + 1), b1=b1)


def topk_truth(X, qidx, k=TOPK):
    """Brute-force top-k by cosine, indexed by ROW (phase_b_gate.recall_at
    reads truth[qi], not truth[position in qidx])."""
    Q = X[qidx]
    S = Q @ X.T
    out = [np.empty(0, dtype=np.int64)] * len(X)
    for i, qi in enumerate(qidx):
        out[qi] = np.argsort(-S[i])[:k]
    return out


def route_report(tag, X, qidx, truth, k):
    C, bucket_of = kmeans(X, k, seed=SEED_KM)
    occ = np.bincount(bucket_of, minlength=k) / len(X)
    print(f"\n[{tag}] kmeans K={k}")
    print(f"  occupancy max {occ.max():.1%}  min {occ.min():.1%}  "
          f"ideal {1/k:.1%}  used {(occ > 0).sum()}/{k}")
    rows = []
    for p in PROBE_SET:
        rec, scan = recall_at(X, bucket_of, C, qidx, truth, probes=p)
        rows.append((p, rec, scan))
        print(f"  probes={p}  recall@{TOPK}={rec:.4f}  scan={scan:.1%}")
    return rows


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    print(f"  {N} vectors, dim {X.shape[1]}", flush=True)

    n_freeze = int(N * FREEZE_FRAC)
    mean_frozen = X[:n_freeze].mean(axis=0)
    mean_full = X.mean(axis=0)
    Xc = (X - mean_frozen).astype(np.float32)

    # how much of the signal IS that shared direction?
    nrm = np.linalg.norm(X, axis=1)
    print(f"\n  ||mean||/mean||x|| = "
          f"{np.linalg.norm(mean_frozen)/nrm.mean():.4f}")
    print(f"  mean cos(mean, x)  = "
          f"{float((X @ mean_frozen / (nrm*np.linalg.norm(mean_frozen)+1e-9)).mean()):.4f}")

    print("\n=== 1. ADDRESS ===")
    A_raw = build_addresses(X)
    A_ctr = build_addresses(Xc)
    A_oracle = build_addresses((X - mean_full).astype(np.float32))
    addr_report("raw", A_raw)
    addr_report("centred (frozen mean, 40%)", A_ctr)
    addr_report("centred (oracle mean, 100%)", A_oracle)

    # cost of NOT freezing: recompute the mean after append
    same = sum(1 for a, b in zip(map(tuple, A_ctr), map(tuple, A_oracle)) if a == b)
    print(f"\n  addresses that MOVE if the mean is recomputed on 100%: "
          f"{N-same}/{N} ({(N-same)/N:.2%})")

    print("\n=== 2. ROUTING ===")
    rng = np.random.default_rng(SEED_Q)
    qidx = rng.choice(N, min(N_Q, N), replace=False).tolist()

    truth_raw = topk_truth(X, qidx)
    truth_ctr = topk_truth(Xc, qidx)
    overlap = np.mean([len(set(truth_raw[qi].tolist()) & set(truth_ctr[qi].tolist())) / TOPK
                       for qi in qidx])
    print(f"\n  top-{TOPK} overlap raw vs centred = {overlap:.4f} "
          f"({overlap*TOPK:.2f} of {TOPK} neighbours are the same)")

    k = 26
    route_report("raw", X, qidx, truth_raw, k)
    route_report("centred", Xc, qidx, truth_ctr, k)


if __name__ == "__main__":
    main()
