#!/usr/bin/env python3
"""eval_router.py — measure the router against labelled queries (output of prep_miracl_th.py).

Two separate baselines, so you can tell WHO loses recall:
  brute-force vs qrels    -> how well the EMBEDDER itself finds what a human marked relevant
  router vs brute-force   -> how much the ROUTER (bucket pruning) loses on top of that
                             (column "keep" = share of brute-force top-10 still returned)

Routers (same face counts as the rhombicuboctahedron levels):
  kmeans  K in {6,14,26}  — anchor style: evenly-spaced init, Lloyd, L2 nearest centroid
  dir3    K in {6,14,26}  — static 3-axis direction faces on a PCA-3 projection (axial / +corner / +edge)
Columns: scan = mean fraction of corpus scored, rec10 = qrels recall@10, hit10 = any positive in top-10,
         keep = overlap with brute-force top-10.

Usage: python eval_router.py --dir data/miracl_th [--k 6 14 26] [--probes 1 2 4 8] [--seed 3]
"""
import argparse, itertools, json, os
import numpy as np

# ---------------- CONFIG ----------------
DEFAULT_K      = (6, 14, 26)
DEFAULT_PROBES = (1, 2, 4, 8)
KMEANS_ITERS   = 25
TOPN           = 10
PCA_SAMPLE     = 5000
LEVEL_KINDS    = {6: {1}, 14: {1, 3}, 26: {1, 2, 3}}   # nonzero axes per face kind
# ----------------------------------------


def metrics(top, qrels):
    """top: [Q,TOPN] corpus idx. returns (recall@10, hit@10, mrr@10)."""
    rec, hit, mrr = [], [], []
    for qi, row in enumerate(top):
        pos = set(qrels[qi])
        got = [i for i, d in enumerate(row) if d in pos]
        rec.append(len(got) / min(len(pos), TOPN))
        hit.append(1.0 if got else 0.0)
        mrr.append(1.0 / (got[0] + 1) if got else 0.0)
    return np.mean(rec), np.mean(hit), np.mean(mrr)


def kmeans(X, K, iters=KMEANS_ITERS):
    N = len(X)
    C = X[np.linspace(0, N - 1, K).astype(int)].copy()
    x2 = (X ** 2).sum(1)[:, None]
    for _ in range(iters):
        a = (x2 - 2 * X @ C.T + (C ** 2).sum(1)[None]).argmin(1)
        for k in range(K):
            m = a == k
            if m.any():
                C[k] = X[m].mean(0)
    a = (x2 - 2 * X @ C.T + (C ** 2).sum(1)[None]).argmin(1)
    return C, a


def dirs(K):
    kinds = LEVEL_KINDS[K]
    V = np.array([v for v in itertools.product((-1, 0, 1), repeat=3) if any(v) and sum(map(abs, v)) in kinds], float)
    return V / np.linalg.norm(V, axis=1, keepdims=True)


def run_router(name, assign, order, K, C, Q, S, brute_top, qrels, probes):
    sizes = np.bincount(assign, minlength=K)
    print(f"\n{name}  K={K}  max-bucket {sizes.max()/len(C):.1%}  empty {(sizes == 0).sum()}")
    print("  probes   scan   rec10   hit10   keep")
    rows = []
    for p in probes:
        if p > K:
            continue
        top = np.zeros((len(Q), TOPN), dtype=np.int64)
        scan, keep = [], []
        for qi in range(len(Q)):
            cand = np.where(np.isin(assign, order[qi, :p]))[0]
            best = cand[np.argsort(-S[qi, cand])[:TOPN]]
            top[qi, :len(best)] = best
            if len(best) < TOPN:
                top[qi, len(best):] = -1
            scan.append(len(cand) / len(C))
            keep.append(len(set(best) & set(brute_top[qi])) / TOPN)
        r, h, _ = metrics(top, qrels)
        print(f"  {p:6d} {np.mean(scan):6.1%}  {r:6.3f}  {h:6.3f}  {np.mean(keep):6.3f}")
        rows.append({"name": name, "K": K, "probes": p, "scan": float(np.mean(scan)),
                     "rec10": float(r), "hit10": float(h), "keep": float(np.mean(keep))})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="data/miracl_th")
    ap.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_K))
    ap.add_argument("--probes", type=int, nargs="+", default=list(DEFAULT_PROBES))
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()

    C = np.load(os.path.join(a.dir, "corpus.npy"))
    Q = np.load(os.path.join(a.dir, "queries.npy"))
    meta = json.load(open(os.path.join(a.dir, "meta.json"), encoding="utf-8"))
    qrels = {int(k): v for k, v in meta["qrels"].items()}
    if meta.get("embedder") == "FAKE":
        print("!! embedder=FAKE: plumbing test only, numbers are meaningless\n")
    print(f"corpus {C.shape}  queries {Q.shape}  embedder={meta.get('embedder')}")

    S = Q @ C.T
    brute_top = np.argsort(-S, axis=1)[:, :TOPN]
    r, h, m = metrics(brute_top, qrels)
    print(f"\nBASELINE 1 — brute-force vs qrels (embedder quality): rec10 {r:.3f}  hit10 {h:.3f}  mrr10 {m:.3f}")
    print("  if this is far below published numbers for the dataset, fix the pipeline (prefix/sanitize/embedder) first")

    results = []
    nrng = np.random.default_rng(a.seed)
    sub = C if len(C) <= PCA_SAMPLE else C[nrng.choice(len(C), PCA_SAMPLE, replace=False)]
    mu = sub.mean(0)
    _, _, Vt = np.linalg.svd(sub - mu, full_matrices=False)
    proj_C, proj_Q = (C - mu) @ Vt[:3].T, (Q - mu) @ Vt[:3].T

    for K in a.k:
        cent, assign = kmeans(C, K)
        order = np.argsort(((Q ** 2).sum(1)[:, None] - 2 * Q @ cent.T + (cent ** 2).sum(1)[None]), axis=1)
        results += run_router("kmeans", assign, order, K, C, Q, S, brute_top, qrels, a.probes)
        if K in LEVEL_KINDS:
            D = dirs(K)
            unit = lambda M: M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)
            order_c = np.argsort(-unit(proj_C) @ D.T, axis=1)
            order_q = np.argsort(-unit(proj_Q) @ D.T, axis=1)
            results += run_router("dir3", order_c[:, 0], order_q, K, C, Q, S, brute_top, qrels, a.probes)

    out = os.path.join(a.dir, "eval_results.json")
    json.dump({"baseline_brute_vs_qrels": {"rec10": float(r), "hit10": float(h), "mrr10": float(m)},
               "routers": results}, open(out, "w"), indent=1)
    print(f"\nwrote {out}")
    print("read order: baseline 1 (embedder) -> keep (router loss) -> rec10 (what the user actually feels)")


if __name__ == "__main__":
    main()
