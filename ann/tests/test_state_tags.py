"""test_state_tags.py — GET /v1/state/tags must answer "which zones exist".

The endpoint exists so an agent can scope a search without opening tag_lut.json
or walking the index. Contract under test:

  Z1  every tag the sidecar carries is listed, with its frozen zone id
  Z2  rows per zone == the sidecar's own recount (same ann_tags.tsv the search
      path resolves scope:<...> with — not a second source of truth)
  Z3  rows per zone == the frozen LUT's zones block (they must agree, or one of
      the two is stale and the endpoint is lying to whoever scopes on it)
  Z4  a tag retired in the LUT (zero rows) is still NAMED, flagged retired
  Z5  the sum of live rows == tagged, tagged + unlabeled == entries
  Z6  the endpoint does not invent zones: an id it lists is walkable — a scoped
      search on zone:<id> returns hits and every hit belongs to that zone
  Z7  read-only: the endpoint does not disturb /health or the search path
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
SCRATCH = os.path.join(REPO, "build", "_state_tags_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
TAGS = os.path.join(SCRATCH, "ann_tags.tsv")
LUT = os.path.join(SCRATCH, "tag_lut.json")
PORT = 8098
H = f"http://127.0.0.1:{PORT}"
N_ROWS = 120
DIM = 192
GROUPS = [("alpha", 0), ("beta", 1), ("gamma", 2)]

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
    os.makedirs(SCRATCH, exist_ok=True)
    sids = []
    with open(INDEX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            c = i % 3
            x = 0.40 * c
            v = [x * ((j % 5) - 2) * 0.1 + 0.01 * ((i * 29 + j * 13) % 7)
                 for j in range(DIM)]
            sid = f"seed-{i:04d}"
            sids.append(sid)
            f.write(json.dumps({"sid": sid, "file": f"grp{c}", "npast": 1,
                                "dim": DIM,
                                "vec": [round(y, 6) for y in v]}) + "\n")
    # the sidecar the search path reads: one row per sid, tag + frozen zone
    with open(TAGS, "w", encoding="utf-8") as f:
        for i, sid in enumerate(sids):
            t, z = GROUPS[i % 3]
            f.write(f"{sid}\t{t}\t{z}\n")
    zones = {str(z): {"tag": t,
                      "rows": sum(1 for i in range(N_ROWS) if i % 3 == z),
                      "retired": False} for t, z in GROUPS}
    zones["7"] = {"tag": "old", "rows": 0, "retired": True}   # retired in the LUT
    tags = {t: {"zone": z, "first_bake": "b1", "last_bake": "b1",
                "count": zones[str(z)]["rows"],
                "retired_at": None} for t, z in GROUPS}
    tags["old"] = {"zone": 7, "first_bake": "b0", "last_bake": "b0",
                   "count": 0, "retired_at": "b1"}
    json.dump({"zones": zones, "tags": tags, "bakes": [],
               "bake_id": "b1", "baked_at": "2026-10-26T00:00:00"},
              open(LUT, "w", encoding="utf-8"), indent=1)
    return sids


def sidecar_counts():
    by_tag, by_zone = {}, {}
    for line in open(TAGS, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 3:
            continue
        by_tag[p[1]] = by_tag.get(p[1], 0) + 1
        z = int(p[2])
        by_zone[z] = by_zone.get(z, 0) + 1
    return by_tag, by_zone


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


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE} — build ann_search_scope.exe first")
        return 2
    sids = build()
    by_tag, by_zone = sidecar_counts()
    print(f"index: {N_ROWS} rows, {DIM} dim, {len(GROUPS)} live tags "
          f"+ 1 retired, scratch {SCRATCH}")
    p = start()
    try:
        http("/v1/state/regrow", {})
        t = http("/v1/state/tags", to=30)
        zones = {z["tag"]: z for z in t.get("zones", [])}
        live = {k: v for k, v in zones.items() if not v["retired"]}

        ok(t.get("status") == "ok", "endpoint answers", str(t.get("status")))
        ok(set(live) == set(by_tag), "Z1 sidecar tags all listed",
           f"{sorted(live)} vs {sorted(by_tag)}")
        ok(all(live[k]["zone"] == next(z for name, z in GROUPS if name == k)
               for k in live), "Z1 zone ids are the frozen ones",
           str({k: live[k]["zone"] for k in live}))
        ok(all(live[k]["rows"] == by_tag[k] for k in live),
           "Z2 rows == sidecar recount",
           str({k: (live[k]["rows"], by_tag[k]) for k in live}))
        ok(all(live[k]["rows"] == by_zone[live[k]["zone"]] for k in live),
           "Z2 rows == sidecar zone recount",
           str({k: live[k]["rows"] for k in live}))
        lut = json.load(open(LUT, encoding="utf-8"))["zones"]
        ok(all(live[k]["rows"] == lut[str(live[k]["zone"])]["rows"] for k in live),
           "Z3 rows == LUT zones block",
           str({k: (live[k]["rows"], lut[str(live[k]["zone"])]["rows"]) for k in live}))
        ok("old" in zones and zones["old"]["retired"] and zones["old"]["rows"] == 0,
           "Z4 retired tag named, zero rows", str(zones.get("old")))
        ok(sum(live[k]["rows"] for k in live) == t.get("tagged", -1),
           "Z5 live rows == tagged", f"{sum(live[k]['rows'] for k in live)} vs {t.get('tagged')}")
        ok(t.get("tagged", 0) + t.get("unlabeled", 0) == t.get("entries", -1),
           "Z5 tagged + unlabeled == entries",
           f"{t.get('tagged')}+{t.get('unlabeled')} vs {t.get('entries')}")

        sid_tag = {sid: GROUPS[i % 3][0] for i, sid in enumerate(sids)}
        qvec = None
        for line in open(INDEX, encoding="utf-8"):
            e = json.loads(line)
            if sid_tag.get(e["sid"]) == GROUPS[0][0]:
                qvec = e["vec"]
                break
        for name, z in GROUPS:
            if live[name]["rows"] < 5:
                continue
            r = http("/v1/state/search",
                     {"vec": qvec, "topk": 5, "scope": f"zone:{z}"}, to=60)
            hits = [h["sid"] for h in r.get("hits", [])]
            ok(len(hits) == 5 and all(sid_tag.get(h) == name for h in hits),
               f"Z6 zone:{z} walkable and same group", f"{len(hits)} hits"
               + ("" if hits else " | " + json.dumps(r)[:300]))

        h = http("/health", to=10)
        ok(h.get("status") == "ok" and (h.get("tags") or {}).get("kinds") == len(by_tag),
           "Z7 health undisturbed", str(h.get("tags")))
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
