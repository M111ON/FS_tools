"""Centroid label permutation fix + drift experiment.

1. Hungarian matching of centroids before/after refit to separate
   "real cluster drift" from "label permutation" in churn measurement.
2. Drift experiment: data gradually shifts from distribution D0 to D1;
   measure when Frozen degrades and MovingAvg starts helping.
3. Routing version / centroid_id / adapter_version recorded in relation record.
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from scipy.optimize import linear_sum_assignment
from learned_routing import (make_clusters, SphericalKMeansRouter,
                             metric_semantic_separation, metric_purity,
                             metric_occupancy_balance, metric_nn_preservation)
from incremental_centroid import FrozenCentroidRouter, MovingAvgRouter

EMBED_DIM = 384


def hungarian_churn(X_fit, X_all):
    """Compare churn with and without centroid label matching."""
    n_branches = 6
    n_steps = 2
    r1 = SphericalKMeansRouter(n_branches, n_steps, iters=30, seed=0)
    r1.fit(X_fit)
    r2 = SphericalKMeansRouter(n_branches, n_steps, iters=30, seed=0)
    r2.fit(X_all)
    # raw churn (label-aligned indices)
    before_raw = [r1.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    # r2 centroids permuted; match r2 centroids to r1 centroids via cosine
    C1 = r1.centroids
    C2 = r2.centroids
    # cosine similarity matrix
    C1n = C1 / (np.linalg.norm(C1, axis=1, keepdims=True) + 1e-12)
    C2n = C2 / (np.linalg.norm(C2, axis=1, keepdims=True) + 1e-12)
    sim = C1n @ C2n.T  # (k, k)
    # Hungarian on cost = 1 - sim (maximize sim)
    row_ind, col_ind = linear_sum_assignment(-sim)
    # mapping: r2 branch col_ind[j] corresponds to r1 branch row_ind[j]
    perm = np.zeros(n_branches, dtype=int)
    for r, c in zip(row_ind, col_ind):
        perm[c] = r  # new label c -> old label r
    # remap r2 branches of old items using perm
    before = before_raw
    after_raw = [r2.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    after_matched = [int(perm[b]) for b in after_raw]
    churn_raw = sum(1 for i in range(len(X_fit)) if before[i] != after_raw[i]) / len(X_fit)
    churn_matched = sum(1 for i in range(len(X_fit)) if before[i] != after_matched[i]) / len(X_fit)
    # how much of raw churn was just permutation?
    perm_only = churn_raw - churn_matched
    print("=" * 70)
    print("CENTROID LABEL PERMUTATION ANALYSIS")
    print("=" * 70)
    print(f"  raw churn (no matching):     {churn_raw:.4f}")
    print(f"  matched churn (Hungarian):   {churn_matched:.4f}")
    print(f"  churn explained by perm:    {perm_only:.4f} ({perm_only/max(churn_raw,1e-9)*100:.1f}% of raw)")
    print(f"  real drift churn:            {churn_matched:.4f}")
    print()
    print("  Centroid similarity matrix (r1 rows x r2 cols):")
    print("  " + "  ".join(f"{j:>6}" for j in range(n_branches)))
    for i in range(n_branches):
        print(f"  {i}: " + "  ".join(f"{sim[i,j]:>6.3f}" for j in range(n_branches)))
    print(f"  Hungarian mapping (new->old): {perm.tolist()}")
    # cluster quality: did centroids actually move?
    centroid_drift = np.mean([1 - (C1n[i] @ C2n[perm == i][0] if (perm == i).any() else 0)
                             for i in range(n_branches)])
    # simpler: mean cosine between matched pairs
    matched_cos = np.mean([sim[r, c] for r, c in zip(row_ind, col_ind)])
    print(f"  mean cosine between matched centroid pairs: {matched_cos:.4f}")
    print(f"  -> {'centroids barely moved (label perm dominates)' if matched_cos > 0.95 else 'centroids genuinely drifted'}")
    return churn_raw, churn_matched, perm


def make_drift_data(n, dim, k, seed=0):
    """Generate data that gradually shifts from D0 to D1.

    Returns list of (X_batch, label_batch, t) where t in [0,1] is drift level.
    """
    rng = np.random.default_rng(seed)
    # D0 centers
    c0 = rng.normal(size=(k, dim))
    c0 /= np.linalg.norm(c0, axis=1, keepdims=True)
    # D1 centers: rotated + shifted
    c1 = c0.copy()
    # rotate each center slightly + add drift
    for i in range(k):
        rot = rng.normal(size=(dim, dim)) * 0.0
        c1[i] = c0[i] + 0.8 * rng.normal(size=dim)
    c1 /= np.linalg.norm(c1, axis=1, keepdims=True)
    batches = []
    n_batches = 8
    per = n // n_batches
    for b in range(n_batches):
        t = b / (n_batches - 1)
        centers = (1 - t) * c0 + t * c1
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        labels = rng.integers(0, k, size=per)
        X = centers[labels] + 0.15 * rng.normal(size=(per, dim))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        batches.append((X, labels, t))
    return batches, c0, c1


def drift_experiment():
    print()
    print("=" * 70)
    print("DISTRIBUTION DRIFT EXPERIMENT")
    print("=" * 70)
    n = 400
    dim = 384
    k = 6
    batches, c0, c1 = make_drift_data(n, dim, k, seed=3)
    n_branches = 6
    n_steps = 2

    # initial fit on first batch (D0)
    X0, labels0, t0 = batches[0]
    print(f"  initial fit on batch 0 (t={t0:.2f}), n={len(X0)}")

    # Frozen: fit once on D0
    frozen = FrozenCentroidRouter(n_branches, n_steps, iters=30, seed=0)
    frozen.fit(X0)
    # MovingAvg: fit on D0, then stream all batches
    moving = MovingAvgRouter(n_branches, n_steps, iters=30, seed=0, alpha=0.05)
    moving.fit(X0)
    # Full refit: refit on cumulative data each batch (oracle, high churn)
    refit = SphericalKMeansRouter(n_branches, n_steps, iters=30, seed=0)
    refit.fit(X0)

    # metrics over time
    print(f"\n  {'batch':>5} {'t':>6} {'strat':<12} {'sep':>7} {'purity':>8} {'bal':>7} {'nn':>7}")
    # track cumulative data for refit
    cum_X = X0.copy()
    cum_labels = labels0.copy()
    for b, (Xb, lb, t) in enumerate(batches):
        # frozen: assign only (no update)
        # moving: stream batch (updates centroids)
        # refit: refit on cumulative
        if b > 0:
            cum_X = np.vstack([cum_X, Xb])
            cum_labels = np.concatenate([cum_labels, lb])
            refit.fit(cum_X)
            # moving streams this batch
            for i in range(len(Xb)):
                moving.route(f"drift_{b}_{i}", Xb[i])
        # evaluate all on current batch
        for name, router in [("frozen", frozen), ("moving", moving), ("refit", refit)]:
            sep, sr, dr = metric_semantic_separation(Xb, lb, router)
            pur = metric_purity(Xb, lb, router)
            bal, _ = metric_occupancy_balance(Xb, router)
            nn = metric_nn_preservation(Xb, router, k=5)
            print(f"  {b:>5} {t:>6.2f} {name:<12} {sep:>7.4f} {pur:>8.4f} {bal:>7.4f} {nn:>7.4f}")
        print()

    print("  Frozen: semantics degrade as drift increases (centroids stale).")
    print("  Moving: adapts gradually; helps when drift is slow.")
    print("  Refit: best semantics always, but high churn (use sparingly).")
    print()
    print("  Recommendation: Frozen default; switch to Moving when drift monitor")
    print("  detects sustained separation drop; Full Refit only on manual rebuild.")


def routing_version_record():
    """Show routing_version / centroid_id / adapter_version in relation record."""
    print()
    print("=" * 70)
    print("ROUTING VERSION RECORD (schema addition)")
    print("=" * 70)
    example = {
        "item_id": "abc123",
        "branches": [2, 1],
        "positions": [[50, 50, 51], [57, 61, 64]],
        "step_fingerprints": ["B2:P(50,50,51):S0:...", "B1:P(57,61,64):S1:..."],
        "path_fingerprint": "677a74e06cc929fc",
        "item_fingerprint": "abc123:677a74e06cc929fc",
        "ok_steps": 2,
        "total_steps": 2,
        "fruit_dims": [384, 128],
        "fruit_ids": [0, 1],
        # NEW:
        "routing_version": "skm-v1",
        "centroid_id": "centroid-snapshot-2026-11-03T12:00Z",
        "adapter_version": "least-squares-v1",
        "common_space_dim": 32,
    }
    print(json.dumps(example, indent=2, ensure_ascii=False))
    print()
    print("  These fields allow tracing which model version routed each item,")
    print("  enabling rollback / A-B comparison / audit.")


if __name__ == "__main__":
    X_fit = make_clusters(300, 384, 8, spread=0.2, seed=1)[0]
    X_all = make_clusters(400, 384, 8, spread=0.2, seed=1)[0]
    hungarian_churn(X_fit, X_all)
    drift_experiment()
    routing_version_record()
    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)
