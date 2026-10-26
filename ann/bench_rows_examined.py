"""Rows LOOKED AT vs rows SCORED, and the roster A/B.

The gather pass looks at every row once per routed bucket to find that bucket's
members; the roster replaces that scan with a jump. Gate: identical top-k,
identical items_scored — only `examined` may change.
"""
import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(DIR, "data", "memcore")
EXE = os.path.join(DIR, "build", "ann_search.exe")


def wait_health(port, timeout=240.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                o = json.loads(r.read())
                if o.get("status") == "ok" and o.get("entries", 0) > 0:
                    return o
        except Exception:
            pass
        time.sleep(0.5)
    return None


def search(port, vec, topk=5):
    body = json.dumps({"vec": vec, "topk": topk}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/state/search",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def prepare(work):
    os.makedirs(work, exist_ok=True)
    idx = os.path.join(work, "ann_index.jsonl")
    if not os.path.exists(idx):
        print(f"copying index -> {work} ...")
        for name in ("ann_index.jsonl", "ann_anchors.bin", "ann_index.order"):
            s = os.path.join(SRC, name)
            if os.path.exists(s):
                shutil.copyfile(s, os.path.join(work, name))
    return idx


def run_pass(work, idx, port, qs, roster, tag):
    env = dict(os.environ)
    env.update({
        "ANN_INDEX_PATH": idx,
        "ANN_ANCH_PATH": os.path.join(work, "ann_anchors.bin"),
        "ANN_ORDER_PATH": os.path.join(work, "ann_index.order"),
        "ANN_ROUTE_LOG": os.path.join(work, f"route-{tag}.log"),
        "LZ_EMBED_HOST": "127.0.0.1",
        "LZ_EMBED_PORT": "18095",
        "ANN_WALK_PRUNE": "1",
        "ANN_WALK_FLOOR": "0.5",
        "LZ_ROUTE_BUCKETS": "5",
        "ANN_TOTEM_WARM": "0",
        "ANN_ROSTER": "1" if roster else "0",
        "ANN_WALK_HASH": "1",
        "ANN_WALK_DUMP": os.path.join(work, f"walk-{tag}.txt"),
        "ANN_PORT": str(port),
    })
    log = open(os.path.join(work, f"server-{tag}.log"), "wb")
    p = subprocess.Popen([EXE, "--serve", str(port)], cwd=DIR, env=env,
                         stdout=log, stderr=log)
    try:
        h = wait_health(port)
        if not h:
            print(f"[{tag}] server did not become healthy")
            return None
        out = []
        for sid, v in qs:
            o = search(port, v)
            rt = o.get("route", {})
            out.append({
                "examined": rt.get("examined", -1),
                "scored": rt.get("items_scored", -1),
                "roster": rt.get("roster", -1),
                "mode": rt.get("mode", "?"),
                "trunc": rt.get("truncated", -1),
                "budget": rt.get("budget", -1),
                "amp": (rt.get("amp") or {}).get("applied", -1),
                "topk": [x.get("sid") for x in o.get("hits", [])],
                "scores": [x.get("score") for x in o.get("hits", [])],
                "walk": rt.get("walk_hash", 0),
                "q": sid,
            })
        return out
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        log.close()


def summarize(tag, rows):
    ex = [r["examined"] for r in rows]
    sc = [r["scored"] for r in rows]
    rs = sum(1 for r in rows if r["roster"] == 1)
    modes = {}
    for r in rows:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    print(f"[{tag}] n={len(rows)} roster_used={rs}/{len(rows)} modes={modes}")
    print(f"  examined mean={statistics.mean(ex):,.1f} p50={statistics.median(ex):,.0f}")
    print(f"  scored   mean={statistics.mean(sc):,.1f} p50={statistics.median(sc):,.0f}")
    return ex, sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--work", default=r"I:\tmp\ann_roster")
    ap.add_argument("--ab", action="store_true", help="run roster OFF then ON and diff")
    ap.add_argument("--roster", type=int, default=None, help="single pass with this setting")
    args = ap.parse_args()

    idx = prepare(args.work)
    rows = []
    with open(idx, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            v = o.get("vec") or o.get("v") or o.get("embedding")
            if v:
                rows.append((o.get("sid") or o.get("id"), v))
    print(f"index={len(rows)} dim={len(rows[0][1])}")
    rnd = random.Random(args.seed)
    qs = rnd.sample(rows, min(args.queries, len(rows)))

    if args.ab:
        off = run_pass(args.work, idx, 8097, qs, 0, "off")
        on = run_pass(args.work, idx, 8098, qs, 1, "on")
        if not off or not on:
            return 1
        ex_off, sc_off = summarize("roster OFF", off)
        ex_on, sc_on = summarize("roster ON ", on)
        same_topk = sum(1 for a, b in zip(off, on) if a["topk"] == b["topk"])
        same_scored = sum(1 for a, b in zip(off, on) if a["scored"] == b["scored"])
        same_walk = sum(1 for a, b in zip(off, on) if a["walk"] == b["walk"] and a["walk"] != 0)
        n = len(qs)
        for a, b in zip(off, on):
            if a["walk"] != b["walk"]:
                print(f"\nWALK DIFF query={a['q']} off={a['walk']:08x} on={b['walk']:08x} "
                      f"scored={a['scored']}/{b['scored']}")
        for a, b in zip(off, on):
            if a["topk"] != b["topk"]:
                print(f"\nDIFF query={a['q']}")
                print(f"  OFF scored={a['scored']} trunc={a['trunc']} budget={a['budget']} amp={a['amp']} examined={a['examined']}")
                print(f"      {list(zip(a['topk'], a['scores']))}")
                print(f"  ON  scored={b['scored']} trunc={b['trunc']} budget={b['budget']} amp={b['amp']} examined={b['examined']}")
                print(f"      {list(zip(b['topk'], b['scores']))}")
        print(f"\nGATE")
        print(f"  top-k identical      : {same_topk}/{n}  {'PASS' if same_topk == n else 'FAIL'}")
        print(f"  walk hash identical  : {same_walk}/{n}  {'PASS' if same_walk == n else 'FAIL'}")
        print(f"  items_scored identical: {same_scored}/{n}  {'PASS' if same_scored == n else 'FAIL'}")
        print(f"  examined OFF mean={statistics.mean(ex_off):,.0f} "
              f"ON mean={statistics.mean(ex_on):,.0f} "
              f"-> {statistics.mean(ex_off)/max(1,statistics.mean(ex_on)):.1f}x fewer looks")
        print(f"  ratio examined/scored OFF={statistics.mean(ex_off)/statistics.mean(sc_off):.2f}x "
              f"ON={statistics.mean(ex_on)/statistics.mean(sc_on):.2f}x")
        return 0 if (same_topk == n and same_scored == n) else 1

    r = 1 if args.roster is None else args.roster
    rows_out = run_pass(args.work, idx, 8097, qs, r, "solo")
    if not rows_out:
        return 1
    summarize(f"roster {'ON' if r else 'OFF'}", rows_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
