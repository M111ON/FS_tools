"""phase_b_gate.py — does a data-independent address route as well as k-means?

Gate question (2026-10-30): on a real corpus, is recall unchanged when the store
grows, while the address stays put and nothing is re-embedded?

Two address schemes over the same real vectors:
  A  k-means anchors — what the repo does now (anch_assign -> bucket -> face).
     Recall is good, but centroids are a function of the whole corpus, so adding
     data re-clusters and every address moves.
  B  pure projection chain — fixed seeded directions, sign -> face -> letter.
     The address is a function of the vector alone, so growth cannot move it.
     The question is whether routing quality survives that.

Data: I:/tools/ann/data/memcore/ann_index.jsonl — 63,915 real 384-dim rows
(multilingual-e5-small) already on disk. Nothing is embedded here.

Multi-probe is by bucket centroid similarity for BOTH methods, so the only
difference under test is how the address is derived.

Run: python phase_b_gate.py [n_rows] [n_queries]
"""
import json
import sys

import numpy as np

INDEX = r"I:\tools\ann\data\memcore\ann_index.jsonl"
K = 64          # buckets
LEVELS = 4      # chain levels
TOPK = 10       # recall@10
PROBES = 3      # buckets searched per query


def load(n_rows):
    vecs = []
    with open(INDEX, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i % 2:                     # every other row keeps it quick
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            v = o.get("vec")
            if not v or len(v) != 384:
                continue
            vecs.append(v)
            if len(vecs) >= n_rows:
                break
    X = np.asarray(vecs, dtype=np.float32)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return X


def kmeans(X, k, iters=12, seed=7):
    rng = np.random.default_rng(seed)
    C = X[rng.choice(len(X), k, replace=False)].copy()
    for _ in range(iters):
        a = (X @ C.T).argmax(axis=1)
        for j in range(k):
            m = a == j
            if m.any():
                C[j] = X[m].mean(axis=0)
        C /= (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    return C, (X @ C.T).argmax(axis=1)


def chain_address(X, levels=LEVELS, seed=11):
    """Pure: fixed seeded projections -> 4 sign bits -> one digit per level."""
    rng = np.random.default_rng(seed)
    paths = np.zeros((len(X), levels), dtype=np.int32)
    for L in range(levels):
        P = rng.standard_normal((4, X.shape[1])).astype(np.float32)
        S = (X @ P.T) > 0
        face = S[:, 0].astype(np.int32) | (S[:, 1].astype(np.int32) << 1) \
             | (S[:, 2].astype(np.int32) << 2)
        paths[:, L] = face * 2 + S[:, 3].astype(np.int32)
    return paths


def path_bucket(paths, k):
    """Fold the path to k buckets with a multiplicative hash.

    NOT `v % k`: the path is base-16, so `% 64` keeps only the last level's low
    bits (16^2 and above vanish mod 64) and the buckets come out wildly skewed —
    measured 20/64 used with one bucket holding 33.8%. The Knuth multiplier
    mixes every level into every bit, which is what makes occupancy flat.
    """
    v = np.zeros(len(paths), dtype=np.uint64)
    for L in range(paths.shape[1]):
        v = v * np.uint64(16) + paths[:, L].astype(np.uint64)
    v = v * np.uint64(2654435761)          # Knuth multiplicative hash
    return (v >> np.uint64(16)) % np.uint64(k)


def centroids_of(X, bucket_of, k):
    C = np.zeros((k, X.shape[1]), dtype=np.float32)
    for j in range(k):
        m = bucket_of == j
        if m.any():
            C[j] = X[m].mean(axis=0)
    n = np.linalg.norm(C, axis=1, keepdims=True)
    return C / (n + 1e-9)


def recall_at(X, bucket_of, C, qidx, truth, probes=PROBES):
    k = len(C)
    order = np.argsort(bucket_of, kind="stable")
    srt = bucket_of[order]
    bounds = np.searchsorted(srt, np.arange(k + 1))
    hit = total = scanned = 0
    for qi in qidx:
        q = X[qi]
        near = np.argsort(-(C @ q))[:probes]      # semantic multi-probe
        rows = [order[bounds[b]:bounds[b + 1]] for b in near
                if bounds[b + 1] > bounds[b]]
        if not rows:
            continue
        rows = np.concatenate(rows)
        scanned += len(rows)
        sims = X[rows] @ q
        top = rows[np.argsort(-sims)[:TOPK]]
        hit += len(set(top.tolist()) & set(truth[qi].tolist()))
        total += len(truth[qi])
    return hit / max(total, 1), scanned / max(len(qidx) * len(X), 1)


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    n_q = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    print(f"  {N} vectors, dim {X.shape[1]}", flush=True)

    rng = np.random.default_rng(3)
    qidx = rng.choice(N, min(n_q, N), replace=False).tolist()

    print("brute-force ground truth ...", flush=True)
    truth = {qi: np.argsort(-(X @ X[qi]))[:TOPK] for qi in qidx}

    print("\n--- A: k-means anchors (current) ---", flush=True)
    Ca, ka = kmeans(X, K)
    rec_a, frac_a = recall_at(X, ka, Ca, qidx, truth)
    occ_a = np.bincount(ka, minlength=K)
    print(f"  recall@{TOPK} = {rec_a:.4f}   scanned {frac_a:.1%}"
          f"   buckets {int((occ_a>0).sum())}/{K}"
          f"   largest {occ_a.max()} ({occ_a.max()/N:.1%})", flush=True)

    print("\n--- B: pure projection chain ---", flush=True)
    kb = path_bucket(chain_address(X), K)
    Cb = centroids_of(X, kb, K)
    rec_b, frac_b = recall_at(X, kb, Cb, qidx, truth)
    occ_b = np.bincount(kb, minlength=K)
    print(f"  recall@{TOPK} = {rec_b:.4f}   scanned {frac_b:.1%}"
          f"   buckets {int((occ_b>0).sum())}/{K}"
          f"   largest {occ_b.max()} ({occ_b.max()/N:.1%})", flush=True)

    print("\n--- C: frozen k-means (learn once, append only) ---", flush=True)
    keep = N - N // 5
    Cc, _ = kmeans(X[:keep], K, seed=7)               # trained on the old corpus
    kc = (X @ Cc.T).argmax(axis=1)                    # new rows join, centroids stay
    rec_c, frac_c = recall_at(X, kc, Cc, qidx, truth)
    occ_c = np.bincount(kc, minlength=K)
    print(f"  recall@{TOPK} = {rec_c:.4f}   scanned {frac_c:.1%}"
          f"   buckets {int((occ_c>0).sum())}/{K}"
          f"   largest {occ_c.max()} ({occ_c.max()/N:.1%})", flush=True)

    print("\n--- address stability when 20% new rows arrive ---", flush=True)
    Ca_old, ka_old = kmeans(X[:keep], K, seed=7)     # trained on the old corpus
    Ca_new, ka_new = kmeans(X, K, seed=7)            # retrained on the grown corpus
    moved_a = int((ka_new[:keep] != ka_old).sum())
    print(f"  A k-means : {moved_a}/{keep} old rows changed bucket "
          f"({moved_a/keep:.1%})", flush=True)

    kb2 = path_bucket(chain_address(X), K)
    moved_b = int((kb2[:keep] != kb[:keep]).sum())
    print(f"  B chain   : {moved_b}/{keep} old rows changed bucket "
          f"({moved_b/keep:.1%})  [pure function: 0 by construction]", flush=True)
    print(f"  C frozen  : 0/{keep} old rows changed bucket (0.0%)  "
          f"[centroids never move: 0 by construction]", flush=True)

    print("\n=== gate ===")
    print(f"  recall      A={rec_a:.4f} (scan {frac_a:.1%})  "
          f"B={rec_b:.4f} (scan {frac_b:.1%})  "
          f"C={rec_c:.4f} (scan {frac_c:.1%})")
    print(f"  stability   A={1-moved_a/keep:.4f}  B=1.0000  C=1.0000")


if __name__ == "__main__":
    main()
