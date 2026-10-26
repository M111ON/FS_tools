"""test_roster.py — the roster must be INVISIBLE: same walk, fewer looks.

The roster is the door -> rows table derived from the stamp sidecar (each row's
(anchor, anchor2), recorded when the row is appended). It replaces the gather
scan (every row, once per routed bucket) with a jump to each door's members.

Contract under test:
  R1  the roster is actually used (route.roster == 1)
  R2  top-k is IDENTICAL to the scan-built walk, query by query
  R3  items_scored is IDENTICAL (the same rows are walked)
  R4  rows LOOKED AT (route.examined) drops
  R5  the SAME holds when the walk is CUT by the budget — the roster must
      preserve the scan's order exactly, or the cut would keep other rows

R5 is the sharp one: a reordered walk list survives R2/R3 while a budget cut
would silently keep a different prefix.
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
SCRATCH = os.path.join(REPO, "build", "_roster_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
PORT = 8097
H = f"http://127.0.0.1:{PORT}"
N_ROWS = 240
DIM = 384

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


def build():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    rows = []
    with open(INDEX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            # 6 clusters of 40 rows; rows inside a cluster differ slightly so
            # the buckets have real members and the walk has real order.
            c = i % 6
            x = 0.30 * c + 0.002 * (i // 6)
            v = [0.0] * DIM
            for j in range(DIM):
                v[j] = x * ((j % 7) - 3) * 0.1 + 0.01 * ((i * 31 + j * 17) % 11)
            rows.append((f"seed-{i:04d}", [round(y, 6) for y in v]))
            f.write(json.dumps({"sid": f"seed-{i:04d}", "file": f"grp{c}",
                                "npast": 1, "dim": DIM,
                                "vec": rows[-1][1]}) + "\n")
    return rows


def start(extra):
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOTEM_WARM": "0",
           "ANN_WALK_PRUNE": "1",
           **extra}
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
    raise RuntimeError("scratch server failed to start on port %d" % PORT)


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


def queries(rows):
    """Exact-row queries + perturbed ones: different buckets, different walks."""
    qs = []
    for i in range(0, N_ROWS, 37):
        qs.append((f"row-{i}", rows[i][1]))
    for i in range(0, N_ROWS, 53):
        v = list(rows[i][1])
        for j in range(0, DIM, 13):
            v[j] = round(v[j] + 0.05, 6)
        qs.append((f"near-{i}", v))
    return qs


def run_arm(rows, roster, budget, tag):
    p = start({"ANN_ROSTER": "1" if roster else "0",
               "LZ_SEARCH_BUDGET": str(budget)})
    try:
        g = http("/v1/state/regrow", {})
        mode = (g.get("regrow") or {}).get("mode")
        out = []
        for name, v in queries(rows):
            r = http("/v1/state/search", {"vec": v, "topk": 5})
            rt = r.get("route", {})
            out.append({
                "q": name,
                "topk": [h["sid"] for h in r.get("hits", [])],
                "scores": [h["score"] for h in r.get("hits", [])],
                "scored": rt.get("items_scored", -1),
                "examined": rt.get("examined", -1),
                "roster": rt.get("roster", -1),
                "mode": rt.get("mode", "?"),
                "trunc": rt.get("truncated", -1),
                "budget": rt.get("budget", -1),
            })
        h = http("/health", to=10)
        return {"mode": mode, "queries": out, "health": h}
    finally:
        stop(p)


def compare(a, b, label):
    n = len(a["queries"])
    same_topk = sum(1 for x, y in zip(a["queries"], b["queries"]) if x["topk"] == y["topk"])
    same_scored = sum(1 for x, y in zip(a["queries"], b["queries"]) if x["scored"] == y["scored"])
    ex_off = sum(x["examined"] for x in a["queries"]) / n
    ex_on = sum(x["examined"] for x in b["queries"]) / n
    used = sum(1 for x in b["queries"] if x["roster"] == 1)
    truncated = sum(1 for x in a["queries"] if x["trunc"] == 1)
    ok(same_topk == n, f"{label}: top-k identical", f"{same_topk}/{n}")
    ok(same_scored == n, f"{label}: items_scored identical", f"{same_scored}/{n}")
    ok(used == n, f"{label}: roster used", f"{used}/{n}")
    ok(ex_on < ex_off, f"{label}: fewer rows looked at",
       f"{ex_off:,.0f} -> {ex_on:,.0f} ({ex_off / max(1.0, ex_on):.1f}x)")
    for x, y in zip(a["queries"], b["queries"]):
        if x["topk"] != y["topk"]:
            print(f"      DIFF {x['q']} trunc={x['trunc']}/{y['trunc']} "
                  f"scored={x['scored']}/{y['scored']}")
            print(f"           OFF {list(zip(x['topk'], x['scores']))}")
            print(f"           ON  {list(zip(y['topk'], y['scores']))}")
    return truncated


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE} — build ann_search_scope.exe first")
        return 2
    rows = build()
    print(f"index: {N_ROWS} rows, {DIM} dim, scratch {SCRATCH}")

    a = run_arm(rows, False, 0, "off-full")
    ok(a["mode"] == "full", "base regrow full", str(a["mode"]))
    b = run_arm(rows, True, 0, "on-full")
    compare(a, b, "full walk")

    # R5: cut the walk. Budget 12 over ~40-row buckets guarantees truncation.
    c = run_arm(rows, False, 12, "off-cut")
    d = run_arm(rows, True, 12, "on-cut")
    trunc = compare(c, d, "budget-cut walk")
    ok(trunc > 0, "budget-cut walk actually truncated", f"{trunc} queries")

    h = d["health"]
    ok((h.get("roster") or {}).get("on") == 1, "health reports the roster",
       str(h.get("roster")))

    print()
    if fails:
        print(f"RESULT: {len(fails)} FAILED / {len(ok_l) + len(fails)}")
        for n, dd in fails:
            print(f"  FAIL {n} {dd}")
        return 1
    print(f"ALL PASS ({len(ok_l)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
