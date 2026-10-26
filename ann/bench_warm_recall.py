#!/usr/bin/env python3
"""Warm-path measurement - the gate the ANN warm-path card (task-tdb4e839d) asks for.

Question: the tombstone warm path (env ANN_TOMBSTONE_WARM, default OFF) reorders
the walk so entries in already-SHUT buckets are scored first and the scan stops
after them (budget = their count). Two claims must be MEASURED, not asserted:

  C1  Novel-query recall is UNCHANGED by turning the warm path on.
  C2  A warm (repeat / overlapping-route) query scores FEWER items than a cold
      one at the same top-k, and its top-k is IDENTICAL to the warm-OFF baseline.

Method (nothing trusted from the server; raw-vec queries vs a numpy oracle):
  1. Load the real index (N x dim).
  2. Query set A = Q random rows; set B = A + small noise (routes OVERLAP A's but
     are not exact repeats). Oracle = brute cosine top-K over ALL N, per set.
  3. OFF server (fresh route log, warm off): pass over A, then over B -> baseline.
  4. ON server (fresh route log, warm on), ONE process so the latch persists:
       pass1 A  (cold: latch all OPEN)   -> must equal OFF on A
       pass2 A  (exact repeat)           -> warm
       pass3 B  (overlap)                -> warm
  5. Report items_scored + recall@k per pass and assert C1/C2.

Nothing writes the live :8096 service or the repo data; scratch servers run on
free ports under --scratch (default I:/tmp/ann_warm).

USAGE
  python bench_warm_recall.py --queries 60 --seed 1234
  python bench_warm_recall.py --sigmas 0.02,0.05,0.10
REQUIREMENTS  numpy + build/ann_search.exe (embed sidecar NOT needed: raw vec).
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required for the oracle")

HERE = os.path.dirname(os.path.abspath(__file__))
ANN = HERE
for _ in range(3):
    if os.path.exists(os.path.join(ANN, "build", "ann_search.exe")):
        break
    ANN = os.path.dirname(ANN)
ANN = ANN.replace("\\", "/")
EXE = ANN + "/build/ann_search.exe"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(port, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                o = json.loads(r.read())
                if o.get("status") == "ok":
                    return o
        except Exception:
            pass
        time.sleep(0.3)
    return None


def search_vec(port, vec, topk, timeout=120):
    body = json.dumps({"vec": vec, "topk": topk}).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/state/search",
                               data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def spawn(idx, anch, order, log, port, warm, exe=EXE, budget=0, extra_env=None):
    pairs = {
        "ANN_INDEX_PATH": idx,
        "ANN_ANCH_PATH": anch,
        "ANN_ORDER_PATH": order,
        "ANN_ROUTE_LOG": log,
        "LZ_EMBED_HOST": "127.0.0.1",
        "LZ_EMBED_PORT": "18095",
        # match the live keepalive env (production route: pruned + top-5 buckets)
        "ANN_WALK_PRUNE": os.environ.get("ANN_WALK_PRUNE", "1"),
        "ANN_WALK_FLOOR": os.environ.get("ANN_WALK_FLOOR", "0.5"),
        "LZ_ROUTE_BUCKETS": os.environ.get("LZ_ROUTE_BUCKETS", "5"),
        "ANN_TOMBSTONE_WARM": "1" if warm else "0",
        "ANN_PORT": str(port),
    }
    if budget > 0:
        pairs["LZ_SEARCH_BUDGET"] = str(budget)
    if extra_env:
        pairs.update(extra_env)
    os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
    env = {**os.environ, **pairs}
    errf = open(log + ".stderr", "wb")
    p = subprocess.Popen([exe, "--serve", str(port)], cwd=ANN, env=env, stdout=errf, stderr=subprocess.STDOUT)
    h = wait_health(port, timeout=180.0)
    if h is None:
        kill_server(p, port)
        try: errf.close()
        except Exception: pass
        tail = ""
        try: tail = open(log + ".stderr", encoding="utf-8", errors="replace").read()[-500:]
        except Exception: pass
        raise SystemExit(f"scratch server on :{port} (warm={warm}) never became healthy; stderr={tail!r}")
    return p, h


def kill_server(p, port=None):
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
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


def load_index(path):
    sids, rows = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            sids.append(o["sid"])
            rows.append(o["vec"])
    return sids, np.asarray(rows, dtype=np.float32)


def oracle(Q, Xn, sids, K):
    sims = Q @ Xn.T
    out = []
    for r in range(sims.shape[0]):
        idx = np.argpartition(-sims[r], K - 1)[:K]
        idx = idx[np.argsort(-sims[r][idx])]
        out.append([sids[j] for j in idx])
    return out


def run_pass(port, vecs, K):
    """Return list of (items_scored, mode, [sids])."""
    out = []
    for v in vecs:
        o = search_vec(port, v, K)
        rt = o.get("route", {})
        out.append((rt.get("items_scored", -1),
                    rt.get("mode", "?"),
                    [h.get("sid") for h in o.get("hits", [])]))
    return out


def recall(truth, got, k):
    h = t = 0
    for i in range(len(truth)):
        want = set(truth[i][:k])
        h += len(want & set(got[i][:k]))
        t += len(want)
    return h / t if t else 0.0


def main():
    ap = argparse.ArgumentParser()
    base = os.path.join(ANN, "data", "memcore")
    ap.add_argument("--index", default=os.path.join(base, "ann_index.jsonl"))
    ap.add_argument("--anch", default=os.path.join(base, "ann_anchors.bin"))
    ap.add_argument("--order", default=os.path.join(base, "ann_index.order"))
    ap.add_argument("--scratch", default=r"I:\tmp\ann_warm")
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--ks", default="1,5,10")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--sigmas", default="0.05",
                    help="noise sigma for the overlapping-route query set")
    ap.add_argument("--exe", default=EXE,
                    help="ann_search binary to spawn (default: build/ann_search.exe)")
    ap.add_argument("--budget", type=int, default=0,
                    help="LZ_SEARCH_BUDGET for the scratch servers; 0 = server default")
    ap.add_argument("--min-keep", type=float, default=0.90,
                    help="gate: a warm fire may not keep less than this fraction of "
                         "the cold walk (must match the server's ANN_WARM_KEEP, "
                         "default 90 -> 0.90)")
    args = ap.parse_args()

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    sigmas = [float(x) for x in args.sigmas.split(",") if x.strip()]
    K = max(ks)
    os.makedirs(args.scratch, exist_ok=True)

    print("=" * 80)
    print("WARM-PATH measurement - novel recall unchanged + warm scores fewer")
    print("=" * 80)
    sids, X = load_index(args.index)
    N, dim = X.shape
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
    print(f"  index={N} dim={dim} budget={args.budget or 'server default'}")

    rng = np.random.default_rng(args.seed)
    qi = rng.choice(N, size=min(args.queries, N), replace=False)
    A = Xn[qi]                                   # novel queries
    print(f"  oracle A ({len(qi)} novel) ...")
    truthA = oracle(A, Xn, sids, K)

    # overlapping set per sigma - each needs its OWN oracle
    sets = {}
    for sigma in sigmas:
        B = A + rng.normal(0, sigma, size=A.shape).astype(np.float32)
        B = (B / (np.linalg.norm(B, axis=1, keepdims=True) + 1e-12)).astype(np.float32)
        print(f"  oracle B sigma={sigma} ...")
        sets[sigma] = (B, oracle(B, Xn, sids, K))
    print()

    import shutil
    idx = os.path.join(args.scratch, "ann_index.jsonl")
    anch = os.path.join(args.scratch, "ann_anchors.bin")
    order = os.path.join(args.scratch, "ann_index.order")
    for src, dst in ((args.index, idx), (args.anch, anch), (args.order, order)):
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)

    # -- OFF server: baselines (fresh log, warm off) ------------------------
    log = os.path.join(args.scratch, "route_off.log")
    if os.path.exists(log):
        os.remove(log)
    port = free_port()
    p, _ = spawn(idx, anch, order, log, port, warm=False, exe=args.exe, budget=args.budget)
    offA = run_pass(port, A.tolist(), K)
    offB = {s: run_pass(port, sets[s][0].tolist(), K) for s in sigmas}
    kill_server(p, port)

    # -- ON server: ONE process so the latch persists across passes ---------
    log = os.path.join(args.scratch, "route_on.log")
    if os.path.exists(log):
        os.remove(log)
    port = free_port()
    p, _ = spawn(idx, anch, order, log, port, warm=True, exe=args.exe, budget=args.budget)
    onA_cold = run_pass(port, A.tolist(), K)          # latch all OPEN
    onA_rep = run_pass(port, A.tolist(), K)           # exact repeat -> warm
    onB = {s: run_pass(port, sets[s][0].tolist(), K) for s in sigmas}
    kill_server(p, port)

    # -- report -------------------------------------------------------------
    def line(tag, rows, truth):
        sc = [r[0] for r in rows]
        modes = {}
        for r in rows:
            modes[r[1]] = modes.get(r[1], 0) + 1
        got = [r[2] for r in rows]
        rec = {k: recall(truth, got, k) for k in ks}
        print(f"  {tag:<16} scored mean={np.mean(sc):9.1f} max={max(sc):8d} modes={modes}")
        print(f"  {'':<16} " + "  ".join(f"R@{k}={rec[k]:.4f}" for k in ks))
        return rec, float(np.mean(sc)), got

    print("-- A: novel set - warm OFF vs warm ON first touch (C1) --")
    recA_off, scA_off, gotA_off = line("OFF  A", offA, truthA)
    recA_cold, scA_cold, gotA_cold = line("ON   A cold", onA_cold, truthA)
    print(f"  C1 recall identical : {recA_off == recA_cold}")
    print(f"  C1 top-k identical  : "
          f"{all(set(a[:K]) == set(b[:K]) for a, b in zip(gotA_off, gotA_cold))}")
    print(f"  C1 scored identical : {scA_off == scA_cold}  "
          f"({scA_off:.1f} vs {scA_cold:.1f})\n")

    print("-- A: exact repeat (C2) --")
    recA_rep, scA_rep, gotA_rep = line("ON   A repeat", onA_rep, truthA)
    print(f"  C2 repeat scores fewer : {scA_rep < scA_cold}  "
          f"({scA_rep:.1f} vs {scA_cold:.1f})")
    print(f"  C2 repeat top-k == cold: "
          f"{all(set(a[:K]) == set(b[:K]) for a, b in zip(gotA_rep, gotA_cold))}\n")

    for sigma in sigmas:
        print(f"-- B sigma={sigma}: overlapping routes (C2) --")
        recB_off, scB_off, gotB_off = line("OFF  B", offB[sigma], sets[sigma][1])
        recB_w, scB_w, gotB_w = line("ON   B warm", onB[sigma], sets[sigma][1])
        print(f"  C2 overlap scores fewer : {scB_w < scB_off}  "
              f"({scB_w:.1f} vs {scB_off:.1f})")
        print("  C2 overlap recall kept  : "
              + "  ".join(f"R@{k} {recB_off[k]:.4f}->{recB_w[k]:.4f}" for k in ks))
        print("  C2 overlap top-k == OFF : "
              f"{all(set(a[:K]) == set(b[:K]) for a, b in zip(gotB_w, gotB_off))}\n")

    print("Note: 'fewer items at the same top-k' is the gate. A literal exact")
    print("repeat routes to the buckets the previous run just shut, so the SHUT set")
    print("equals the walked set and there is nothing to skip; the gain appears on")
    print("queries whose route OVERLAPS previously-walked buckets (the B set).")

    # ── cut-bound gate (added 2026-10-26) ─────────────────────────────────
    # The 2026-10-14 numbers were taken when a bucket was 24-30% of the index,
    # so the warm cut was 0.4% and recall could not move. At K=64 a bucket is
    # ~3%: the same cut reached 20-80% of the route and recall fell with it
    # (R@5 .9333 -> .9100 on the A set). The server now refuses a cut that
    # would keep less than ANN_WARM_KEEP percent (default 90) of the cold
    # walk; this asserts the bound from the outside, on every fire.
    print()
    failed = 0
    for tag, cold, warm in (("A", offA, onA_cold),
                            *[(f"B{sigma}", offB[sigma], onB[sigma]) for sigma in sigmas]):
        fires = [(c[0], w[0]) for c, w in zip(cold, warm)
                 if w[1] == "warm+p" and c[0] > 0 and w[0] > 0]
        if not fires:
            print(f"  GATE {tag:<5} warm fired 0/{len(cold)} - bound not exercised")
            continue
        worst = min(w / c for c, w in fires)
        ok = worst >= args.min_keep
        if not ok:
            failed += 1
        print(f"  GATE {tag:<5} warm fired {len(fires)}/{len(cold)}  min kept={worst:.3f}"
              f"  (bound {args.min_keep:.2f})  {'PASS' if ok else 'FAIL'}")
    print(f"  GATE cut-bound: {'PASS' if failed == 0 else 'FAIL'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
