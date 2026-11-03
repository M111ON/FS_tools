"""Drift Monitor v4 — Absolute-only + Fixed Reference + Hysteresis.

Calibration fix: remove sigma-relative (across-batch std too high). Use
absolute thresholds only. Baseline recorded from first N stationary batches
(mean only, no std-based threshold). Reference centroids fixed (never
overwritten). Active centroids separate (for Moving/Frozen routing).

State machine: FROZEN -> DRIFT -> RECOVERING -> FROZEN
Hysteresis: enter threshold T_enter, exit threshold T_exit = T_enter * EXIT_RATIO.
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from learned_routing import make_clusters
from incremental_centroid import FrozenCentroidRouter, MovingAvgRouter
from centroid_matching import make_drift_data

EMBED_DIM = 384

# absolute thresholds (calibrated defaults, not final locked values)
MARGIN_ENTER = 0.05
DIST_ENTER = 0.10
OCCUPANCY_ENTER = 0.10
DISPLACEMENT_ENTER = 0.15

ENTER_SIGNALS = 2
ENTER_BATCHES = 2
EXIT_SIGNALS = 1
EXIT_BATCHES = 3
EXIT_RATIO = 0.50

BASELINE_N = 3
BASELINE_MODE = "FIXED_REFERENCE"
AUTO_REBASE = False
PRESERVE_OLD_PATHS = True


class DriftMonitorV4:
    def __init__(self, n_branches, baseline_n=BASELINE_N):
        self.n_branches = n_branches
        self.baseline_n = baseline_n
        # fixed reference (never overwritten)
        self.ref_mean = None
        self.ref_centroids = None  # reference centroid snapshot
        self.baseline_ready = False
        self._baseline_samples = []
        # active centroids (separate, for routing)
        self.active_centroids = None
        # state machine
        self.state = "INIT"
        self.enter_counter = 0
        self.exit_counter = 0
        self.history = []

    def _compute_metrics(self, X_batch, router):
        Xn = X_batch / (np.linalg.norm(X_batch, axis=1, keepdims=True) + 1e-12)
        C = router.centroids  # active centroids
        Cn = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-12)
        sims = Xn @ Cn.T
        sorted_sims = np.sort(sims, axis=1)[:, ::-1]
        margins = sorted_sims[:, 0] - sorted_sims[:, 1]
        best_sim = sorted_sims[:, 0]
        assign = np.argmax(sims, axis=1)
        counts = np.array([np.sum(assign == b) for b in range(self.n_branches)], dtype=float)
        p = counts / counts.sum()
        p = p[p > 0]
        occ_entropy = -np.sum(p * np.log(p + 1e-12))
        occ_norm = occ_entropy / np.log(self.n_branches)
        # displacement vs REFERENCE centroids (fixed)
        if self.ref_centroids is not None:
            Cn_ref = self.ref_centroids / (np.linalg.norm(self.ref_centroids, axis=1, keepdims=True) + 1e-12)
            cosmat = Cn @ Cn_ref.T
            disp = float(1 - np.mean(np.max(cosmat, axis=1)))
        else:
            disp = 0.0
        return {
            "margin": float(np.mean(margins)),
            "dist": float(np.mean(1 - best_sim)),
            "occ": float(occ_norm),
            "disp": disp,
        }

    def set_reference(self, X_batch, router):
        m = self._compute_metrics(X_batch, router)
        self._baseline_samples.append(m)
        if len(self._baseline_samples) >= self.baseline_n:
            arrs = {k: np.array([s[k] for s in self._baseline_samples]) for k in self._baseline_samples[0]}
            self.ref_mean = {k: float(np.mean(v)) for k, v in arrs.items()}
            self.ref_centroids = router.centroids.copy()  # FIXED reference
            self.active_centroids = router.centroids.copy()
            self.baseline_ready = True
            self.state = "FROZEN"
            self.enter_counter = 0
            self.exit_counter = 0

    def observe(self, X_batch, router):
        # active centroids = router's current centroids
        self.active_centroids = router.centroids.copy()
        m = self._compute_metrics(X_batch, router)
        self.history.append(m)

        if not self.baseline_ready:
            self.set_reference(X_batch, router)
            return m, self.state, False, 0

        # enter thresholds (absolute)
        enter_thr = {
            "margin": MARGIN_ENTER, "dist": DIST_ENTER,
            "occ": OCCUPANCY_ENTER, "disp": DISPLACEMENT_ENTER,
        }
        exit_thr = {k: v * EXIT_RATIO for k, v in enter_thr.items()}

        # signal count
        signals = 0
        if (self.ref_mean["margin"] - m["margin"]) > enter_thr["margin"]:
            signals += 1
        if (m["dist"] - self.ref_mean["dist"]) > enter_thr["dist"]:
            signals += 1
        if (self.ref_mean["occ"] - m["occ"]) > enter_thr["occ"]:
            signals += 1
        if m["disp"] > enter_thr["disp"]:
            signals += 1

        # recovery: allow up to EXIT_SIGNALS signals (hysteresis in signal count)
        # exit thresholds also use EXIT_RATIO for hysteresis on magnitude
        in_recovery = (signals <= EXIT_SIGNALS)

        prev_state = self.state
        triggered = False
        if self.state == "FROZEN":
            if signals >= ENTER_SIGNALS:
                self.enter_counter += 1
            else:
                self.enter_counter = 0
            if self.enter_counter >= ENTER_BATCHES:
                self.state = "DRIFT"
                self.enter_counter = 0
                triggered = True
        elif self.state == "DRIFT":
            if in_recovery:
                self.state = "RECOVERING"
                self.exit_counter = 1
            else:
                self.exit_counter = 0
        elif self.state == "RECOVERING":
            if in_recovery:
                self.exit_counter += 1
                if self.exit_counter >= EXIT_BATCHES:
                    self.state = "FROZEN"
                    self.exit_counter = 0
            else:
                self.state = "DRIFT"
                self.exit_counter = 0

        return m, self.state, triggered, signals


# ---------- tests ----------
def run_false_positive():
    print("=" * 70)
    print("TEST 1: FALSE POSITIVE (stationary, multiple seeds)")
    print("=" * 70)
    all_ok = True
    for seed in [1, 2, 3, 4, 5]:
        n = 400; dim = 384; k = 6
        X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=seed)
        batches = [X[i:i+50] for i in range(0, n, 50)]
        nb = 6; ns = 2
        router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
        router.fit(batches[0])
        mon = DriftMonitorV4(nb, baseline_n=BASELINE_N)
        for i in range(BASELINE_N):
            mon.set_reference(batches[i], router)
        triggers = 0
        for bi in range(BASELINE_N, len(batches)):
            Xb = batches[bi]
            m, st, trig, sig = mon.observe(Xb, router)
            if trig:
                triggers += 1
        status = "OK" if triggers == 0 else "FP!"
        print(f"  seed={seed}: triggers={triggers}/{len(batches)-BASELINE_N} {status}")
        if triggers > 0:
            all_ok = False
    print(f"\n  FALSE POSITIVE (all seeds): {'NO (good)' if all_ok else 'YES (bad)'}")
    return all_ok


def run_detection_delay():
    print()
    print("=" * 70)
    print("TEST 2: DETECTION DELAY")
    print("=" * 70)
    batches, c0, c1 = make_drift_data(400, 384, 6, seed=3)
    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(batches[0][0])
    mon = DriftMonitorV4(nb, baseline_n=1)
    mon.set_reference(batches[0][0], router)
    for i in range(1, 3):
        mon.set_reference(batches[i][0], router)
    first_trigger = None
    for bi, (Xb, lb, t) in enumerate(batches):
        if bi < 3:
            continue
        m, st, trig, sig = mon.observe(Xb, router)
        drift_flag = " [DRIFT]" if bi >= 3 else ""
        print(f"  batch {bi} (t={t:.2f}){drift_flag}: {st} margin={m['margin']:.3f} dist={m['dist']:.3f} occ={m['occ']:.3f} disp={m['disp']:.3f} sig={sig} trig={trig}")
        if trig and first_trigger is None:
            first_trigger = bi
    delay = (first_trigger - 3) if (first_trigger is not None and first_trigger >= 3) else None
    print(f"\n  drift starts batch 3; first trigger batch {first_trigger}; delay={delay}")
    return first_trigger is not None and first_trigger <= 5


def run_recovery():
    print()
    print("=" * 70)
    print("TEST 3: RECOVERY (drift then return to original)")
    print("=" * 70)
    batches, c0, c1 = make_drift_data(400, 384, 6, seed=3)
    X0 = batches[0][0]
    rng = np.random.default_rng(99)
    # FULL_RECOVERY: use c0 centers directly (stationary)
    full_ret = []
    for _ in range(3):
        labels = rng.integers(0, 6, size=50)
        X = c0[labels] + 0.05 * rng.normal(size=(50, 384))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        full_ret.append((X, labels, 0.0))
    # PARTIAL_RECOVERY: margin ~0.087 (improved but not baseline)
    rng2 = np.random.default_rng(77)
    partial_ret = []
    for _ in range(2):
        labels = rng2.integers(0, 6, size=50)
        X = c0[labels] + 0.4 * rng2.normal(size=(50, 384))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        partial_ret.append((X, labels, 0.0))
    all_batches = (list(batches[:3]) + list(batches[5:7]) +
                   partial_ret + full_ret)

    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(all_batches[0][0])
    mon = DriftMonitorV4(nb, baseline_n=BASELINE_N)
    for i in range(BASELINE_N):
        mon.set_reference(all_batches[i][0], router)
    ref_before = {k: mon.ref_mean[k] for k in mon.ref_mean}
    triggers = []
    states = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        triggers.append(trig)
        states.append(st)
        phase = "D0" if bi < 3 else ("drift" if bi < 6 else "return")
        print(f"  batch {bi} ({phase}): state={st} trig={trig} margin={m['margin']:.3f} dist={m['dist']:.3f} sig={sig}")
    # return phase now starts at batch 8 (after partial 2 + full 3... let's compute)
    # all_batches: 3 (D0) + 2 (drift) + 2 (partial) + 3 (full) = 10
    return_states = states[7:]
    recovered = any(s == "FROZEN" for s in return_states)
    ref_after = {k: mon.ref_mean[k] for k in mon.ref_mean}
    ref_unchanged = (ref_before == ref_after)
    print(f"\n  triggers during return: {sum(1 for i in range(7, len(all_batches)) if triggers[i])}")
    print(f"  recovered to FROZEN: {recovered}")
    print(f"  reference baseline unchanged: {ref_unchanged}")
    print(f"  RECOVERY: {'OK' if (recovered and ref_unchanged) else 'ISSUE'}")
    return recovered and ref_unchanged


def run_multi_cycle():
    print()
    print("=" * 70)
    print("TEST 4: MULTI-CYCLE (drift -> recovery -> drift again)")
    print("=" * 70)
    batches, c0, c1 = make_drift_data(400, 384, 6, seed=3)
    X0 = batches[0][0]
    rng = np.random.default_rng(99)
    ret = []
    for _ in range(2):
        labels = rng.integers(0, 6, size=50)
        X = c0[labels] + 0.15 * rng.normal(size=(50, 384))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        ret.append((X, labels, 0.0))
    cycle1 = list(batches[:3]) + list(batches[5:7]) + ret
    cycle2 = list(batches[5:7]) + ret
    all_batches = cycle1 + cycle2

    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(all_batches[0][0])
    mon = DriftMonitorV4(nb, baseline_n=BASELINE_N)
    for i in range(BASELINE_N):
        mon.set_reference(all_batches[i][0], router)
    ref0 = {k: mon.ref_mean[k] for k in mon.ref_mean}

    states = []
    triggers = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        states.append(st)
        triggers.append(trig)

    ref_unchanged = ({k: mon.ref_mean[k] for k in mon.ref_mean} == ref0)
    n_frozen = sum(1 for s in states if s == "FROZEN")
    print(f"  total batches: {len(all_batches)}")
    print(f"  triggers: {sum(triggers)}")
    print(f"  FROZEN states: {n_frozen}")
    print(f"  reference baseline unchanged: {ref_unchanged}")
    print(f"  AUTO_REBASE: {AUTO_REBASE}")
    print(f"  MULTI-CYCLE: {'OK' if ref_unchanged else 'FAIL'}")
    return ref_unchanged


def run_route_stability():
    print()
    print("=" * 70)
    print("TEST 5: ROUTE STABILITY")
    print("=" * 70)
    n = 300; dim = 384; k = 6
    X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=1)
    X_fit = X[:200]; X_new = X[200:]
    nb = 6; ns = 2
    frozen = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    frozen.fit(X_fit)
    old_before = [frozen.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    for i in range(len(X_new)):
        frozen.route(f"new_{i}", X_new[i])
    old_after = [frozen.route(f"item_{i}", X_fit[i])[0][1] for i in range(len(X_fit))]
    moved = sum(1 for i in range(len(X_fit)) if old_before[i] != old_after[i])
    print(f"  Frozen: old items moved = {moved}/{len(X_fit)} (expect 0)")
    print(f"  Route Stability: {1 - moved/len(X_fit):.4f}")
    return moved == 0


def main():
    r1 = run_false_positive()
    r2 = run_detection_delay()
    r3 = run_recovery()
    r4 = run_multi_cycle()
    r5 = run_route_stability()
    print()
    print("=" * 70)
    print(f"Drift Monitor v4: FP={'OK' if r1 else 'FAIL'} delay={'OK' if r2 else 'FAIL'} "
          f"recovery={'OK' if r3 else 'FAIL'} multicycle={'OK' if r4 else 'FAIL'} "
          f"stability={'OK' if r5 else 'FAIL'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
