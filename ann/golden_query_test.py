#!/usr/bin/env python
"""golden_query_test.py — regression suite for the ANN memory layer.

Runs a FIXED set of queries against the live index and asserts:
  1. top-1 sid matches the expected (golden) sid
  2. top-1 score >= min_score
  3. latency <= latency_budget_ms (default 50ms warm, 200ms cold)

Exit 0 if all pass, 1 otherwise. Use in CI / pre-deploy gate.

Usage:
    python golden_query_test.py
    python golden_query_test.py --budget 100
    python golden_query_test.py --json
"""
import argparse
import json
import os
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

# Golden set: query -> expected top-1 sid prefix + min score.
# SIDs are prefixes (first 12 hex) so the suite survives index regrows.
GOLDEN = [
    {"q": "DWGLS field contract MAP not compress", "sid_prefix": "8f828acebc4f", "min_score": 0.80},
    {"q": "KV cache temporal gates acceptance", "sid_prefix": "a4dbd0cf516b", "min_score": 0.70},
    {"q": "chat-pool concurrency rules", "sid_prefix": "9ab1d70e3ce5", "min_score": 0.70},
    {"q": "ANN multi-view gate verdict", "sid_prefix": "986a222b0c04", "min_score": 0.70},
    {"q": "breathing_fs foundation", "sid_prefix": "91b0f0248fc0", "min_score": 0.70},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=200.0, help="latency budget ms (cold-friendly)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import mcp_ann_search as m
    s = m._get_gpu_search()
    s._ensure_loaded()

    # warmup
    s.search("warmup", topk=1)

    results = []
    all_pass = True
    for g in GOLDEN:
        t0 = time.perf_counter()
        r = s.search(g["q"], topk=5)
        lat = (time.perf_counter() - t0) * 1000.0
        hits = r.get("hits", []) if isinstance(r, dict) else []
        top = hits[0] if hits else {}
        sid = top.get("sid", "")
        score = top.get("score", 0.0)
        sid_ok = sid.startswith(g["sid_prefix"])
        score_ok = score >= g["min_score"]
        lat_ok = lat <= args.budget
        passed = sid_ok and score_ok and lat_ok
        if not passed:
            all_pass = False
        results.append({
            "query": g["q"],
            "passed": passed,
            "top_sid": sid[:12],
            "expected_prefix": g["sid_prefix"],
            "sid_ok": sid_ok,
            "score": round(score, 4),
            "min_score": g["min_score"],
            "score_ok": score_ok,
            "latency_ms": round(lat, 1),
            "budget_ms": args.budget,
            "latency_ok": lat_ok,
        })

    if args.json:
        print(json.dumps({"all_pass": all_pass, "results": results}, ensure_ascii=False, indent=2))
        return 0 if all_pass else 1

    print("=" * 70)
    print("GOLDEN QUERY REGRESSION — ANN MEMORY LAYER")
    print("=" * 70)
    for r in results:
        flag = "PASS" if r["passed"] else "FAIL"
        print("[%s] %s" % (flag, r["query"]))
        print("       top=%s (exp %s) sid_ok=%s | score=%.4f (min %.2f) score_ok=%s | lat=%.1fms (budget %.0f) lat_ok=%s"
              % (r["top_sid"], r["expected_prefix"], r["sid_ok"], r["score"], r["min_score"],
                 r["score_ok"], r["latency_ms"], r["budget_ms"], r["latency_ok"]))
    print("-" * 70)
    print("RESULT: %s (%d/%d passed)" % ("ALL PASS" if all_pass else "FAILURES PRESENT",
                                         sum(1 for r in results if r["passed"]), len(results)))
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
PY