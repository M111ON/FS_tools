#!/usr/bin/env python3
"""Latch A/B — the budget-limited, snapshot-pinned measurement the phase-2
card (task-tcce8f5f2) asks for, replacing the run whose gate never fired.

Why this file exists (review 2026-10-25):
  * the previous A/B reported items_scored 62,934 == the whole index on BOTH
    arms. That is not "the latch did nothing", it is the FULL-SCAN fallback:
    a scratch copy taken while the live index was growing pairs the index of
    one moment with anchors/order of another (antr != nent), the route layer
    refuses the pair, and every query scores every row. Both arms are brute.
    => snapshot() below copies only a CONSISTENT triple (ntr == order n ==
       index rows), re-checks the source did not move during the copy, and
       computes the anchor checksum. No snapshot, no measurement.
  * recall 1.0000 -> 1.0000 saturates: querying a row with itself is trivial.
    => --hard builds queries BETWEEN rows (mean of two rows + noise), so the
       route is ambiguous and over-skipping shows up as a recall drop.
  * G2.6 (latch != semantic): the parity pairing (b ^ 1) is a fact about
    k-means ids, which carry no spatial meaning. ANN_PAIR now selects
    centroid | parity | own, so the choice is MEASURED, not argued.
  * a single run is not a latency result => --runs 5, report per-run + median.

Arms (each its own process, own fresh route log, same frozen snapshot):
    OFF                warm path off (baseline)
    ON own             warm on, partner = none (own bucket only)
    ON parity          warm on, partner = cell^1 (the rejected choice)
    ON centroid        warm on, partner = nearest centroid (G2.6 default)

Per arm: pass 1 = hard queries (cold), pass 2 = overlapping queries (warm).

Gates printed by the run:
    VALID     median scored(cold) < 0.9 * N and no fallback on the majority
    G2.2      median scored(warm) < median scored(cold)
    G2.3      R@k(warm) does not drop beyond --tol vs OFF on the same queries
    G2.6      centroid pairing is not worse than parity (scored <=, recall >=)
    COST      p50 latency(warm) vs p50(cold), median over runs

USAGE
  python bench_latch_ab.py                       # 5 runs, 40 hard queries
  python bench_latch_ab.py --runs 1 --queries 12 --verbose
REQUIREMENTS numpy + build/ann_search.exe (or --exe).
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required (the oracle must not come from the server)")

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(port, path, body=None, timeout=180):
    url = f"http://127.0.0.1:{port}{path}"
    if body is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(port, timeout=240.0):
    """The index is ready when health reports entries; embed may be down
    (we always pass raw vectors, so the sidecar is not on the path)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = http_json(port, "/health", timeout=3)
            if int(h.get("entries") or 0) > 0:
                return h
        except Exception:
            pass
        time.sleep(0.3)
    return None


# ── snapshot: a CONSISTENT triple or nothing ─────────────────────────────

def anchors_header(path):
    with open(path, "rb") as f:
        b = f.read(20)
    if len(b) < 20:
        return None
    magic, K, dim, ntr, cksum = struct.unpack("<5I", b)
    return {"magic": magic, "K": K, "dim": dim, "ntr": ntr, "cksum": cksum}


def order_header(path):
    try:
        with open(path, "rb") as f:
            b = f.read(20)
    except FileNotFoundError:
        return None
    if len(b) < 20:
        return None
    magic, K, n, tck, pck = struct.unpack("<5I", b)
    return {"magic": magic, "K": K, "n": n, "table_cksum": tck, "perm_cksum": pck}


def count_rows(path):
    n = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 22)
            if not chunk:
                break
            n += chunk.count(b"\n")
    return n


def stat_sig(path):
    st = os.stat(path)
    return (st.st_size, int(st.st_mtime))


def snapshot(src_idx, src_anch, src_order, dst_dir, tries=6):
    """Build a snapshot that is CONSISTENT BY CONSTRUCTION.

    The live index appends while the anchor table is rewritten on regrow, so a
    blind copy can pair index-of-now with anchors-of-a-minute-ago (the exact
    failure that made the previous A/B score every row). The anchor header
    states how many rows the table covers (ntr): that is the coherent PREFIX.
    So: read ntr, copy only the first ntr complete lines of the index, copy
    anchors, then re-verify the header did not move during the copy. The order
    sidecar is optional — in labels=derive mode the server derives the walk
    order from the rows and never reads it.
    """
    os.makedirs(dst_dir, exist_ok=True)
    d_idx = os.path.join(dst_dir, os.path.basename(src_idx))
    d_anch = os.path.join(dst_dir, os.path.basename(src_anch))
    d_ord = os.path.join(dst_dir, os.path.basename(src_order))
    last = ""
    for attempt in range(1, tries + 1):
        ah0 = anchors_header(src_anch)
        if ah0 is None or ah0["ntr"] <= 0:
            last = "anchors header unreadable"
            time.sleep(1.0)
            continue
        ntr = ah0["ntr"]
        # copy anchors first, then take the index prefix that table covers
        a_sig0 = stat_sig(src_anch)
        shutil.copyfile(src_anch, d_anch)
        a_sig1 = stat_sig(src_anch)
        ah1 = anchors_header(d_anch)
        if a_sig0 != a_sig1 or ah1 is None or ah1["ntr"] != ntr:
            last = "anchors moved during copy"
            time.sleep(1.0)
            continue
        rows = 0
        with open(src_idx, "rb") as fi, open(d_idx, "wb") as fo:
            for line in fi:
                if rows >= ntr:
                    break
                if line.strip():
                    fo.write(line)
                    rows += 1
        if rows != ntr:
            last = f"index prefix short: {rows} < ntr {ntr}"
            time.sleep(1.0)
            continue
        # order sidecar: keep it only when it covers exactly this prefix
        ord_n = None
        oh = order_header(src_order)
        if oh is not None and oh["n"] == ntr:
            shutil.copyfile(src_order, d_ord)
            ord_n = order_header(d_ord)["n"] if order_header(d_ord) else None
            if ord_n != ntr:
                try:
                    os.remove(d_ord)
                except OSError:
                    pass
                ord_n = None
        else:
            try:
                os.remove(d_ord)
            except OSError:
                pass
        h = hashlib.sha256()
        with open(d_idx, "rb") as f:
            h.update(f.read(1 << 20))
        meta = {
            "rows": rows, "K": ah1["K"], "dim": ah1["dim"], "ntr": ntr,
            "anchors_cksum": ah1["cksum"], "order_n": ord_n,
            "index_bytes": os.path.getsize(d_idx),
            "index_head_sha256": h.hexdigest()[:16],
        }
        return [d_idx, d_anch, d_ord], meta
    raise SystemExit(f"snapshot refused after {tries} tries: {last}")


# ── queries + oracle ─────────────────────────────────────────────────────

def load_rows(path, limit=None):
    sids, rows = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            sids.append(o["sid"])
            rows.append(o["vec"])
            if limit and len(rows) >= limit:
                break
    X = np.asarray(rows, dtype=np.float32)
    return sids, X


def norm(a):
    return a / (np.linalg.norm(a, axis=-1, keepdims=True) + 1e-12)


def oracle(Q, Xn, sids, K):
    sims = Q @ Xn.T
    out = []
    for r in range(sims.shape[0]):
        idx = np.argpartition(-sims[r], K - 1)[:K]
        idx = idx[np.argsort(-sims[r][idx])]
        out.append([sids[j] for j in idx])
    return out


# ── server ───────────────────────────────────────────────────────────────

def spawn(paths, log, port, exe, warm, pair, budget, buckets, extra=None):
    idx, anch, order = paths
    env = {**os.environ}
    env.update({
        "ANN_INDEX_PATH": idx,
        "ANN_ANCH_PATH": anch,
        "ANN_ORDER_PATH": order,
        "ANN_ROUTE_LOG": log,
        "ANN_PORT": str(port),
        "ANN_TOMBSTONE_WARM": "1" if warm else "0",
        "ANN_PAIR": pair,
        "LZ_SEARCH_BUDGET": str(budget),
        "LZ_ROUTE_BUCKETS": str(buckets),
        # the route floor is what turned the previous run into a full scan;
        # keep the production setting so the gate is measured as shipped.
        "ANN_WALK_PRUNE": os.environ.get("ANN_WALK_PRUNE", "1"),
        "ANN_WALK_FLOOR": os.environ.get("ANN_WALK_FLOOR", "0.5"),
    })
    if extra:
        env.update(extra)
    err = open(log + ".stderr", "wb")
    p = subprocess.Popen([exe, "--serve", str(port)], cwd=HERE, env=env,
                         stdout=err, stderr=subprocess.STDOUT)
    h = wait_ready(port)
    if h is None:
        kill(p)
        tail = ""
        try:
            tail = open(log + ".stderr", encoding="utf-8", errors="replace").read()[-400:]
        except Exception:
            pass
        raise SystemExit(f"server on :{port} (warm={warm} pair={pair}) never became ready: {tail!r}")
    return p, h


def kill(p):
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:
                pass
    except Exception:
        pass
    if p.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            pass


def run_pass(port, vecs, K):
    rows = []
    for v in vecs:
        t0 = time.perf_counter()
        o = http_json(port, "/v1/state/search", {"vec": v, "topk": K})
        ms = (time.perf_counter() - t0) * 1000.0
        rt = o.get("route", {}) or {}
        rows.append({
            "scored": int(rt.get("items_scored", -1)),
            "total": int(rt.get("items_total", -1)),
            "budget": int(rt.get("budget", -1)),
            "trunc": int(rt.get("truncated", 0)),
            "fall": int(rt.get("fallback", 0)),
            "mode": rt.get("mode", "?"),
            "buckets": rt.get("buckets", []),
            "pair": (rt.get("fog") or {}).get("pair", -1),
            "ms": ms,
            "sids": [x.get("sid") for x in (o.get("hits") or [])],
        })
    return rows


def recall(truth, got, k):
    h = t = 0
    for i in range(len(truth)):
        want = set(truth[i][:k])
        h += len(want & set(got[i][:k]))
        t += len(want)
    return h / t if t else 0.0


def summarize(rows, truth, ks):
    sc = np.array([r["scored"] for r in rows], dtype=float)
    ms = np.array([r["ms"] for r in rows], dtype=float)
    modes = {}
    for r in rows:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    got = [r["sids"] for r in rows]
    return {
        "scored_med": float(np.median(sc)),
        "scored_mean": float(np.mean(sc)),
        "scored_max": int(sc.max()) if len(sc) else -1,
        "p50_ms": float(np.median(ms)),
        "trunc": sum(r["trunc"] for r in rows),
        "fall": sum(r["fall"] for r in rows),
        "modes": modes,
        "recall": {k: recall(truth, got, k) for k in ks},
        "sids": got,
        "raw": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    base = os.path.join(HERE, "data", "memcore")
    ap.add_argument("--index", default=os.path.join(base, "ann_index.jsonl"))
    ap.add_argument("--anch", default=os.path.join(base, "ann_anchors.bin"))
    ap.add_argument("--order", default=os.path.join(base, "ann_index.order"))
    ap.add_argument("--scratch", default=r"I:\tmp\ann_latch_ab")
    ap.add_argument("--exe", default=os.path.join(HERE, "build", "ann_search.exe"))
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--budget", type=int, default=24000, help="LZ_SEARCH_BUDGET (hard cap)")
    ap.add_argument("--buckets", type=int, default=5, help="LZ_ROUTE_BUCKETS walked per query")
    ap.add_argument("--ks", default="1,5,10")
    ap.add_argument("--hard-sigma", type=float, default=0.35,
                    help="noise on the between-rows query (0 = pure midpoint)")
    ap.add_argument("--overlap-sigma", type=float, default=0.06,
                    help="noise that makes set B route OVER set A's buckets")
    ap.add_argument("--tol", type=float, default=0.02, help="allowed recall drop (G2.3)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    K = max(ks)
    if not os.path.exists(args.exe):
        sys.exit(f"missing binary: {args.exe}")

    print("=" * 78)
    print("LATCH A/B — budget-limited, snapshot-pinned (phase 2, 3-axis latch)")
    print("=" * 78)
    paths, meta = snapshot(args.index, args.anch, args.order,
                           os.path.join(args.scratch, "snap"))
    N = meta["rows"]
    print(f"  snapshot : rows={N} K={meta['K']} dim={meta['dim']} "
          f"anchors_ntr={meta['ntr']} order_n={meta['order_n']}")
    print(f"             index_bytes={meta['index_bytes']} "
          f"head_sha256={meta['index_head_sha256']}")
    print(f"  budget   : {args.budget}   buckets/query: {args.buckets}   "
          f"queries: {args.queries}   runs: {args.runs}")

    sids, X = load_rows(paths[0])
    assert len(sids) == N, f"row count moved while loading ({len(sids)} vs {N})"
    Xn = norm(X).astype(np.float32)

    rng = np.random.default_rng(args.seed)
    qi = rng.choice(N, size=min(args.queries, N), replace=False)
    qi2 = rng.choice(N, size=len(qi), replace=False)
    # HARD set: a point BETWEEN two rows — the route is genuinely ambiguous,
    # so over-skipping cannot hide behind a saturated recall.
    H = 0.5 * (Xn[qi] + Xn[qi2]) + rng.normal(0, args.hard_sigma, size=(len(qi), X.shape[1])).astype(np.float32)
    H = norm(H).astype(np.float32)
    # B: OVERLAPS A's route (near the A rows) — the only shape where the
    # warm path has something to skip (an exact repeat routes to the buckets
    # it just shut, so warm == walked and there is nothing to gain).
    B = norm(Xn[qi] + rng.normal(0, args.overlap_sigma, size=Xn[qi].shape).astype(np.float32)).astype(np.float32)
    print(f"  oracle   : H ({len(H)} between-rows) + B ({len(B)} overlapping) ...")
    truthH = oracle(H, Xn, sids, K)
    truthB = oracle(B, Xn, sids, K)
    print()

    arms = [
        ("OFF",        False, "centroid"),
        ("ON own",     True,  "own"),
        ("ON parity",  True,  "parity"),
        ("ON centroid", True, "centroid"),
    ]
    SEQ = H.tolist() + B.tolist()          # one sequence, served in this order
    TRU = truthH + truthB
    nA = len(H)

    runs = []
    for run in range(1, args.runs + 1):
        print(f"--- run {run}/{args.runs} ---")
        per_arm = {}
        for name, warm, pair in arms:
            log = os.path.join(args.scratch, f"route_{name.replace(' ', '_')}_{run}.log")
            if os.path.exists(log):
                os.remove(log)
            port = free_port()
            p, h = spawn(paths, log, port, args.exe, warm, pair, args.budget, args.buckets)
            pair_n = (h.get("fog") or {}).get("pair", -1)
            try:
                rows = run_pass(port, SEQ, K)
            finally:
                kill(p)
            blocks = {
                "A": summarize(rows[:nA], truthH, ks),
                "B": summarize(rows[nA:], truthB, ks),
            }
            per_arm[name] = blocks
            warm_hits = sum(1 for r in rows[nA:] if "warm" in r["mode"])
            print(f"  {name:<12} pair={pair_n:<4} "
                  f"A scored={blocks['A']['scored_med']:7.0f} R@1={blocks['A']['recall'][1]:.4f} "
                  f"fall={blocks['A']['fall']} trunc={blocks['A']['trunc']} | "
                  f"B scored={blocks['B']['scored_med']:7.0f} R@1={blocks['B']['recall'][1]:.4f} "
                  f"R@5={blocks['B']['recall'][5]:.4f} R@10={blocks['B']['recall'][10]:.4f} "
                  f"trunc={blocks['B']['trunc']} "
                  f"warm-q={warm_hits}/{len(B)} p50={blocks['B']['p50_ms']:5.1f}ms")
        runs.append(per_arm)

    def med(arm, blk, key):
        return float(np.median([r[arm][blk][key] for r in runs]))

    def med_rec(arm, blk, k):
        return float(np.median([r[arm][blk]["recall"][k] for r in runs]))

    print()
    print("=" * 78)
    print("GATES  (OFF is the baseline over the SAME query sequence, so position")
    print("        effects cancel: the ON arms engage the latch from query 2 on)")
    print("=" * 78)
    offA = med("OFF", "A", "scored_med")
    offFall = med("OFF", "A", "fall")
    valid = offA < 0.9 * N and offFall < max(1, nA * 0.5)
    print(f"  VALID  bucket walk, not brute (OFF block-A scored {offA:.0f} < 0.9*N="
          f"{0.9 * N:.0f}, fallback median {offFall:.1f}) : "
          f"{'PASS' if valid else 'FAIL — the gate did not fire, this run proves nothing'}")

    for name in ("ON own", "ON parity", "ON centroid"):
        w = med(name, "B", "scored_med")
        c = med("OFF", "B", "scored_med")
        print(f"  G2.2  {name:<12} B warm {w:7.0f} vs OFF {c:7.0f} "
              f"({(w / c - 1) * 100:+.1f}%) : {'PASS' if w < c else 'FAIL'}")

    print()
    print("  arm          A: R@1/5/10            B: R@1/5/10            "
          "p50 A / B (ms)")
    for name, _, _ in arms:
        a = " / ".join(f"{med_rec(name, 'A', k):.3f}" for k in ks)
        b = " / ".join(f"{med_rec(name, 'B', k):.3f}" for k in ks)
        print(f"  {name:<12} {a:<22} {b:<22} "
              f"{med(name, 'A', 'p50_ms'):5.1f} / {med(name, 'B', 'p50_ms'):5.1f}")

    g23 = True
    for k in ks:
        off = med_rec("OFF", "B", k)
        on = med_rec("ON centroid", "B", k)
        if on < off - args.tol:
            g23 = False
        print(f"  G2.3  B R@{k}: OFF {off:.4f} vs ON centroid {on:.4f} "
              f"(tol {args.tol}) : {'PASS' if on >= off - args.tol else 'FAIL'}")
    print(f"  G2.3  overall (block B) : {'PASS' if g23 else 'FAIL'}")

    # G2.6: centroid pairing must not be worse than parity on the same work
    par_w = med("ON parity", "B", "scored_med")
    cen_w = med("ON centroid", "B", "scored_med")
    par_r = med_rec("ON parity", "B", 1)
    cen_r = med_rec("ON centroid", "B", 1)
    g26 = cen_w <= par_w * 1.02 and cen_r >= par_r - args.tol
    print(f"  G2.6  centroid vs parity: B scored {cen_w:.0f} vs {par_w:.0f}, "
          f"R@1 {cen_r:.4f} vs {par_r:.4f} : {'PASS' if g26 else 'FAIL'}")

    # COST: warm path latency against the SAME block on the OFF arm
    for name in ("ON own", "ON parity", "ON centroid"):
        print(f"  COST  {name:<12} p50 B = {med(name, 'B', 'p50_ms'):.1f} ms "
              f"vs OFF {med('OFF', 'B', 'p50_ms'):.1f} ms "
              f"({med(name, 'B', 'p50_ms') / max(med('OFF', 'B', 'p50_ms'), 1e-9):.3f}x)")

    print()
    print(f"  G2.4 (isometric view receipt) remains an OWNER decision — not measured here.")
    print(f"  snapshot pinned for every run: rows={N} anchors_cksum={meta['anchors_cksum']}")


if __name__ == "__main__":
    main()
