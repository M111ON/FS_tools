#!/usr/bin/env python3
"""D5 — budget vs recall. Independent measurement, OUTSIDE the server logic.

Question: the serve path caps the scored set at `budget` (default 8000, env
LZ_SEARCH_BUDGET). Does that cap cost recall versus an unbounded walk, and
versus the TRUE top-k over the whole index?

Method (no server code trusted):
  1. Load the real index (N x dim). Pick Q query rows (fixed seed) from it.
  2. Oracle: brute-force cosine top-K over ALL N rows (numpy), per query.
  3. For each budget B in a sweep, spawn a SCRATCH ann_search.exe with
     LZ_SEARCH_BUDGET=B, send each query as a raw "vec" (its own embedding, no
     sidecar drift), read hits, and compute recall@k = |truth_k ∩ got_k| / k.
  4. Report the recall curve B -> recall@k. Find where recall flattens and
     whether the default 8000 sits on the flat part.

Nothing writes the live :8096 service or the repo data; scratch servers run on
free ports under --scratch (default I:/tmp/ann_budget).

USAGE
  python bench_budget_recall.py                       # defaults, 40 queries
  python bench_budget_recall.py --queries 80 --ks 1,5,10
  python bench_budget_recall.py --budgets 500,1000,2000,4000,8000,16000,0   # 0 = unbounded
REQUIREMENTS  numpy + build/ann_search.exe + the embed sidecar NOT needed
              (raw vec queries).
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


def wait_health(port, timeout=90.0):
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


def search_vec(port, vec, topk, timeout=60):
    body = json.dumps({"vec": vec, "topk": topk}).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/state/search",
                               data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def spawn(idx, anch, order, log, port, budget, extra_env=None):
    pairs = {
        "ANN_INDEX_PATH": idx,
        "ANN_ANCH_PATH": anch,
        "ANN_ORDER_PATH": order,
        "ANN_ROUTE_LOG": log,
        "ANN_PORT": str(port),
        "LZ_EMBED_HOST": "127.0.0.1",
        "LZ_EMBED_PORT": "18095",
        # match the live keepalive env exactly — the production route is
        # pruned + top-5 buckets + 0.5 floor; a bare spawn measures a
        # different (bucket-0-prefix) walk and is not the served behaviour.
        "ANN_WALK_PRUNE": os.environ.get("ANN_WALK_PRUNE", "1"),
        "ANN_WALK_FLOOR": os.environ.get("ANN_WALK_FLOOR", "0.5"),
        "LZ_ROUTE_BUCKETS": os.environ.get("LZ_ROUTE_BUCKETS", "5"),
    }
    if budget > 0:
        pairs["LZ_SEARCH_BUDGET"] = str(budget)
    if extra_env:
        pairs.update(extra_env)
    os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
    # Launch the exe DIRECTLY with env= — do NOT wrap it in a .bat. A .bat makes
    # cmd.exe the child, so terminate() kills cmd.exe and leaks a resident
    # ann_search.exe per budget (measured: 19 orphans after a few sweeps).
    # Spawning the exe makes the Popen handle the real process, so terminate()
    # reaches it. kill_server() is the belt-and-braces reaper.
    env = {**os.environ, **pairs}
    p = subprocess.Popen([EXE], cwd=ANN, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    h = wait_health(port)
    if h is None:
        kill_server(p, port)
        raise SystemExit(f"scratch server on :{port} (budget={budget}) never became healthy")
    return p, h


def kill_server(p, port=None):
    """Stop a scratch server and make sure the PORT is actually free again.

    terminate() is enough when the exe was spawned directly, but be defensive:
    if the process is still alive after the wait, taskkill the tree by PID
    (and, as a last resort, by the port's owning PID). Idempotent — safe to
    call on an already-dead server. Never touches the live :8096 instance
    (that is a different PID/port)."""
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
    except Exception:
        pass
    if p.poll() is None:                       # still alive -> kill the tree
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        except Exception:
            pass


def load_index(path):
    """Return (sids, np.float32 matrix)."""
    sids = []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            sids.append(o["sid"])
            rows.append(o["vec"])
    return sids, np.asarray(rows, dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    base = os.path.join(ANN, "data", "memcore")
    ap.add_argument("--index", default=os.path.join(base, "ann_index.jsonl"))
    ap.add_argument("--anch", default=os.path.join(base, "ann_anchors.bin"))
    ap.add_argument("--order", default=os.path.join(base, "ann_index.order"))
    ap.add_argument("--scratch", default=r"I:\tmp\ann_budget")
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--ks", default="1,5,10")
    ap.add_argument("--budgets", default="500,1000,2000,4000,8000,16000,0",
                    help="0 = unbounded (full scan)")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    K = max(ks)
    os.makedirs(args.scratch, exist_ok=True)

    print("=" * 72)
    print("D5 — budget vs recall (independent: raw-vec queries vs numpy oracle)")
    print("=" * 72)
    print(f"  index : {args.index}")
    print(f"  loading (N x dim) ...")
    sids, X = load_index(args.index)
    N, dim = X.shape
    print(f"  entries={N}  dim={dim}")

    # normalize once
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)

    rng = np.random.default_rng(args.seed)
    qi = rng.choice(N, size=min(args.queries, N), replace=False)
    Q = Xn[qi]                      # query rows (already unit norm)
    qsids = [sids[i] for i in qi]

    # oracle: brute top-K over ALL entries
    print(f"  oracle: brute cosine top-{K} over all {N} for {len(qi)} queries ...")
    t0 = time.perf_counter()
    sims = Q @ Xn.T                 # (Q, N)
    truth = []
    for r in range(sims.shape[0]):
        idx = np.argpartition(-sims[r], K - 1)[:K]
        idx = idx[np.argsort(-sims[r][idx])]
        truth.append([sids[j] for j in idx])
    print(f"  oracle done in {time.perf_counter()-t0:.1f}s")
    print()

    # scratch copies so a scratch server never touches repo data
    import shutil
    idx = os.path.join(args.scratch, "ann_index.jsonl")
    anch = os.path.join(args.scratch, "ann_anchors.bin")
    order = os.path.join(args.scratch, "ann_index.order")
    if not os.path.exists(idx):
        shutil.copyfile(args.index, idx)
    if not os.path.exists(anch):
        shutil.copyfile(args.anch, anch)
    if not os.path.exists(order):
        shutil.copyfile(args.order, order)

    hdr = "  budget      scored     trunc | " + " | ".join(f"R@{k:<3}" for k in ks)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    curve = {}
    for B in budgets:
        port = free_port()
        log = os.path.join(args.scratch, f"route_b{B}.log")
        if os.path.exists(log):
            os.remove(log)
        # 0 = unbounded. Since the 2026-10-11 widen-trap removal, an unset
        # LZ_SEARCH_BUDGET falls back to the 24000 DEFAULT, not a full scan —
        # so map 0 -> N explicitly or the "unbounded" row silently measures
        # the default and prints scored=24000. (The pruned walk still stops at
        # the covered-entry count; that is the real ceiling, reported honestly
        # as items_scored in the row.)
        Bspawn = N if B == 0 else B
        p, h = spawn(idx, anch, order, log, port, Bspawn)
        hits = {k: 0 for k in ks}
        tot = {k: 0 for k in ks}
        scored_last = 0
        total_last = 0
        trunc_last = 0
        for r, qv in enumerate(Q.tolist()):
            try:
                o = search_vec(port, qv, K)
            except Exception as e:
                continue
            got = [hh.get("sid") for hh in o.get("hits", [])]
            rt = o.get("route", {})
            scored_last = rt.get("items_scored", scored_last)
            total_last = rt.get("items_total", total_last)
            trunc_last = rt.get("truncated", trunc_last)
            for k in ks:
                want = set(truth[r][:k])
                hits[k] += len(want & set(got[:k]))
                tot[k] += len(want)
        kill_server(p, port)
        rec = {k: (hits[k] / tot[k] if tot[k] else 0.0) for k in ks}
        curve[B] = rec
        label = "unbounded" if B == 0 else str(B)
        cells = " | ".join(f"{rec[k]:6.4f}" for k in ks)
        print(f"  {label:<10}  {scored_last:>7}  {total_last:>8}  {trunc_last:>4} | {cells}")

    print()
    # verdict vs default 8000
    if 0 in curve and 8000 in curve:
        d = {k: curve[0][k] - curve[8000][k] for k in ks}
        print("  delta (unbounded - 8000): " +
              "  ".join(f"R@{k}={d[k]:+.4f}" for k in ks))
        worst = max(d.values())
        print(f"  worst recall lost by capping at 8000: {worst:+.4f}")
        print("  => " + ("8000 is on the flat part (cap costs ~nothing)"
                         if abs(worst) < 0.01 else
                         "8000 LOSES recall vs unbounded — cap is loading-bearing"))
    print()


if __name__ == "__main__":
    main()