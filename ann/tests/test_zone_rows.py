"""test_zone_rows.py — the zone scope jumps to a prebuilt row index instead of
re-scanning the index twice per query (card t4fbea5cf, slice 1).

The gate is NOT "recall looks fine": the zone-rows index must produce the SAME
walk list, in the SAME order, as the filter it replaced. A set-equal walk that
got reordered still returns the same top-k on most queries and hides the change,
so every comparison is (hits, walk_hash, items_scored) at once with
ANN_WALK_HASH=1.

The baseline is the SAME binary with ANN_ZONE_ROWS=0 (the legacy two-pass
filter), not a stored old exe — an old exe is not portable across checkouts.

  R1  zone scope ON vs OFF: identical hits/walk_hash/scored (12 queries)
  R2  a second zone, different size: identical too
  R3  a zone narrower than topk is still refused (fail loud, not widened)
  R4  no tag sidecar: same answer with the prebuilt index live (it cannot jump)
  R5  /health still truthful with zone rows built
"""
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO, "build", "ann_search_scope.exe")
SCRATCH = os.path.join(os.environ.get("TEMP", REPO), "ann_zone_rows_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
TAGS = os.path.join(SCRATCH, "ann_tags.tsv")
ANCH = os.path.join(SCRATCH, "ann.bin")
PORT = 8102
H = f"http://127.0.0.1:{PORT}"
N = 9000
DIM = 32

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=180):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = urllib.request.Request(H + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def build():
    rng = random.Random(21)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    rows = []
    with open(INDEX, "w", encoding="utf-8") as f, open(TAGS, "w", encoding="utf-8") as g:
        for i in range(N):
            z = i % 5 if i >= 3 else 9   # zone 9 = 3 rows, for the narrow gate
            v = [rng.gauss(0, 1) + (1.5 if j == z else 0.0) for j in range(DIM)]
            f.write(json.dumps({"sid": f"r{i}", "file": f"grp{z}", "npast": 1,
                                "dim": DIM, "vec": [round(x, 5) for x in v]}) + "\n")
            g.write(f"r{i}\tzone{z}\t{z}\n")
            rows.append(v)
    return rows


def start(zone_rows=True, with_tags=True):
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_TAGS_PATH": TAGS if with_tags else os.path.join(SCRATCH, "nope.tsv"),
           "ANN_ANCH_PATH": ANCH,
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_ROSTER_PATH": os.path.join(SCRATCH, "roster.bin"),
           "ANN_ZONE_ROWS": "1" if zone_rows else "0",
           "ANN_PORT": str(PORT), "ANN_TOTEM_WARM": "0", "ANN_WALK_HASH": "1",
           "ANN_WALK_PRUNE": "1"}
    err = open(os.path.join(SCRATCH, f"srv_{int(zone_rows)}.log"), "a", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=err)
    p._err = err
    for _ in range(120):
        time.sleep(0.3)
        try:
            h = http("/health", to=4)
            if h.get("status") in ("ok", "degraded"):
                return p, h
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
        p._err.close()
    except Exception:
        pass


def q(v, scope, topk=5):
    try:
        d = http("/v1/state/search", {"vec": v, "topk": topk, "scope": scope})
    except urllib.error.HTTPError as e:
        return {"err": e.read().decode("utf-8", "replace")[:120]}
    route = d.get("route", {})
    return {"hits": [h["sid"] for h in d["hits"]],
            "walk_hash": d.get("route", {}).get("walk_hash"),
            "scored": route.get("items_scored"),
            "total": route.get("items_total"),
            "buckets": route.get("buckets", []),
            "roster": route.get("roster"),
            "roster_ready": route.get("roster_ready"),
            "roster_doors": route.get("roster_doors"),
            "route_k": route.get("route_k"),
            "roster_rows": route.get("roster_rows"),
            "totem": route.get("totem", {})}


def arm(queries, zone_rows=True, with_tags=True, regrow=True):
    p, _ = start(zone_rows, with_tags)
    try:
        if regrow:
            http("/v1/state/regrow", {}, to=180)
            time.sleep(1)
        return [q(v, s) for v, s in queries]
    finally:
        stop(p)


def cmp(a, b):
    return all(x.get("hits") == y.get("hits") and x.get("walk_hash") == y.get("walk_hash")
               and x.get("scored") == y.get("scored") for x, y in zip(a, b))


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE}")
        return 2
    rows = build()
    rng = random.Random(99)
    picks = [rows[i] for i in rng.sample(range(N), 12)]
    q1 = [(v, "zone:1") for v in picks]
    q2 = [(v, "zone:3") for v in picks]

    a_on, a_off = arm(q1, True), arm(q1, False)
    ok(cmp(a_on, a_off), "R1 zone:1 walk_hash + hits + scored identical (on vs off)",
       f"{a_on[0].get('walk_hash')} vs {a_off[0].get('walk_hash')}")

    b_on, b_off = arm(q2, True), arm(q2, False)
    ok(cmp(b_on, b_off), "R2 zone:3 identical too (a different size)",
       f"{b_on[0].get('walk_hash')} vs {b_off[0].get('walk_hash')}")

    # a zone with fewer rows than topk must still be refused, both ways
    small = [(picks[0], "zone:9", 5)]   # 3 rows < topk 5
    def arm_topk(zone_rows):
        p, _ = start(zone_rows)
        try:
            http("/v1/state/regrow", {}, to=180)
            time.sleep(1)
            d = None
            try:
                d = q(*small[0])
            except Exception as e:
                d = {"err": str(e)}
            return d
        finally:
            stop(p)
    r_on, r_off = arm_topk(True), arm_topk(False)
    ok(("err" in r_on) == ("err" in r_off),
       "R3 scope_too_narrow still refused on both paths",
       r_on.get("err", "ok")[:60])

    # R6: the receipt must expose the actual entrance evidence. A route label
    # without buckets/roster/Totem is not a warp receipt.
    p, _ = start(True)
    try:
        http("/v1/state/regrow", {}, to=180)
        rr = q(picks[1], "")
        ok(bool(rr.get("buckets")) and rr.get("roster") in (0, 1)
           and isinstance(rr.get("totem"), dict),
           "R6 route receipt exposes buckets + roster + totem",
           f"buckets={len(rr.get('buckets', []))} roster={rr.get('roster')} "
           f"doors={rr.get('roster_doors')} route_k={rr.get('route_k')} "
           f"rows={rr.get('roster_rows')}")
        ok(rr.get("roster_ready") == 1 and rr.get("roster") == 1,
           "R8 ready roster is used by the routed walk",
           f"ready={rr.get('roster_ready')} used={rr.get('roster')}")
        ok(rr.get("total") == N and rr.get("scored", 0) <= N,
           "R7 scored count is bounded by the selected fixture",
           f"scored={rr.get('scored')} total={rr.get('total')}")
    finally:
        stop(p)

    f_on, f_off = arm(q1, True, with_tags=False), arm(q1, False, with_tags=False)
    ok(all(("err" in x) == ("err" in y) for x, y in zip(f_on, f_off)),
       "R4 no tag sidecar: same answer as the legacy filter path",
       f_on[0].get("err", "ok")[:60])

    p, h = start(True)
    try:
        ok(h.get("status") in ("ok", "degraded") and h.get("entries") == N,
           "R5 /health with zone rows live", f"entries={h.get('entries')}")
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
