"""Drift Monitor Calibration.

Tests the decision system that switches between Frozen / Moving / Refit modes.

Monitors (label-free where possible):
  - Assignment Margin: gap between best and 2nd-best centroid similarity
  - Distance-to-Centroid: mean distance of batch to nearest centroid
  - Occupancy Entropy: branch occupancy balance
  - Centroid Displacement: how much centroids moved between batches
  - (label-dependent, for calibration only) Semantic Separation, Purity

Tests:
  1. False Positive: stationary distribution -> monitor must NOT trigger
  2. Detection Delay: how many batches after drift starts until trigger
  3. Recovery: when distribution returns to original, routing stays continuous
  4. Route Stability: old items' branches unchanged when centroids move
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from learned_routing import make_clusters
from incremental_centroid import FrozenCentroidRouter, MovingAvgRouter
from centroid_matching import make_drift_data

EMBED_DIM = 384


class DriftMonitor:
    """Label-free drift detection with hysteresis."""

    def __init__(self, n_branches, window=3, threshold_margin=0.05,
                 threshold_dist=0.10, threshold_occ=0.10, threshold_disp=0.15):
        self.n_branches = n_branches
        self.window = window
        self.thr = {"margin": threshold_margin, "dist": threshold_dist,
                    "occ": threshold_occ, "disp": threshold_disp}
        # history
        self.hist_margin = []
        self.hist_dist = []
        self.hist_occ = []
        self.hist_disp = []
        self.prev_centroids = None
        self.trigger_count = 0
        self.mode = "frozen"  # frozen | moving | refit

    def observe(self, X_batch, router):
        """Compute label-free metrics for a batch. Return dict."""
        Xn = X_batch / (np.linalg.norm(X_batch, axis=1, keepdims=True) + 1e-12)
        C = router.centroids
        Cn = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-12)
        sims = Xn @ Cn.T  # (n, k)
        # assignment margin: best - 2nd best sim
        sorted_sims = np.sort(sims, axis=1)[:, ::-1]
        margins = sorted_sims[:, 0] - sorted_sims[:, 1]
        mean_margin = float(np.mean(margins))
        # distance to centroid (in normalized space, 1 - sim)
        best_sim = sorted_sims[:, 0]
        mean_dist = float(np.mean(1 - best_sim))
        # occupancy
        assign = np.argmax(sims, axis=1)
        counts = np.array([np.sum(assign == b) for b in range(self.n_branches)], dtype=float)
        p = counts / counts.sum()
        p = p[p > 0]
        occ_entropy = -np.sum(p * np.log(p + 1e-12))
        max_ent = np.log(self.n_branches)
        occ_norm = occ_entropy / max_ent
        # centroid displacement
        disp = 0.0
        if self.prev_centroids is not None:
            Cn_prev = self.prev_centroids / (np.linalg.norm(self.prev_centroids, axis=1, keepdims=True) + 1e-12)
            # match by Hungarian-ish (greedy) for displacement
            # simple: mean of max cosine per centroid
            cosmat = Cn @ Cn_prev.T
            disp = float(1 - np.mean(np.max(cosmat, axis=1)))
        self.prev_centroids = C.copy()

        metrics = {
            "margin": mean_margin,
            "dist": mean_dist,
            "occ": occ_norm,
            "disp": disp,
        }
        self.hist_margin.append(mean_margin)
        self.hist_dist.append(mean_dist)
        self.hist_occ.append(occ_norm)
        self.hist_disp.append(disp)

        # decision: trigger if sustained deviation in window
        trigger = self._decide(metrics)
        return metrics, trigger

    def _decide(self, m):
        # need at least `window` observations
        if len(self.hist_margin) < self.window:
            return False
        # baseline = first `window` observations (stationary reference)
        base_margin = np.mean(self.hist_margin[:self.window])
        base_dist = np.mean(self.hist_dist[:self.window])
        base_occ = np.mean(self.hist_occ[:self.window])
        # current = last `window`
        cur_margin = np.mean(self.hist_margin[-self.window:])
        cur_dist = np.mean(self.hist_dist[-self.window:])
        cur_occ = np.mean(self.hist_occ[-self.window:])
        cur_disp = np.mean(self.hist_disp[-self.window:])

        signals = 0
        if base_margin - cur_margin > self.thr["margin"]:
            signals += 1
        if cur_dist - base_dist > self.thr["dist"]:
            signals += 1
        if base_occ - cur_occ > self.thr["occ"]:
            signals += 1
        if cur_disp > self.thr["disp"]:
            signals += 1
        # trigger if at least 2 signals (avoid FP from noise)
        triggered = signals >= 2
        if triggered:
            self.trigger_count += 1
        return triggered


def run_false_positive():
    print("=" * 70)
    print("TEST 1: FALSE POSITIVE (stationary distribution)")
    print("=" * 70)
    n = 400
    dim = 384
    k = 6
    X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=1)
    batches = [X[i:i+50] for i in range(0, n, 50)]
    n_branches = 6
    n_steps = 2
    router = FrozenCentroidRouter(n_branches, n_steps, iters=30, seed=0)
    router.fit(batches[0])
    mon = DriftMonitor(n_branches, window=3)
    triggers = 0
    for bi, Xb in enumerate(batches):
        m, trig = mon.observe(Xb, router)
        if trig:
            triggers += 1
            print(f"  batch {bi}: TRIGGERED (FP!) metrics={m}")
        else:
            print(f"  batch {bi}: ok metrics={m}")
    print(f"\n  triggers: {triggers} / {len(batches)}")
    print(f"  FALSE POSITIVE: {'YES (bad)' if triggers > 0 else 'NO (good)'}")
    return triggers == 0


def run_detection_delay():
    print()
    print("=" * 70)
    print("TEST 2: DETECTION DELAY (drift starts at batch 3)")
    print("=" * 70)
    n = 400
    dim = 384
    k = 6
    batches, c0, c1 = make_drift_data(n, dim, k, seed=3)
    # batches 0-2 stationary (D0), 3-7 drift
    n_branches = 6
    n_steps = 2
    router = FrozenCentroidRouter(n_branches, n_steps, iters=30, seed=0)
    router.fit(batches[0][0])
    mon = DriftMonitor(n_branches, window=3)
    first_trigger = None
    for bi, (Xb, lb, t) in enumerate(batches):
        m, trig = mon.observe(Xb, router)
        status = "TRIGGERED" if trig else "ok"
        drift_flag = " [DRIFT]" if bi >= 3 else ""
        print(f"  batch {bi} (t={t:.2f}){drift_flag}: {status} margin={m['margin']:.3f} dist={m['dist']:.3f} occ={m['occ']:.3f} disp={m['disp']:.3f}")
        if trig and first_trigger is None:
            first_trigger = bi
    delay = (first_trigger - 3) if first_trigger is not None and first_trigger >= 3 else None
    print(f"\n  drift starts at batch 3; first trigger at batch {first_trigger}")
    print(f"  detection delay: {delay} batches" if delay is not None else "  never triggered")
    return first_trigger is not None and first_trigger <= 5


def run_recovery():
    print()
    print("=" * 70)
    print("TEST 3: RECOVERY (drift then return to original)")
    print("=" * 70)
    n = 400
    dim = 384
    k = 6
    batches, c0, c1 = make_drift_data(n, dim, k, seed=3)
    # build: 3 batches D0, 3 batches drift, 2 batches back to D0
    # approximate by reusing D0 data for "return"
    X0 = batches[0][0]
    # create return batches from c0
    rng = np.random.default_rng(99)
    ret_batches = []
    for _ in range(2):
        labels = rng.integers(0, k, size=50)
        X = c0[labels] + 0.15 * rng.normal(size=(50, dim))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        ret_batches.append((X, labels, 0.0))
    all_batches = list(batches[:3]) + list(batches[5:7]) + ret_batches

    n_branches = 6
    n_steps = 2
    router = FrozenCentroidRouter(n_branches, n_steps, iters=30, seed=0)
    router.fit(all_batches[0][0])
    mon = DriftMonitor(n_branches, window=3)
    triggers = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, trig = mon.observe(Xb, router)
        triggers.append(trig)
        phase = "D0" if bi < 3 else ("drift" if bi < 6 else "return")
        print(f"  batch {bi} ({phase}): trigger={trig} margin={m['margin']:.3f} dist={m['dist']:.3f}")
    # recovery: after return, triggers should stop
    return_triggers = sum(1 for i in range(6, len(all_batches)) if triggers[i])
    print(f"\n  triggers during return phase: {return_triggers}")
    print(f"  RECOVERY: {'OK (triggers stop)' if return_triggers == 0 else 'ISSUE (still triggering)'}")
    return return_triggers == 0


def run_route_stability():
    print()
    print("=" * 70)
    print("TEST 4: ROUTE STABILITY (old items unchanged when centroids move)")
    print("=" * 70)
    n = 300
    dim = 384
    k = 6
    X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=1)
    X_fit = X[:200]
    X_new = X[200:]
    n_branches = 6
    n_steps = 2

    # frozen: old items never move
    frozen = FrozenCentroidRouter(n_branches, n_steps, iters=30, seed=0)
    frozen.fit(X_fit)
    old_before = [frozen.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    # process new items (frozen doesn't update centroids, so no change)
    for i in range(len(X_new)):
        frozen.route(f"new_{i}", X_new[i])
    old_after = [frozen.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    moved_frozen = sum(1 for i in range(len(X_fit)) if old_before[i] != old_after[i])
    print(f"  Frozen: old items moved = {moved_frozen} / {len(X_fit)} (expect 0)")

    # moving: centroids update -> old items may move
    moving = MovingAvgRouter(n_branches, n_steps, iters=30, seed=0, alpha=0.05)
    moving.fit(X_fit)
    old_before_m = [moving.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    # snapshot centroids
    snap = moving.centroids.copy()
    for i in range(len(X_new)):
        moving.route(f"new_{i}", X_new[i])
    old_after_m = [moving.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    moved_moving = sum(1 for i in range(len(X_fit)) if old_before_m[i] != old_after_m[i])
    print(f"  Moving: old items moved = {moved_moving} / {len(X_fit)}")

    print()
    print("  Frozen guarantees route stability (0 moved) by construction.")
    print("  Moving allows old-route churn when centroids drift (trade-off).")
    print("  Route Stability metric: fraction of old items whose branch changed.")
    print(f"  Frozen stability: {1 - moved_frozen/len(X_fit):.4f}")
    print(f"  Moving stability: {1 - moved_moving/len(X_fit):.4f}")
    return moved_frozen == 0


def main():
    r1 = run_false_positive()
    r2 = run_detection_delay()
    r3 = run_recovery()
    r4 = run_route_stability()
    print()
    print("=" * 70)
    print(f"Drift Monitor: FP={'OK' if r1 else 'FAIL'}, delay={'OK' if r2 else 'FAIL'}, "
          f"recovery={'OK' if r3 else 'FAIL'}, stability={'OK' if r4 else 'FAIL'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
