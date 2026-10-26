#!/usr/bin/env python3
"""bench_query_speed.py — what the roster costs, and what a repeated query costs.

Measured on the LIVE index (data/memcore/ann_index.jsonl), no fixture:

  A. vector path, roster OFF vs ON — two scratch servers, the same query
     vectors, the real /v1/state/search endpoint. This is the search itself
     (the embedder is not in the loop; the ann server never sees text).
  B. repeat, on the live server  — the same vector twice: first touch vs
     second touch, i.e. what the server's query cache actually buys.

A different vector is a different query even when it means the same thing.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_rows_examined import DIR, EXE, SRC, search, wait_health  # noqa: E402

WORDS = None


def pct(xs, p):
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[i]


def http_json(url, body=None, timeout=180):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_queries(n, seed=1234):
    import random
    rows = []
    with open(os.path.join(SRC, "ann_index.jsonl"), "r", encoding="utf-8",
              errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            v = o.get("vec") or o.get("v") or o.get("embedding")
            if v:
                rows.append((o.get("sid"), v))
    rnd = random.Random(seed)
    return rnd.sample(rows, min(n, len(rows)))


def timed_pass(port, qs, K, reps):
    lat, scored, modes = [], [], []
    for rep in range(reps):
        for _sid, v in qs:
            t0 = time.perf_counter()
            o = search(port, v, K)
            lat.append((time.perf_counter() - t0) * 1000.0)
            rt = o.get("route") or {}
            scored.append(rt.get("items_scored", -1))
            modes.append(rt.get("mode", "?"))
    return lat, scored, modes


def start(port, env_extra, tag, work):
    env = dict(os.environ)
    env.update({
        "ANN_INDEX_PATH": os.path.join(SRC, "ann_index.jsonl"),
        "ANN_ANCH_PATH": os.path.join(SRC, "ann_anchors.bin"),
        "ANN_ORDER_PATH": os.path.join(SRC, "ann_index.order"),
        "ANN_ROUTE_LOG": os.path.join(work, f"route-{tag}.log"),
        "ANN_TAGS_PATH": os.path.join(SRC, "ann_tags.tsv"),
        "LZ_EMBED_HOST": "127.0.0.1",
        "LZ_EMBED_PORT": "18095",
        "ANN_TOTEM_WARM": "0",
        "ANN_PORT": str(port),
    })
    env.update(env_extra)
    log = open(os.path.join(work, f"server-{tag}.log"), "wb")
    p = subprocess.Popen([EXE, "--serve", str(port)], cwd=DIR, env=env,
                         stdout=log, stderr=log)
    return p, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--live-port", type=int, default=8096)
    ap.add_argument("--work", default=r"I:\tmp\ann_speed")
    args = ap.parse_args()

    os.makedirs(args.work, exist_ok=True)
    K = args.topk
    qs = load_queries(args.queries)
    print("=" * 78)
    print("QUERY SPEED — roster cost + what a repeat costs")
    print("=" * 78)
    print(f"index={os.path.join(SRC, 'ann_index.jsonl')}  queries={len(qs)}"
          f"  topk={K}  reps={args.reps}  (embedder NOT in this path)")

    res = {}
    for label, port, extra in (("OFF", 8210, {"ANN_ROSTER": "0"}),
                               ("ON", 8211, {"ANN_ROSTER": "1"})):
        p, log = start(port, extra, label.lower(), args.work)
        try:
            h = wait_health(port, 240)
            if not h:
                print(f"[{label}] server did not come up")
                return 1
            search(port, qs[0][1], K)          # warm the route cache
            lat, scored, modes = timed_pass(port, qs, K, args.reps)
            h2 = http_json(f"http://127.0.0.1:{port}/health")
            res[label] = dict(lat=lat, scored=scored, modes=modes, health=h2)
            print(f"\n[{label}] entries={h2.get('entries')} roster={h2.get('roster')}")
            print(f"  latency ms   p50={pct(lat,50):7.2f}  p95={pct(lat,95):7.2f}"
                  f"  mean={statistics.mean(lat):7.2f}  max={max(lat):7.2f}")
            print(f"  items_scored mean={statistics.mean(scored):.0f}"
                  f"  modes={ {m: modes.count(m) for m in set(modes)} }")
        finally:
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                pass
            log.close()

    lo, on = res["OFF"]["lat"], res["ON"]["lat"]
    print("\n  A/B verdict")
    print(f"    items_scored identical: {res['OFF']['scored'] == res['ON']['scored']}")
    print(f"    p50 {pct(lo,50):.2f} -> {pct(on,50):.2f} ms"
          f"   ({pct(on,50)/max(pct(lo,50),1e-9):.2f}x the OFF time)")
    print(f"    p95 {pct(lo,95):.2f} -> {pct(on,95):.2f} ms"
          f"   ({pct(on,95)/max(pct(lo,95),1e-9):.2f}x)")

    # ── B: repeat on the live server ──
    live = args.live_port
    try:
        h = http_json(f"http://127.0.0.1:{live}/health")
    except Exception as e:
        print(f"\nlive :{live} unreachable ({e}) — skipping the repeat test")
        return 0
    print(f"\n[live :{live}] repeat test, {len(qs)} vectors x 2 touches"
          f"  roster={h.get('roster')}")
    c0 = http_json(f"http://127.0.0.1:{live}/v1/canary")["queries"]
    first, _, _ = timed_pass(live, qs, K, 1)
    again, _, _ = timed_pass(live, qs, K, 1)
    c1 = http_json(f"http://127.0.0.1:{live}/v1/canary")["queries"]
    print(f"  first touch  p50={pct(first,50):7.2f}  p95={pct(first,95):7.2f}"
          f"  mean={statistics.mean(first):7.2f}")
    print(f"  same vector  p50={pct(again,50):7.2f}  p95={pct(again,95):7.2f}"
          f"  mean={statistics.mean(again):7.2f}")
    print(f"  cache hit +{c1['cache_hit']-c0['cache_hit']}"
          f"  miss +{c1['cache_miss']-c0['cache_miss']}"
          f"   p50 speedup {pct(first,50)/max(pct(again,50),1e-9):.2f}x")
    print(f"  route warm_taken +{c1['warm_taken']-c0['warm_taken']}"
          f"  skipped_rows +{c1['skipped_rows']-c0['skipped_rows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
