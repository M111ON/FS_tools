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


def embed_queries(embed_port, queries):
    """Embed each query through the sidecar, exactly as the server does.

    The server's text path is query -> sidecar -> vector -> route. To measure the
    scan half alone we need the same vector the server would have produced, so
    we ask the same sidecar. A sidecar that cannot be reached is a hard error:
    falling back to zeros would silently measure an idle server.
    """
    out = []
    for q in queries:
        body = json.dumps({"input": [q], "model": "e5-small"}).encode()
        r = urllib.request.Request(f"http://127.0.0.1:{embed_port}/v1/embeddings",
                                   data=body, headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(r, timeout=120).read())
        v = d["data"][0]["embedding"]
        if not v or not any(v):
            raise SystemExit(f"FATAL: sidecar returned a zero vector for {q!r} — "
                             f"the scan measurement would be void.")
        out.append(v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--embed-port", type=int, default=18095)
    args = ap.parse_args()

    print("=" * 68)
    print(f"latency split — :{args.port}, {len(QUERIES)} queries x {args.reps} reps")
    print("=" * 68)

    text_ms, vec_ms = [], []
    # The vec-only pass must use the SAME query's real embedding. A zero vector
    # makes the route miss every bucket, so the server scores 0 items and the
    # "scan" number becomes the cost of doing nothing (measured 2026-10-19:
    # zero-vec p50 = 1.5 ms with items_scored=0 vs real-vec p50 = 60 ms with
    # items_scored=24000). Any latency claim from a zero vector is void.
    real_vecs = embed_queries(args.embed_port, QUERIES)
    for r in range(args.reps):
        for q in QUERIES:
            ms, d = call(args.port, {"query": q, "topk": args.topk})
            text_ms.append(ms)
    # vec-only pass: the real embedding of the same query (scan only)
    for r in range(args.reps):
        for v in real_vecs:
            ms, d = call(args.port, {"vec": v, "topk": args.topk})
            scored = (d.get("route") or {}).get("items_scored")
            if scored is not None and scored <= 0:
                raise SystemExit(
                    f"FATAL: vec-only query scored {scored} items — the server did no "
                    f"work, so this run cannot measure scan cost. Aborting.")
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
