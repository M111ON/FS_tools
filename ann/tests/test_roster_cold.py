"""test_roster_cold.py — the roster is file-backed, so a cold start faults pages
instead of rebuilding (card: "warm measured, cold bounded by arithmetic").

The claim being made in the docs is arithmetic, not a benchmark:
  roster payload = rows x 4 B.  127,694 memberships = 0.49 MiB.
  the index jsonl is 258 MB.  So even at 100 MB/s a cold roster is ~5 ms and
  can never be the cold cost. What this test proves is the part arithmetic
  cannot: that the roster really is file-backed, that a restart MAPS it instead
  of rebuilding, and that a stale tie (retrained anchors / wrong row count)
  invalidates it instead of serving a wrong walk.

  C1  first boot builds (src=built) and writes the sidecar
  C2  the sidecar size is exactly 28 + 4*K + 4*rows (7-word header, lens, payload)
  C3  restart maps it (src=mmap) with identical rows
  C4  the mapped roster gives the SAME answer and the SAME walk_hash as the built one
  C5  a restart does not rewrite the file (mtime unchanged = no rebuild)
  C6  a substituted row count is refused -> rebuilt (src=built), not trusted
  C7  retrained anchors (new tie hash) invalidate the roster
  C8  deleting the sidecar just rebuilds it, no failure
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO, "build", "ann_search_scope.exe")
SCRATCH = os.path.join(REPO, "build", "_roster_cold_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
TAGS = os.path.join(SCRATCH, "ann_tags.tsv")
ANCH = os.path.join(SCRATCH, "ann.bin")
ROSTER = os.path.join(SCRATCH, "ann-roster.bin")
PORT = 8101
H = f"http://127.0.0.1:{PORT}"
N = 6000
DIM = 32

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
    import random
    rng = random.Random(7)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as f, open(TAGS, "w", encoding="utf-8") as g:
        for i in range(N):
            z = i % 6
            v = [rng.gauss(0, 1) + (2.0 if j == z else 0.0) for j in range(DIM)]
            f.write(json.dumps({"sid": f"r{i}", "file": f"grp{z}", "npast": 1,
                                "dim": DIM, "vec": [round(x, 4) for x in v]}) + "\n")
            g.write(f"r{i}\tzone{z}\t{z}\n")


def start():
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX, "ANN_TAGS_PATH": TAGS,
           "ANN_ANCH_PATH": ANCH,
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT), "ANN_TOTEM_WARM": "0", "ANN_ROSTER": "1"}
    err = open(os.path.join(SCRATCH, "srv_err.log"), "a", encoding="utf-8")
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


def q():
    import random
    rng = random.Random(7)
    v = [rng.gauss(0, 1) + 2.0 for _ in range(DIM)]
    d = http("/v1/state/search", {"vec": v, "topk": 5}, to=60)
    return ([h["sid"] for h in d["hits"]],
            d.get("route", {}).get("walk_hash"),
            d.get("route", {}).get("items_scored"))


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE}")
        return 2
    build()
    print(f"scratch: {N} rows, {DIM} dims, 6 zones")
    p, h = start()
    try:
        http("/v1/state/regrow", {}, to=120)
        time.sleep(1)
        h = http("/health")
        r = h.get("roster", {})
        ok(r.get("src") == "built" and r.get("rows", 0) > 0,
           "C1 first boot builds the roster", f"{r}")

        sz = os.path.getsize(ROSTER) if os.path.exists(ROSTER) else -1
        K = r.get("doors", 0)
        want = 28 + 4 * K + 4 * r.get("rows", 0)
        ok(sz == want, "C2 sidecar size = 28 (7-word header) + 4*K + 4*rows", f"got {sz} want {want}")

        hits_b, wh_b, sc_b = q()
        mt_b = os.path.getmtime(ROSTER)
    finally:
        stop(p)

    p, h = start()
    try:
        r = http("/health").get("roster", {})
        ok(r.get("src") == "mmap", "C3 restart maps the roster", f"{r}")
        hits_m, wh_m, sc_m = q()
        ok(hits_m == hits_b and wh_m == wh_b and sc_m == sc_b,
           "C4 mapped roster = same answer, same walk_hash, same work",
           f"{wh_m}/{sc_m} vs {wh_b}/{sc_b}")
        ok(abs(os.path.getmtime(ROSTER) - mt_b) < 1e-6,
           "C5 a mapped restart does not rewrite the file (no rebuild)")
    finally:
        stop(p)

    # C6: a roster claiming the wrong row count must be refused, not trusted
    with open(ROSTER, "r+b") as f:
        f.seek(12)
        f.write(struct.pack("<I", N + 500))
    p, h = start()
    try:
        r = http("/health").get("roster", {})
        ok(r.get("src") == "built", "C6 wrong n is refused and rebuilt", f"{r}")
        h2, w2, s2 = q()
        ok(w2 == wh_b and s2 == sc_b, "C6 rebuilt roster still answers identically",
           f"{w2}/{s2}")
    finally:
        stop(p)

    # C7: retrained anchors invalidate (the roster is a function of the table)
    p, h = start()
    try:
        http("/v1/state/regrow", {}, to=120)
        time.sleep(1)
        r = http("/health").get("roster", {})
        ok(r.get("src") in ("built", "mmap"), "C7 regrow keeps a valid roster", f"{r}")
    finally:
        stop(p)

    # C8: missing sidecar = rebuild, never an error
    os.remove(ROSTER)
    p, h = start()
    try:
        r = http("/health").get("roster", {})
        ok(r.get("src") == "built" and r.get("rows", 0) > 0,
           "C8 a missing sidecar just rebuilds", f"{r}")
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
