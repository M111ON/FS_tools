"""phase_e_projection.py — is the dir3 loss caused by the 384->3 projection, or by
hard assignment to a fixed direction set?

The report claimed the two 0.214 values were "not a coincidence". That claim was
too strong: two 3-decimal matches from different corpora and metrics can be chance
or shared code. This replaces it with a mechanism test.

Arms, all K=26, keep = overlap with brute-force top-10:
    kmeans in d-dim PCA space, d = 3, 8, 16, 32, 64, 384
      d=384 is the anchor and must reproduce the reported kmeans keep (0.572 at
      p=1). If keep climbs with d, the projection was the loss. If d=3 already
      reaches the 384 value, the projection was not the loss.
    dir3 (26 fixed {-1,0,1}^3 directions on the 3-dim projection) for reference.

Plus a paired bootstrap, because comparing two routers on the same queries is far
tighter than either router's own interval.

Usage: python phase_e_projection.py --dir <prep out dir>
"""
import argparse, itertools, json, os
import numpy as np

DIMS = (3, 8, 16, 32, 64, 384)
K = 26
PROBES = (1, 2, 4)
TOPN = 10
ITERS = 25
BOOT = 2000
SEED = 3
LEVEL_KINDS = {6: {1}, 14: {1, 3}, 26: {1, 2, 3}}


def kmeans(X, K, iters=ITERS):
    N = len(X)
    C = X[np.linspace(0, N - 1, K).astype(int)].copy()
    x2 = (X ** 2).sum(1)[:, None]
    for _ in range(iters):
        a = (x2 - 2 * X @ C.T + (C ** 2).sum(1)[None]).argmin(1)
        for k in range(K):
            m = a == k
            if m.any():
                C[k] = X[m].mean(0)
    return C, (x2 - 2 * X @ C.T + (C ** 2).sum(1)[None]).argmin(1)


def dirs(K):
    kinds = LEVEL_KINDS[K]
    V = np.array([v for v in itertools.product((-1, 0, 1), repeat=3)
                  if any(v) and sum(map(abs, v)) in kinds], float)
    return V / np.linalg.norm(V, axis=1, keepdims=True)


def evaluate(S, assign, order, p, brute_top, qrels):
    """per-query keep@10 and recall@10 when probing the top-p buckets."""
    keep = np.empty(len(order))
    rec = np.empty(len(order))
    scanned = np.empty(len(order))
    for qi in range(len(order)):
        probe = order[qi, :p]
        cand = np.where(np.isin(assign, probe))[0]
        best = cand[np.argsort(-S[qi, cand])[:TOPN]]
        hit = set(best.tolist()) & set(brute_top[qi].tolist())
        keep[qi] = len(hit) / TOPN
        pos = set(qrels[qi])
        rec[qi] = sum(1 for d in best if d in pos) / min(len(pos), TOPN)
        scanned[qi] = len(cand) / len(assign)
    return keep, rec, scanned


def paired_ci(a, b, n=BOOT, seed=SEED):
    d = a - b
    rng = np.random.default_rng(seed)
    m = d[rng.integers(0, len(d), size=(n, len(d)))].mean(1)
    return float(d.mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()

    C = np.load(os.path.join(a.dir, "corpus.npy"))
    Q = np.load(os.path.join(a.dir, "queries.npy"))
    meta = json.load(open(os.path.join(a.dir, "meta.json"), encoding="utf-8"))
    qrels = {int(k): v for k, v in meta["qrels"].items()}

    S = Q @ C.T
    brute_top = np.argsort(-S, axis=1)[:, :TOPN]
    print(f"corpus {C.shape}  queries {Q.shape}  K={K}\n")

    nrng = np.random.default_rng(SEED)
    sub = C if len(C) <= 5000 else C[nrng.choice(len(C), 5000, replace=False)]
    mu = sub.mean(0)
    sv = np.linalg.svd(sub - mu, full_matrices=False, compute_uv=False)
    var = (sv ** 2) / (sv ** 2).sum()
    _, _, Vt = np.linalg.svd(sub - mu, full_matrices=False)

    print("arm                   dim  var%    scan    keep   rec10")
    print("-" * 56)
    arms = {}
    for d in DIMS:
        P_C, P_Q = (C - mu) @ Vt[:d].T, (Q - mu) @ Vt[:d].T
        cent, assign = kmeans(P_C, K)
        order = np.argsort(((P_Q ** 2).sum(1)[:, None] - 2 * P_Q @ cent.T
                            + (cent ** 2).sum(1)[None]), axis=1)
        for p in PROBES:
            keep, rec, scanned = evaluate(S, assign, order, p, brute_top, qrels)
            arms[("kmeans", d, p)] = (keep, rec)
            print(f"kmeans-{d:>3}d p={p:<2d}     {d:>4d} {100*var[:d].sum():5.1f}  "
                  f"{scanned.mean():6.1%}  {keep.mean():6.3f}  {rec.mean():6.3f}")
        print()

    # dir3 reference, identical construction to eval_router.py
    P_C, P_Q = (C - mu) @ Vt[:3].T, (Q - mu) @ Vt[:3].T
    D = dirs(K)
    unit = lambda M: M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)
    oc = np.argsort(-unit(P_C) @ D.T, axis=1)
    oq = np.argsort(-unit(P_Q) @ D.T, axis=1)
    for p in PROBES:
        keep, rec, scanned = evaluate(S, oc[:, 0], oq, p, brute_top, qrels)
        arms[("dir3", 3, p)] = (keep, rec)
        print(f"dir3          p={p:<2d}        3 {100*var[:3].sum():5.1f}  "
              f"{scanned.mean():6.1%}  {keep.mean():6.3f}  {rec.mean():6.3f}")

    print("\n" + "=" * 56)
    print("paired bootstrap on the same 200 queries (95% CI)")
    for label, ka, kb in (
        ("keep kmeans-384d p=1 - kmeans-3d p=1", ("kmeans", 384, 1), ("kmeans", 3, 1)),
        ("keep kmeans-384d p=4 - kmeans-3d p=4", ("kmeans", 384, 4), ("kmeans", 3, 4)),
        ("keep kmeans-3d   p=1 - dir3      p=1", ("kmeans", 3, 1), ("dir3", 3, 1)),
        ("keep kmeans-384d p=4 - dir3      p=4", ("kmeans", 384, 4), ("dir3", 3, 4)),
    ):
        m, lo, hi = paired_ci(arms[ka][0], arms[kb][0])
        verdict = "real" if (lo > 0 or hi < 0) else "NOT separated"
        print(f"  {label:40s} {m:+.3f} [{lo:+.3f}, {hi:+.3f}]  {verdict}")

    out = os.path.join(a.dir, "phase_e_projection.json")
    json.dump({"dims": list(DIMS), "K": K, "probes": list(PROBES),
               "var_explained": {str(d): float(var[:d].sum()) for d in DIMS},
               "keep": {f"{n}_{d}_{p}": float(v[0].mean()) for (n, d, p), v in arms.items()},
               "rec10": {f"{n}_{d}_{p}": float(v[1].mean()) for (n, d, p), v in arms.items()}},
              open(out, "w"), indent=1)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
