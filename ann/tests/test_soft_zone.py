"""test_soft_zone.py — `bias` turns the zone scope into a nudge, not a filter.

Card tabbd47c5: soft zone bias vs the hard zone filter. The hard arm replaces
the walk (an answer outside the zone is unreachable); the soft arm keeps the
route and only ADDS `bias` to the score of rows in the zone. Contract:

  S1  no `bias` -> byte-identical to a plain search (same hits, same walk_hash,
      same items_scored) — the feature is invisible unless asked for
  S2  hard `scope:zone:N` cannot return a row outside the zone (by design)
  S3  soft `bias` KEEPS the global answer (the far-zone row survives) and is
      labelled soft+zone
  S4  the soft walk is the SAME walk — walk_hash and items_scored unchanged,
      so the bias cannot hide work or a route change
  S5  `bias` is clamped (99 behaves like the 0.5 ceiling, no error)
  S6  `bias` on a non-zone scope is ignored (mode stays a scope mode), so it
      can never silently widen a tag/file filter into a global search
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO, "build", "ann_search_scope.exe")
SCRATCH = os.path.join(REPO, "build", "_soft_zone_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
TAGS = os.path.join(SCRATCH, "ann_tags.tsv")
PORT = 8100
H = f"http://127.0.0.1:{PORT}"
DIM = 64

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=120):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = urllib.request.Request(H + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def unit(v):
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def build():
    """One axis per zone (far apart), and a single bridge row in zone 2 that is
    *almost* as close to the zone-0 query as the zone-0 rows are. That row is
    the thing a hard filter throws away and a nudge must keep."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    ax = [[1.0] + [0.0] * (DIM - 1),
          [0.0, 1.0] + [0.0] * (DIM - 2),
          [0.0, 0.0, 1.0] + [0.0] * (DIM - 3)]
    rows = []

    def add(sid, zone, vec):
        rows.append((sid, zone, vec))

    # zone 0: four very close rows (the query's true neighbours) + six far ones
    for i in range(4):
        add(f"z0-near-{i}", 0, unit([1.0] + [0.0005 * (i + 1)] + [0.0] * (DIM - 2)))
    for i in range(6):
        add(f"z0-far-{i}", 0, unit([1.0, 0.25 + 0.05 * i] + [0.0] * (DIM - 2)))
    # zone 1: its own cluster, unrelated to the query
    for i in range(10):
        add(f"z1-{i}", 1, unit(ax[1] + [0.01 * i] + [0.0] * (DIM - 2)))
    # zone 2: the bridge row is close to the zone-0 query (cos ~0.999)
    add("z2-bridge", 2, unit([1.0, 0.02] + [0.0] * (DIM - 2)))
    for i in range(9):
        add(f"z2-{i}", 2, unit(ax[2] + [0.01 * i] + [0.0] * (DIM - 2)))
    with open(INDEX, "w", encoding="utf-8") as f:
        for sid, z, v in rows:
            f.write(json.dumps({"sid": sid, "file": f"grp{z}", "npast": 1,
                                "dim": DIM,
                                "vec": [round(x, 6) for x in v]}) + "\n")
    with open(TAGS, "w", encoding="utf-8") as f:
        for sid, z, _ in rows:
            f.write(f"{sid}\tzone{z}\t{z}\n")
    return rows


def start():
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_TAGS_PATH": TAGS,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOTEM_WARM": "0",
           "ANN_ROSTER": "0"}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "a", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    for _ in range(80):
        time.sleep(0.3)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start")


def stop(p):
    p.poll() or p.kill()
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    try:
        p._errlog.close()
    except Exception:
        pass


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE}")
        return 2
    rows = build()
    near = unit([1.0, 0.0007] + [0.0] * (DIM - 2))
    print(f"scratch: {len(rows)} rows, zones 0/1/2, bridge row z2-bridge")
    p = start()
    try:
        http("/v1/state/regrow", {})
        base = {"vec": near, "topk": 5}
        g = http("/v1/state/search", base, to=60)
        gs = [h["sid"] for h in g["hits"]]
        grt = g.get("route", {})
        ok(len(gs) == 5, "setup: global top-5", str(gs))
        ok("z2-bridge" in gs, "setup: the bridge row IS a global answer", str(gs))

        d1 = http("/v1/state/search", {**base, "bias": 0}, to=60)
        ok([h["sid"] for h in d1["hits"]] == gs and
           d1.get("route", {}).get("walk_hash") == grt.get("walk_hash") and
           d1.get("route", {}).get("items_scored") == grt.get("items_scored"),
           "S1 bias=0 is the plain search (hits+walk_hash+scored)",
           f"{d1.get('route', {}).get('mode')}")

        hard = http("/v1/state/search", {**base, "scope": "zone:0"}, to=60)
        hs = [h["sid"] for h in hard["hits"]]
        ok("z2-bridge" not in hs and all(s.startswith("z0-") for s in hs),
           "S2 hard filter cannot return the bridge row", str(hs))

        soft = http("/v1/state/search", {**base, "scope": "zone:0", "bias": 0.005}, to=60)
        ss = [h["sid"] for h in soft["hits"]]
        srt = soft.get("route", {})
        ok(ss == gs, "S3 soft keeps the global answer", str(ss))
        ok(srt.get("mode") == "soft+zone", "S3 labelled soft+zone", str(srt.get("mode")))
        ok(srt.get("walk_hash") == grt.get("walk_hash") and
           srt.get("items_scored") == grt.get("items_scored"),
           "S4 the soft walk is unchanged (same hash, same work)",
           f"{srt.get('walk_hash')} / {srt.get('items_scored')}")

        big = http("/v1/state/search", {**base, "scope": "zone:0", "bias": 99}, to=60)
        ok(big.get("status") == "ok" and
           big.get("route", {}).get("items_scored") == grt.get("items_scored"),
           "S5 bias is clamped, not rejected",
           f"mode={big.get('route', {}).get('mode')}")

        fscope = http("/v1/state/search", {**base, "scope": "grp2", "bias": 0.05}, to=60)
        ok(fscope.get("route", {}).get("mode", "").startswith("scope"),
           "S6 bias on a file scope stays a filter",
           str(fscope.get("route", {}).get("mode")))
    finally:
        stop(p)

    print()
    if fails:
        print(f"RESULT: {len(fails)} FAILED / {len(ok_l) + len(fails)}")
        for n, d in fails:
            print(f"  FAIL {n} {d}")
        return 1
    print(f"ALL PASS ({len(ok_l)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
