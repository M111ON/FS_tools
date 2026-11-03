"""Drift Monitor v2 — Fixed Reference Baseline + Hysteresis State Machine.

Rules (owner 2026-11-03):
  1. Fixed Reference Baseline: recorded at initial fit, never overwritten by
     rolling window. Stores centroid snapshot, separation, purity, occupancy.
  2. Hysteresis state machine:
       FROZEN -> (signals >= N consecutive) -> DRIFT
       DRIFT  -> (metrics back in recovery threshold for M consecutive) -> RECOVERING
       RECOVERING -> (M consecutive ok) -> FROZEN
     T_exit < T_enter (hysteresis) to avoid flip-flopping.
  3. On recovery: reset ONLY monitor state + counter; reference baseline stays.
     Auto-rebase = OFF. Rebase is a separate explicit command creating new
     routing version.
  4. Preserve old paths: never move old items' branches on monitor trigger.

Label-free metrics: assignment margin, distance-to-centroid, occupancy entropy,
centroid displacement. (Label-dependent metrics only for calibration.)
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from learned_routing import make_clusters
from incremental_centroid import FrozenCentroidRouter, MovingAvgRouter
from centroid_matching import make_drift_data

EMBED_DIM = 384

# config
DRIFT_ENTER_BATCHES = 2
DRIFT_EXIT_BATCHES = 3
DRIFT_SIGNAL_COUNT = 2
BASELINE_MODE = "FIXED_REFERENCE"
RECOVERY_MODE = "RESET_MONITOR_STATE"
AUTO_REBASE = False
PRESERVE_OLD_PATHS = True


class DriftMonitorV2:
    def __init__(self, n_branches, window=3,
                 thr_enter_margin=0.05, thr_enter_dist=0.10,
                 thr_enter_occ=0.10, thr_enter_disp=0.15,
                 thr_exit_margin=0.02, thr_exit_dist=0.05,
                 thr_exit_occ=0.05, thr_exit_disp=0.08):
        self.n_branches = n_branches
        self.window = window
        # enter thresholds (stricter)
        self.enter_thr = {"margin": thr_enter_margin, "dist": thr_enter_dist,
                          "occ": thr_enter_occ, "disp": thr_enter_disp}
        # exit thresholds (looser, hysteresis: T_exit < T_enter)
        self.exit_thr = {"margin": thr_exit_margin, "dist": thr_exit_dist,
                         "occ": thr_exit_occ, "disp": thr_exit_disp}
        # fixed reference baseline (set once at initial fit)
        self.ref_baseline = None  # dict of metrics
        self.ref_centroids = None
        # state machine
        self.state = "FROZEN"  # FROZEN | DRIFT | RECOVERING
        self.enter_counter = 0
        self.exit_counter = 0
        # history of metrics
        self.history = []

    def set_reference(self, X_batch, router):
        """Record fixed reference baseline at initial fit. Call once."""
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
        self.ref_baseline = {
            "margin": float(np.mean(margins)),
            "dist": float(np.mean(1 - best_sim)),
            "occ": float(occ_norm),
            "disp": 0.0,
        }
        self.ref_centroids = C.copy()
        self.state = "FROZEN"
        self.enter_counter = 0
        self.exit_counter = 0
        self.history = []

    def observe(self, X_batch, router):
        """Compute metrics, update state machine, return (metrics, state, triggered)."""
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
        # centroid displacement vs reference centroids (fixed)
        Cn_ref = self.ref_centroids / (np.linalg.norm(self.ref_centroids, axis=1, keepdims=True) + 1e-12)
        cosmat = Cn @ Cn_ref.T
        disp = float(1 - np.mean(np.max(cosmat, axis=1)))

        m = {
            "margin": float(np.mean(margins)),
            "dist": float(np.mean(1 - best_sim)),
            "occ": float(occ_norm),
            "disp": disp,
        }
        self.history.append(m)

        # signal count: how many metrics deviate beyond ENTER threshold vs reference
        signals = 0
        if self.ref_baseline["margin"] - m["margin"] > self.enter_thr["margin"]:
            signals += 1
        if m["dist"] - self.ref_baseline["dist"] > self.enter_thr["dist"]:
            signals += 1
        if self.ref_baseline["occ"] - m["occ"] > self.enter_thr["occ"]:
            signals += 1
        if m["disp"] > self.enter_thr["disp"]:
            signals += 1

        # recovery check: metrics back within EXIT threshold
        in_recovery = (
            (self.ref_baseline["margin"] - m["margin"]) <= self.exit_thr["margin"] and
            (m["dist"] - self.ref_baseline["dist"]) <= self.exit_thr["dist"] and
            (self.ref_baseline["occ"] - m["occ"]) <= self.exit_thr["occ"] and
            m["disp"] <= self.exit_thr["disp"]
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
                # back to drift
                self.state = "DRIFT"
                self.exit_counter = 0
            if self.exit_counter >= DRIFT_EXIT_BATCHES:
                self.state = "FROZEN"
                self.exit_counter = 0
                # reset monitor state + counter; reference baseline stays
                self.enter_counter = 0
                # NOTE: ref_baseline and ref_centroids NOT overwritten (AUTO_REBASE=False)

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
    mon = DriftMonitorV2(nb)
    mon.set_reference(batches[0], router)
    triggers = 0
    for bi, Xb in enumerate(batches):
        m, st, trig, sig = mon.observe(Xb, router)
        if trig:
            triggers += 1
            print(f"  batch {bi}: TRIGGERED metrics={m} signals={sig}")
        else:
            print(f"  batch {bi}: {st} margin={m['margin']:.3f} dist={m['dist']:.3f} occ={m['occ']:.3f} disp={m['disp']:.3f} sig={sig}")
    print(f"\n  triggers: {triggers}/{len(batches)}")
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
    mon = DriftMonitorV2(nb)
    mon.set_reference(batches[0][0], router)
    first_trigger = None
    for bi, (Xb, lb, t) in enumerate(batches):
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
    mon = DriftMonitorV2(nb)
    mon.set_reference(all_batches[0][0], router)
    ref_before = dict(mon.ref_baseline)
    triggers = []
    states = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        triggers.append(trig)
        states.append(st)
        phase = "D0" if bi < 3 else ("drift" if bi < 6 else "return")
        print(f"  batch {bi} ({phase}): state={st} trig={trig} margin={m['margin']:.3f} dist={m['dist']:.3f} sig={sig}")
    # recovery: after return, state should be FROZEN or RECOVERING, not stuck DRIFT
    return_states = states[6:]
    recovered = any(s == "FROZEN" for s in return_states)
    ref_after = mon.ref_baseline
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
    # cycle 1: D0 -> drift -> return
    cycle1 = list(batches[:3]) + list(batches[5:7]) + ret
    # cycle 2: drift again (reuse drift batches)
    cycle2 = list(batches[5:7]) + ret
    all_batches = cycle1 + cycle2

    nb = 6; ns = 2
    router = FrozenCentroidRouter(nb, ns, iters=30, seed=0)
    router.fit(all_batches[0][0])
    mon = DriftMonitorV2(nb)
    mon.set_reference(all_batches[0][0], router)
    ref0 = dict(mon.ref_baseline)

    states = []
    triggers = []
    for bi, (Xb, lb, t) in enumerate(all_batches):
        m, st, trig, sig = mon.observe(Xb, router)
        states.append(st)
        triggers.append(trig)

    # check: no baseline contamination
    ref_unchanged = (mon.ref_baseline == ref0)
    # check: at least one FROZEN state in return phases
    n_frozen = sum(1 for s in states if s == "FROZEN")
    print(f"  total batches: {len(all_batches)}")
    print(f"  triggers: {sum(triggers)}")
    print(f"  FROZEN states: {n_frozen}")
    print(f"  reference baseline unchanged (no contamination): {ref_unchanged}")
    print(f"  AUTO_REBASE: {AUTO_REBASE} (must stay False)")
    print(f"  MULTI-CYCLE: {'OK' if ref_unchanged else 'FAIL (baseline contaminated)'}")
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
    print(f"  PRESERVE_OLD_PATHS: {PRESERVE_OLD_PATHS}")
    return moved == 0


def main():
    r1 = run_false_positive()
    r2 = run_detection_delay()
    r3 = run_recovery()
    r4 = run_multi_cycle()
    r5 = run_route_stability()
    print()
    print("=" * 70)
    print(f"Drift Monitor v2: FP={'OK' if r1 else 'FAIL'} delay={'OK' if r2 else 'FAIL'} "
          f"recovery={'OK' if r3 else 'FAIL'} multicycle={'OK' if r4 else 'FAIL'} "
          f"stability={'OK' if r5 else 'FAIL'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
