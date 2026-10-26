#!/usr/bin/env python3
"""bench_roster_scaling.py — when does the walk start to cost real time?

Two knobs that turn the roster from "free" into "the thing that pays":

  BUCKETS  the walk looks at every row once per routed bucket, so the gather
           cost is rnb x n without a roster and ~|members| with one. rnb=5 is
           today's live setting; this sweeps 5/12/16/24 to find the crossover.
  COLD     on this box the parsed index sits in RAM, so touching 330k rows is
           free. EmptyWorkingSet() pushes the process's pages out, so the next
           pass faults them back from disk - that is the regime where a lookup
           that touches 13x fewer rows must win.

Prints, per arm: latency p50/p95, rows examined, rows scored.
"""
import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_rows_examined import DIR, EXE, SRC, search, wait_health  # noqa: E402
from bench_query_speed import http_json, load_queries, pct  # noqa: E402


def trim_working_set(pid):
    psapi = ctypes.WinDLL("psapi.dll")
    kernel32 = ctypes.WinDLL("kernel32.dll")
    h = kernel32.OpenProcess(0x1F0FFF, False, pid)   # PROCESS_ALL_ACCESS
    if not h:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(h))
    finally:
        kernel32.CloseHandle(h)


def start(port, work, tag, buckets, roster):
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
        "ANN_WALK_PRUNE": "1",
        "ANN_WALK_FLOOR": "0.5",
        "LZ_ROUTE_BUCKETS": str(buckets),
        "ANN_ROSTER": "1" if roster else "0",
        "ANN_PORT": str(port),
    })
    log = open(os.path.join(work, f"server-{tag}.log"), "wb")
    p = subprocess.Popen([EXE, "--serve", str(port)], cwd=DIR, env=env,
                         stdout=log, stderr=log)
    return p, log


def one_pass(port, qs, K):
    lat, scored, modes = [], [], []
    for _sid, v in qs:
        t0 = time.perf_counter()
        o = search(port, v, K)
        lat.append((time.perf_counter() - t0) * 1000.0)
        rt = o.get("route") or {}
        scored.append(rt.get("items_scored", -1))
        modes.append(rt.get("mode", "?"))
    return lat, scored, modes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--buckets", default="5,12,16,24")
    ap.add_argument("--cold", action="store_true", help="also run trimmed passes")
    ap.add_argument("--work", default=r"I:\tmp\ann_scale")
    args = ap.parse_args()

    os.makedirs(args.work, exist_ok=True)
    K = args.topk
    qs = load_queries(args.queries)
    print("=" * 92)
    print("ROSTER SCALING — gather cost vs bucket count, warm and cold")
    print("=" * 92)
    print(f"index={os.path.join(SRC, 'ann_index.jsonl')}  queries={len(qs)}"
          f"  topk={K}  warm_prune=on")

    hdr = (f"{'buckets':>7} {'roster':>6} | {'p50':>7} {'p95':>7} {'mean':>7} |"
           f" {'scored':>7} | {'cold p50':>8} {'cold p95':>8} | {'mode':>9}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for buckets in [int(x) for x in args.buckets.split(",")]:
        for roster in (0, 1):
            tag = f"b{buckets}-r{roster}"
            port = 8230
            p, log = start(port, args.work, tag, buckets, roster)
            try:
                h = wait_health(port, 300)
                if not h:
                    print(f"{buckets:>7} {roster:>6} | server did not come up")
                    continue
                search(port, qs[0][1], K)      # warm the route cache
                one_pass(port, qs[:5], K)      # warm the pages
                lat, scored, modes = one_pass(port, qs, K)
                clat = []
                if args.cold:
                    if trim_working_set(p.pid):
                        clat, _, _ = one_pass(port, qs, K)
                    else:
                        clat = []
                mode = max(set(modes), key=modes.count)
                print(f"{buckets:>7} {roster:>6} | {pct(lat,50):7.2f}"
                      f" {pct(lat,95):7.2f} {statistics.mean(lat):7.2f} |"
                      f" {statistics.mean(scored):7.0f} |"
                      f" {pct(clat,50):8.2f} {pct(clat,95):8.2f} | {mode:>9}")
            finally:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except Exception:
                    pass
                log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
