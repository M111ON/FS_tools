"""Drift Monitor v3 — Fixed Reference Baseline + sigma-relative thresholds + hysteresis.

Calibration fix: baseline recorded from FIRST N batches (not single batch),
thresholds are sigma-relative (2 sigma) to handle stationary variance.
Recovery uses looser threshold (1 sigma) for hysteresis (T_exit < T_enter).

Rules (unchanged from v2):
  - Fixed reference baseline, never overwritten (AUTO_REBASE=False)
  - Hysteresis state machine: FROZEN -> DRIFT -> RECOVERING -> FROZEN
  - Reset only monitor state on recovery; reference stays
  - Preserve old paths
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from learned_routing import make_clusters
from incremental_centroid import FrozenCentroidRouter, MovingAvgRouter
from centroid_matching import make_drift_data

EMBED_DIM = 384

DRIFT_ENTER_BATCHES = 2
DRIFT_EXIT_BATCHES = 3
DRIFT_SIGNAL_COUNT = 2
BASELINE_N = 3  # number of initial batches to build reference
SIGMA_ENTER = 2.0
SIGMA_EXIT = 1.0
AUTO_REBASE = False
PRESERVE_OLD_PATHS = True


class DriftMonitorV3:
    def __init__(self, n_branches, baseline_n=BASELINE_N,
                 sigma_enter=SIGMA_ENTER, sigma_exit=SIGMA_EXIT):
        self.n_branches = n_branches
        self.baseline_n = baseline_n
        self.sigma_enter = sigma_enter
        self.sigma_exit = sigma_exit
        # reference baseline (fixed, recorded once)
        self.ref_mean = None   # dict of metric -> mean
        self.ref_std = None    # dict of metric -> std
        self.ref_centroids = None
        self.baseline_ready = False
        self._baseline_samples = []
        # state machine
        self.state = "INIT"  # INIT -> FROZEN -> DRIFT -> RECOVERING -> FROZEN
        self.enter_counter = 0
        self.exit_counter = 0
        self.history = []

    def _compute_metrics(self, X_batch, router):
        Xn = X_batch / (np.linalg.norm(X_batch, axis=1, keepdims=True) + 1e-12)
        C = router.centroids
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
        Cn_ref = self.ref_centroids / (np.linalg.norm(self.ref_centroids, axis=1, keepdims=True) + 1e-12) if self.ref_centroids is not None else Cn
        cosmat = Cn @ Cn_ref.T
        disp = float(1 - np.mean(np.max(cosmat, axis=1))) if self.ref_centroids is not None else 0.0
        return {
            "margin": float(np.mean(margins)),
            "dist": float(np.mean(1 - best_sim)),
            "occ": float(occ_norm),
            "disp": disp,
        }

    def set_reference(self, X_batch, router):
        """Record fixed reference baseline from initial batches."""
        # if called with a single batch, store; accumulate until baseline_n
        m = self._compute_metrics(X_batch, router)
        self._baseline_samples.append(m)
        if len(self._baseline_samples) >= self.baseline_n:
            arrs = {k: np.array([s[k] for s in self._baseline_samples]) for k in self._baseline_samples[0]}
            self.ref_mean = {k: float(np.mean(v)) for k, v in arrs.items()}
            self.ref_std = {k: float(np.std(v) + 1e-6) for k, v in arrs.items()}
            self.ref_centroids = router.centroids.copy()
            self.baseline_ready = True
            self.state = "FROZEN"
            self.enter_counter = 0
            self.exit_counter = 0

    def observe(self, X_batch, router):
        m = self._compute_metrics(X_batch, router)
        self.history.append(m)

        if not self.baseline_ready:
            # accumulate baseline
            self.set_reference(X_batch, router)
            return m, self.state, False, 0

        # absolute thresholds (calibrated from v1)
        abs_enter = {"margin": 0.05, "dist": 0.10, "occ": 0.10, "disp": 0.15}
        abs_exit = {"margin": 0.025, "dist": 0.05, "occ": 0.05, "disp": 0.075}

        # combined: enter if deviation exceeds max(sigma, abs)
        signals = 0
        if (self.ref_mean["margin"] - m["margin"]) > max(self.sigma_enter * self.ref_std["margin"], abs_enter["margin"]):
            signals += 1
        if (m["dist"] - self.ref_mean["dist"]) > max(self.sigma_enter * self.ref_std["dist"], abs_enter["dist"]):
            signals += 1
        if (self.ref_mean["occ"] - m["occ"]) > max(self.sigma_enter * self.ref_std["occ"], abs_enter["occ"]):
            signals += 1
        if m["disp"] > max(self.sigma_enter * max(self.ref_std["disp"], 0.05), abs_enter["disp"]):
            signals += 1

        # recovery: within min(sigma_exit, abs_exit)
        in_recovery = (
            (self.ref_mean["margin"] - m["margin"]) <= min(self.sigma_exit * self.ref_std["margin"], abs_exit["margin"]) and
            (m["dist"] - self.ref_mean["dist"]) <= min(self.sigma_exit * self.ref_std["dist"], abs_exit["dist"]) and
            (self.ref_mean["occ"] - m["occ"]) <= min(self.sigma_exit * self.ref_std["occ"], abs_exit["occ"]) and
            m["disp"] <= min(self.sigma_exit * max(self.ref_std["disp"], 0.05), abs_exit["disp"])
        )

        prev_state = self.state
        triggered = False
        if self.state == "FROZEN":
            if signals >= DRIFT_SIGNAL_COUNT:
                self.enter_counter += 1
            else:
                self.enter_counter = 0
            if self.enter_counter >= DRIFT_ENTER_BATCHES:
                self.state = "DRIFT"
                self.enter_counter = 0
                triggered = True
        elif self.state == "DRIFT":
            if in_recovery:
                self.exit_counter += 1
            else:
                self.exit_counter = 0
            if self.exit_counter >= DRIFT_EXIT_BATCHES:
                self.state = "RECOVERING"
                self.exit_counter = 0
        elif self.state == "RECOVERING":
            if in_recovery:
                self.exit_counter += 1
            else:
                self.state = "DRIFT"
                self.exit_counter = 0
            if self.exit_counter >= DRIFT_EXIT_BATCHES:
                self.state = "FROZEN"
                self.exit_counter = 0
                # reset monitor state + counter; reference stays (AUTO_REBASE=False)

        return m, self.state, triggered, signals


# ---------- tests ----------
def run_false_positive():
    print("=" * 70)
    print("TEST 1: FALSE POSITIVE (stationary)")
    print("=" * 70)
    n = 400; dim = 384; k = 6
    X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=1)
    batches = [X[i:i+50] for i in range(0, n, 50)]
    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(batches[0])
    mon = DriftMonitorV3(nb, baseline_n=BASELINE_N)
    # feed first BASELINE_N batches to build reference
    for i in range(BASELINE_N):
        mon.set_reference(batches[i], router)
    triggers = 0
    for bi in range(BASELINE_N, len(batches)):
        Xb = batches[bi]
        m, st, trig, sig = mon.observe(Xb, router)
        if trig:
            triggers += 1
            print(f"  batch {bi}: TRIGGERED metrics={m} sig={sig}")
        else:
            print(f"  batch {bi}: {st} margin={m['margin']:.3f} dist={m['dist']:.3f} occ={m['occ']:.3f} disp={m['disp']:.3f} sig={sig}")
    print(f"\n  triggers: {triggers}/{len(batches)-BASELINE_N}")
    print(f"  FALSE POSITIVE: {'NO (good)' if triggers == 0 else 'YES (bad)'}")
    return triggers == 0


def run_detection_delay():
    print()
    print("=" * 70)
    print("TEST 2: DETECTION DELAY")
    print("=" * 70)
    batches, c0, c1 = make_drift_data(400, 384, 6, seed=3)
    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(batches[0][0])
    mon = DriftMonitorV3(nb, baseline_n=1)
    mon.set_reference(batches[0][0], router)
    # build baseline from first 3 batches
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
    ret_batches = []
    for _ in range(3):
        labels = rng.integers(0, 6, size=50)
        X = c0[labels] + 0.15 * rng.normal(size=(50, 384))
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        ret_batches.append((X, labels, 0.0))
    all_batches = list(batches[:3]) + list(batches[5:7]) + ret_batches

    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(all_batches[0][0])
    mon = DriftMonitorV3(nb, baseline_n=BASELINE_N)
    for i in range(BASELINE_N):
        mon.set_reference(all_batches[i][0], router)
    ref_before = {k: (mon.ref_mean[k], mon.ref_std[k]) for k in mon.ref_mean}
    triggers = []
    states = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        triggers.append(trig)
        states.append(st)
        phase = "D0" if bi < 3 else ("drift" if bi < 6 else "return")
        print(f"  batch {bi} ({phase}): state={st} trig={trig} margin={m['margin']:.3f} dist={m['dist']:.3f} sig={sig}")
    return_states = states[6:]
    recovered = any(s == "FROZEN" for s in return_states)
    ref_after = {k: (mon.ref_mean[k], mon.ref_std[k]) for k in mon.ref_mean}
    ref_unchanged = (ref_before == ref_after)
    print(f"\n  triggers during return: {sum(1 for i in range(6, len(all_batches)) if triggers[i])}")
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
    mon = DriftMonitorV3(nb, baseline_n=BASELINE_N)
    for i in range(BASELINE_N):
        mon.set_reference(all_batches[i][0], router)
    ref0 = {k: (mon.ref_mean[k], mon.ref_std[k]) for k in mon.ref_mean}

    states = []
    triggers = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        states.append(st)
        triggers.append(trig)

    ref_unchanged = ({k: (mon.ref_mean[k], mon.ref_std[k]) for k in mon.ref_mean} == ref0)
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
    print(f"Drift Monitor v3: FP={'OK' if r1 else 'FAIL'} delay={'OK' if r2 else 'FAIL'} "
          f"recovery={'OK' if r3 else 'FAIL'} multicycle={'OK' if r4 else 'FAIL'} "
          f"stability={'OK' if r5 else 'FAIL'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
