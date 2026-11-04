"""RCU / epoch reclamation stress test — 1 writer + 1 reader (2 logical CPUs).

Conditions under test (the owner's three):
  1. a reader that began BEFORE a swap finishes on the old index
  2. a reader that begins AFTER a swap sees the new index
  3. the old index is reclaimed only when no reader holds it

Correctness requirements the harness must itself satisfy (the first version of
this test failed because it did not): the epoch bump and the publication must be
ONE atomic step under the same lock, and the reclamation safety check must be
independent of the epoch arithmetic used to make the decision. Here the decision
uses epochs, while the check uses object identity (`holds` holds the actual
Version objects), so the two cannot agree by construction.

Invariants:
  I1 snapshot integrity   checksum of the published version is unchanged across
                          the reader's whole critical section
  I2 generation coherence the object acquired is the object released
  I3 no use-after-reclaim no held version is flagged invalidated
  I4 reclamation safety   no reclaimed version is identical to a held version
  I5 post-swap freshness  a read that acquired after generation g was published
                          must not report a generation older than g

Deliberate deferral test: every 25th read holds its critical section for
HOLD_MS, which is longer than a publish interval, so at least one reclamation
must be deferred. That is what makes condition 3 observable.
"""
import os, sys, time, threading, hashlib
from collections import deque
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import _fvecs
from ico_cost_quality import icosphere, vert_adj

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N = 50_000
BUDGET = 20.0
SWAPS = 10
HOLD_MS = 120.0
HOLD_EVERY = 12


def cs(sizes):
    return hashlib.blake2b(np.ascontiguousarray(sizes).tobytes(), digest_size=8).digest()


class Version:
    def __init__(self, gen, level, assign, v, f):
        self.gen = gen
        self.level = level
        self.v = v
        self.f = f
        self.adj = vert_adj(len(v), f)
        nv = len(v)
        counts = np.bincount(assign, minlength=nv)
        order = np.argsort(assign, kind='stable')
        parts = np.split(order, np.cumsum(counts)[:-1])
        self.cells = {i: (parts[i].astype(np.int32) if len(parts[i])
                          else np.empty(0, np.int32)) for i in range(nv)}
        self.sizes = np.array([len(self.cells[i]) for i in range(nv)], dtype=np.int64)
        self.checksum = cs(self.sizes)
        self.epoch = None
        self.invalidated = False

    def verify(self):
        return cs(self.sizes) == self.checksum


class Reg:
    def __init__(self):
        self.lock = threading.Lock()
        self.epoch = 0
        self.held = {}          # rid -> Version object (or None)

    def enter(self, rid, cur):
        """Atomically read the current version, its epoch, and register the hold."""
        with self.lock:
            ver = cur[0]
            self.held[rid] = ver
            return ver, self.epoch

    def leave(self, rid):
        with self.lock:
            self.held[rid] = None

    def publish(self, ver, cur):
        """Publication and epoch bump are one step."""
        with self.lock:
            self.epoch += 1
            ver.epoch = self.epoch
            cur[0] = ver
            return self.epoch

    def reclaimable(self, live, cur):
        """Versions strictly older than every held epoch, excluding current."""
        with self.lock:
            active = [e for e in [self.held[k].epoch if self.held[k] is not None else None
                                  for k in self.held] if e is not None]
            mn = min(active) if active else self.epoch + 1
            out = [g for g, v in live.items() if v is not cur[0] and v.epoch < mn]
            return out, list(self.held.values())

    def any_held_identity(self, ver, snapshot=None):
        with self.lock:
            vals = snapshot if snapshot is not None else list(self.held.values())
            return any(h is ver for h in vals if h is not None)


def build_version(gen, level, cache, verts, mu, U3, X):
    src = 3 if level == 2 else 2
    vfrom, _ = verts[src]
    vto, _ = verts[level]
    node_from = cache["node_" + str(src)]
    order_pc = cache["order_" + str(src)]
    dv_pc = cache["dv_" + str(src)]
    vf = vfrom.astype(np.float64)
    vt = vto.astype(np.float64)
    P = cache["P"]
    assign = np.empty(len(P), dtype=np.int32)
    for a in range(len(vf)):
        rows = np.nonzero(node_from == a)[0]
        if rows.size == 0:
            continue
        dv_s, o = dv_pc[a], order_pc[a]
        dpv = np.linalg.norm(P[rows] - vf[a], axis=1)
        m = dv_s[1] if len(dv_s) > 1 else np.inf
        skip = 2.0 * dpv < m
        assign[rows[skip]] = a
        keep = rows[~skip]
        if keep.size == 0:
            continue
        k = np.maximum(np.searchsorted(dv_s, 2.0 * dpv[~skip], side='right'), 1)
        for kk in np.unique(k):
            sel = keep[k == kk]
            C = np.sort(o[:kk])
            d = np.linalg.norm(P[sel, None, :] - vt[None, C, :], axis=2)
            assign[sel] = C[np.argmin(d, axis=1)]
    return Version(gen, level, assign, vto, verts[level][1])


def main():
    print("=" * 96)
    print("RCU / EPOCH RECLAMATION STRESS — 1 writer + 1 reader (2 logical CPUs)")
    print("=" * 96)
    X_all = _fvecs(BASE)
    X = X_all[np.random.default_rng(4).choice(len(X_all), size=N, replace=False)]
    qs = X[np.random.default_rng(9).choice(N, size=16, replace=False)]

    verts = {lv: icosphere(lv) for lv in (2, 3)}
    rng = np.random.default_rng(0)
    si = rng.choice(N, min(20000, N), replace=False)
    S = X[si].astype(np.float64)
    mu = S.mean(0).astype(np.float32)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)
    P = (X.astype(np.float32) - mu) @ U3
    P = (P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)).astype(np.float64)

    cache = {"P": P}
    for lv, (v, f) in verts.items():
        vv = v.astype(np.float64)
        cache["node_" + str(lv)] = np.argmax(P @ vv.T, axis=1).astype(np.int32)
        target = verts[3 if lv == 2 else 2][0].astype(np.float64)
        order, dv = [], []
        for a in range(len(vv)):
            d = np.linalg.norm(target - vv[a], axis=1)
            o = np.argsort(d, kind='stable')
            order.append(o); dv.append(d[o])
        cache["order_" + str(lv)] = order
        cache["dv_" + str(lv)] = dv

    PQ = []
    for q in qs:
        p = (q.astype(np.float32) - mu) @ U3
        PQ.append((p / max(float(np.linalg.norm(p)), 1e-9)).astype(np.float32))

    reg = Reg()
    cur = [None]
    state = {"live": {}, "gen": 0, "publish_t": {}}
    viol = {k: 0 for k in ("I1", "I2", "I3", "I4", "I5")}
    st = {"reads": 0, "reclaims": 0, "deferred": 0, "max_live": 1,
          "gen_seen": {}, "crit_ms": [], "hold_reads": 0}
    stop = threading.Event()

    def do_query(ver, pq):
        start = int(np.argmax(ver.v @ pq))
        max_c = int(BUDGET / 100.0 * N)
        a0 = ver.cells[start]
        if len(a0) >= max_c:
            cands = a0[:max_c]
        else:
            parts = [a0]; seen = len(a0); vis = {start}
            qq = deque([start])
            while qq and seen < max_c:
                node = qq.popleft()
                for nb in ver.adj[node]:
                    if nb in vis:
                        continue
                    vis.add(nb); qq.append(nb)
                    a = ver.cells[nb]
                    take = min(len(a), max_c - seen)
                    if take:
                        parts.append(a[:take]); seen += take
                    if seen >= max_c:
                        break
                if seen >= max_c:
                    break
            cands = np.concatenate(parts) if len(parts) > 1 else parts[0]
        if len(cands):
            sub = X[cands].astype(np.int32)
            qf = X[0].astype(np.int32)
            _ = np.argsort(np.einsum('ij,ij->i', sub - qf, sub - qf))[:10]
        return len(cands)

    def reader():
        rid = 1
        while not stop.is_set():
            ver, ep = reg.enter(rid, cur)
            if ver is None:
                reg.leave(rid); time.sleep(0.001); continue
            t_acq = time.perf_counter()
            hold = (st["reads"] % HOLD_EVERY == 0 and st["reads"] > 0)
            try:
                if not ver.verify():
                    viol["I1"] += 1
                g_acq = ver.gen
                do_query(ver, PQ[st["reads"] % len(PQ)])
                if hold:
                    time.sleep(HOLD_MS / 1000.0)      # deliberate slow reader
                    st["hold_reads"] += 1
                if not ver.verify():
                    viol["I1"] += 1
                if ver.invalidated:
                    viol["I3"] += 1               # used after reclaim
                if ver is not cur[0] and g_acq < state["gen"]:
                    # legitimate only if we acquired before that publish finished
                    if t_acq > state["publish_t"].get(g_acq + 1, float("inf")):
                        viol["I5"] += 1
                if reg.held.get(rid) is not ver:
                    viol["I2"] += 1
            finally:
                reg.leave(rid)
            st["crit_ms"].append((time.perf_counter() - t_acq) * 1000)
            st["reads"] += 1
            st["gen_seen"][ver.gen] = st["gen_seen"].get(ver.gen, 0) + 1

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    print(f"N={N}  budget {BUDGET}%  swaps {SWAPS}  "
          f"slow reader every {HOLD_EVERY} reads ({HOLD_MS:.0f} ms hold)")
    print(f"\n{'swap':>4} {'gen':>4} {'level':>5} {'build_ms':>9} {'live':>5} "
          f"{'reclaimed':>11} {'deferred':>9} {'reads':>7}")
    for g in range(1, SWAPS + 1):
        level = 3 if g % 2 == 1 else 2
        t0 = time.perf_counter()
        ver = build_version(g, level, cache, verts, mu, U3, X)
        build_ms = (time.perf_counter() - t0) * 1000

        reg.publish(ver, cur)                 # atomic publish + epoch bump
        state["live"][g] = ver
        state["gen"] = g
        state["publish_t"][g] = time.perf_counter()

        todo, snapshot_held = reg.reclaimable(state["live"], cur)
        done = []
        for gg in todo:
            v = state["live"].pop(gg)
            if reg.any_held_identity(v, snapshot_held):   # independent check
                viol["I4"] += 1
            v.invalidated = True
            done.append(gg)
            st["reclaims"] += 1
        if not done:
            st["deferred"] += 1
        st["max_live"] = max(st["max_live"], len(state["live"]))
        print(f"{g:>4} {g:>4} {level:>5} {build_ms:>9.0f} {len(state['live']):>5} "
              f"{str(done):>11} {'yes' if not done else 'no':>9} {st['reads']:>7}")
        # coverage gate (NOT part of the protocol, only to guarantee every
        # generation is actually read before the next publish)
        t_wait = time.perf_counter()
        while st["gen_seen"].get(g, 0) < 2 and time.perf_counter() - t_wait < 1.0:
            time.sleep(0.005)
        time.sleep(0.02)

    stop.set(); th.join(timeout=5)

    # drain: everything except the current version is reclaimable now
    todo, snapshot_held = reg.reclaimable(state["live"], cur)
    for gg in todo:
        v = state["live"].pop(gg)
        if reg.any_held_identity(v, snapshot_held):
            viol["I4"] += 1
        v.invalidated = True
        st["reclaims"] += 1

    print(f"\nreads served                       : {st['reads']}")
    print(f"reads per generation               : {dict(sorted(st['gen_seen'].items()))}")
    print(f"generations published              : {SWAPS}")
    print(f"versions reclaimed / deferred      : {st['reclaims']} / {st['deferred']}")
    print(f"max simultaneously live versions   : {st['max_live']}")
    print(f"slow (holding) reads exercised     : {st['hold_reads']}")
    print(f"reader critical section p50/p95    : "
          f"{np.percentile(st['crit_ms'],50):.2f} / {np.percentile(st['crit_ms'],95):.2f} ms")

    print("\nINVARIANTS")
    print(f"  I1 snapshot mutated while live         : {viol['I1']}")
    print(f"  I2 acquired object != released object  : {viol['I2']}")
    print(f"  I3 use-after-reclaim                   : {viol['I3']}")
    print(f"  I4 reclaimed while held (identity)     : {viol['I4']}")
    print(f"  I5 reported a generation older than a publish that preceded acquisition"
          f" : {viol['I5']}")
    ok = all(v == 0 for v in viol.values())
    print(f"  -> {'PASS (all zero)' if ok else 'FAIL'}")

    print("\nCONDITIONS")
    gens = st["gen_seen"]
    mid = [g for g in gens if 0 < g < SWAPS]
    print(f"  1 pre-swap reader finishes on old index : "
          f"{all(gens.get(g,0) > 0 for g in range(1, SWAPS))}  (reads per gen above)")
    print(f"  2 post-swap reader sees the new index   : {gens.get(SWAPS, 0) > 0}")
    print(f"  3 reclaim only when unheld              : "
          f"{viol['I4'] == 0 and st['max_live'] >= 2}  "
          f"(max live {st['max_live']} shows deferral happened)")
    print("\n  This box has 2 logical CPUs, so only 1 reader + 1 writer could be")
    print("  exercised; >2 concurrent readers needs a bigger machine (colab).")


if __name__ == '__main__':
    main()