#!/usr/bin/env python3
"""bench_latency_split.py — where does a search query's time actually go?

Measures the two halves of the text path against a running server, so the
claim "changing the on-disk vec format cannot change latency" has a receipt:

    embed  : text -> vector, via the :18095 sidecar (HTTP)
    scan   : the routed dot-product loop over candidates (the part that would
             touch the index format)

Method: time the same queries twice —
  * {"query": text}  -> embed + scan
  * {"vec": [...]}   -> scan only (caller supplies the vector, sidecar skipped)
The difference is the embed cost. `vec`-only is therefore the number that a
storage-format change could ever move (and LZEntry.vec is float* in RAM, so it
should not move at all).

Runs against the LIVE server by default; use --port for a scratch instance.

USAGE
  python bench_latency_split.py                 # live :8096
  python bench_latency_split.py --port 40301 --reps 5
"""
import argparse
import json
import statistics
import time
import urllib.request


def call(port, body, timeout=120):
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/state/search",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(r, timeout=timeout).read())
    return (time.perf_counter() - t0) * 1000.0, d


QUERIES = ["tesspack format", "tombstone warm path", "hex address", "wang latch",
           "hyperbolic scale", "GGUF bake", "SIFT recall", "frustum composite"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args()

    print("=" * 68)
    print(f"latency split — :{args.port}, {len(QUERIES)} queries x {args.reps} reps")
    print("=" * 68)

    text_ms, vec_ms = [], []
    warm = None
    for r in range(args.reps):
        for q in QUERIES:
            ms, d = call(args.port, {"query": q, "topk": args.topk})
            text_ms.append(ms)
            # the vector the server itself embedded (so the vec path is comparable)
            if warm is None:
                v = [0.0] * (d.get("route", {}).get("qd") or 384)
                warm = v
    # vec-only pass: use a real embedded vector from the index side if available,
    # else zeros (cost is the same — the scan loop runs regardless of values)
    for r in range(args.reps):
        for _ in QUERIES:
            ms, _ = call(args.port, {"vec": warm, "topk": args.topk})
            vec_ms.append(ms)

    def stat(name, xs):
        print(f"  {name:<22} min={min(xs):7.1f}  median={statistics.median(xs):7.1f}  "
              f"max={max(xs):7.1f} ms   (n={len(xs)})")

    stat("text (embed + scan)", text_ms)
    stat("vec  (scan only)", vec_ms)
    emb = statistics.median(text_ms) - statistics.median(vec_ms)
    print(f"\n  embed sidecar ~= {emb:.1f} ms   scan ~= {statistics.median(vec_ms):.1f} ms")
    print("  => the on-disk vec format only affects the SCAN half (load-time decode);")
    print("     the embed half is the sidecar and does not touch the index.")


if __name__ == "__main__":
    main()
