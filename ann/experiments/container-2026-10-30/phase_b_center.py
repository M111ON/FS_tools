"""phase_b_center.py — was B's failure a real limit, or a missing mean subtraction?

phase_b_gate reported pure projection at recall 0.214 against k-means 0.775, and
phase_d found 844 of 20,000 addresses duplicated in a 52^12 space. Both point at
the same suspicion: e5 embeddings sit in a narrow cone, so a random projection is
dominated by the corpus mean and the sign bits carry almost no per-row
information. Centring the vectors before projecting should fix both if that is
the cause.

Run: python phase_b_center.py [n_rows] [n_queries]
"""
import sys

import numpy as np

from phase_b_gate import K, TOPK, centroids_of, chain_address, kmeans, load, \
    path_bucket, recall_at


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    n_q = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    rng = np.random.default_rng(3)
    qidx = rng.choice(N, min(n_q, N), replace=False).tolist()
    truth = {qi: np.argsort(-(X @ X[qi]))[:TOPK] for qi in qidx}

    mu = X.mean(axis=0, keepdims=True)
    Xc = X - mu
    Xc /= (np.linalg.norm(Xc, axis=1, keepdims=True) + 1e-9)

    # how much of a raw vector is just the mean?
    print(f"  ||mean|| / mean||x|| = "
          f"{np.linalg.norm(mu)/np.mean(np.linalg.norm(X,axis=1)):.3f}", flush=True)
    print(f"  cosine(mean, x) median = "
          f"{np.median(X @ mu.T/np.linalg.norm(mu)):.3f}\n", flush=True)

    for tag, V in (("raw", X), ("centred", Xc)):
        print(f"--- B pure projection, {tag} ---", flush=True)
        paths = chain_address(V)
        kb = path_bucket(paths, K)
        Cb = centroids_of(V, kb, K)
        rec, frac = recall_at(V, kb, Cb, qidx, truth)
        occ = np.bincount(kb, minlength=K)
        uniq = len(set(map(tuple, paths)))
        print(f"  recall@{TOPK} = {rec:.4f}  scanned {frac:.1%}  "
              f"buckets {int((occ>0).sum())}/{K}  largest {occ.max()/N:.1%}  "
              f"distinct addresses {uniq}/{N}", flush=True)

    print("\n--- A k-means on centred vectors (reference) ---", flush=True)
    Ca, ka = kmeans(Xc, K)
    rec_a, frac_a = recall_at(Xc, ka, Ca, qidx, truth)
    print(f"  recall@{TOPK} = {rec_a:.4f}  scanned {frac_a:.1%}", flush=True)


if __name__ == "__main__":
    main()
