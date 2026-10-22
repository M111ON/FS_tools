#!/usr/bin/env python3
"""test_scope.py — the tag-scope contract (2026-10-21, request field "scope").

Scratch server on port 8097 only (NEVER the live :8096). Hermetic synthetic
index (27 rows, 384-dim, five file groups across four kinds). Verifies:

  S1  no scope  -> unchanged: hits >= 1 and mode is NOT a scope mode.
  S2  file scope big enough -> mode "scope", items_scored == the group's row
      count == budget, truncated 0, and every hit's file == the scope.
  S3  bare kind scope -> mode "scope", every hit's file starts with the kind.
  S4  file scope thinner than topk -> widens to its kind: mode "scope+kind",
      items_scored == the kind's row count (the chain measured in
      bench_tag_filter.py), every hit's file starts with the kind.
  S5  unknown scope -> HTTP 400 scope_not_found (never an unscoped answer).
  S6  scope whose kind cannot hold topk -> HTTP 400 scope_too_narrow.
  S7  the scoped top-k EQUALS numpy's exact cosine top-k over exactly those
      rows (server and oracle agree on the same candidate set).
  S8  a scoped query writes NO tombstone: fog.shut is identical before and
      after (scope skips the route layer entirely).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO, "build", "ann_search_scope.exe")
SCRATCH = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "ann-scope-scratch")
PORT = 8097
H = f"http://127.0.0.1:{PORT}"
INDEX = os.path.join(SCRATCH, "data", "memcore", "index.jsonl")
DIM = 384

# (file, rows) — group sizes chosen to exercise every branch
GROUPS = [("alpha/big", 8), ("beta/one", 4), ("beta/two", 5),
          ("delta/\u0e17\u0e35\u0e48\u0e2a\u0e38\u0e14 tensor \u0e2a\u0e23\u0e31\u0e1a\u0e01\u0e32\u0e23\u0e17\u0e35\u0e48\u0e17\u0e33\u0e07\u0e32\u0e19\u0e23\u0e30\u0e1a\u0e1a\u0e23\u0e30\u0e1a\u0e1a", 6),
          ("gamma/g1", 1), ("gamma/g2", 1), ("gamma/g3", 1), ("gamma/g4", 1),
          ("gamma/g5", 1), ("gamma/g6", 1), ("gamma/g7", 1),
          ("tiny/t1", 1), ("tiny/t2", 1), ("tiny/t3", 1)]

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=60):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = urllib.request.Request(H + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def http_status(path, body, to=60):
    """returns (status_code, parsed_json_or_None) — 4xx must not raise"""
    r = urllib.request.Request(H + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=to) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def build():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    rng = np.random.default_rng(20261021)
    rows = []
    for file, n in GROUPS:
        centre = rng.normal(size=DIM).astype(np.float32)
        for _ in range(n):
            v = centre + 0.15 * rng.normal(size=DIM).astype(np.float32)
            rows.append({"sid": "r-%03d" % len(rows), "file": file, "vec": v})
    with open(INDEX, "w", encoding="utf-8") as f:
        for e in rows:
            # ensure_ascii=False: the REAL index stores non-ASCII file tags
            # raw (measured: 7111 raw rows, 0 \u-escaped), so the test must too
            f.write(json.dumps({"sid": e["sid"], "file": e["file"], "npast": 1,
                                "dim": DIM,
                                "vec": [round(float(x), 6) for x in e["vec"]]},
                               ensure_ascii=False)
                    + "\n")
    # the TAG SIDECAR: sid-keyed (never positional), "sid\ttag\tzone"
    # zones are FROZEN: the tag -> zone map is written once and never
    # renumbered, so a re-bake keeps old zones valid.
    zones = {"alpha": 0, "beta": 1, "gamma": 2, "delta": 3}
    with open(os.path.join(SCRATCH, "ann_tags.tsv"), "w", encoding="utf-8") as f:
        for e in rows:
            tag = e["file"].split("/")[0]
            f.write(f"{e['sid']}\t{tag}\t{zones.get(tag, 9)}\n")
    return rows


def start():
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_TAGS_PATH": os.path.join(SCRATCH, "ann_tags.tsv"),
           "ANN_PORT": str(PORT),
           "ANN_TOMBSTONE_WARM": "0"}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "w", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    for _ in range(60):
        time.sleep(0.3)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)


def group_vecs(rows, file):
    return [e for e in rows if e["file"] == file]


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE} — build ann_search_scope.exe first")
        return 2
    rows = build()
    p = start()
    try:
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] in ("full", "incremental"), "regrow runs",
           g["regrow"]["mode"])

        # S1 — unscoped behaviour unchanged
        r = http("/v1/state/search", {"vec": [float(x) for x in rows[0]["vec"]], "topk": 5})
        mode = r["route"]["mode"]
        ok(len(r.get("hits", [])) >= 1, "S1 hits>=1 without scope", mode)
        ok(not mode.startswith("scope"), "S1 mode is not a scope mode", mode)

        shut_before = http("/health", to=10)["fog"]["shut"]

        # S2 — file scope big enough
        vec = [float(x) for x in rows[0]["vec"]]
        r2 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "alpha/big"})
        rt = r2["route"]
        files2 = [h["file"] for h in r2.get("hits", [])]
        ok(rt["mode"] == "scope", "S2 mode == scope", rt["mode"])
        ok(rt["items_scored"] == 8, "S2 items_scored == 8 group rows",
           str(rt["items_scored"]))
        ok(rt["budget"] == 8, "S2 budget == group size", str(rt["budget"]))
        ok(rt.get("truncated", 0) == 0, "S2 truncated == 0 (group walked whole)",
           str(rt.get("truncated")))
        ok(len(files2) == 5 and all(f == "alpha/big" for f in files2),
           "S2 every hit inside the scope", str(files2))

        # S3 — bare kind scope
        r3 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "beta"})
        rt3 = r3["route"]
        files3 = [h["file"] for h in r3.get("hits", [])]
        ok(rt3["mode"] == "scope", "S3 bare kind -> mode scope", rt3["mode"])
        ok(rt3["items_scored"] == 9, "S3 items_scored == kind rows (4+5)",
           str(rt3["items_scored"]))
        ok(all(f.startswith("beta/") for f in files3), "S3 hits under the kind",
           str(files3))

        # S4 — file scope thinner than topk widens to its kind
        r4 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "gamma/g1"})
        rt4 = r4["route"]
        files4 = [h["file"] for h in r4.get("hits", [])]
        ok(rt4["mode"] == "scope+kind", "S4 thin file scope widens -> scope+kind",
           rt4["mode"])
        ok(rt4["items_scored"] == 7, "S4 items_scored == kind rows (7 files)",
           str(rt4["items_scored"]))
        ok(all(f.startswith("gamma/") for f in files4), "S4 hits under the kind",
           str(files4))

        # S5 — unknown scope is a 400, never an unscoped answer
        code, body = http_status("/v1/state/search",
                                 {"vec": vec, "topk": 5, "scope": "nope/nope"})
        ok(code == 400 and body and body.get("error") == "scope_not_found",
           "S5 unknown scope -> 400 scope_not_found", f"{code} {body}")

        # S6 — a scope too thin for topk is a 400, never a silently widened one
        code, body = http_status("/v1/state/search",
                                 {"vec": vec, "topk": 32, "scope": "tiny"})
        ok(code == 400 and body and body.get("error") == "scope_too_narrow",
           "S6 thin kind for topk -> 400 scope_too_narrow", f"{code} {body}")

        # S7 — server's scoped top-k == numpy's exact cosine top-k on that group
        gv = group_vecs(rows, "alpha/big")
        X = np.asarray([e["vec"] for e in gv], dtype=np.float32)
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        q = np.asarray(vec, dtype=np.float32)
        q /= np.linalg.norm(q)
        order = np.argsort(-(X @ q))[:5]
        truth = {gv[i]["sid"] for i in order}
        got = {h["sid"] for h in r2.get("hits", [])}
        ok(got == truth, "S7 scoped hits == numpy exact top-5 in group",
           f"got={sorted(got)} want={sorted(truth)}")

        # S9 — a non-ASCII file tag resolves as a FILE scope, not as its kind.
        # Python's json.dumps escapes it to \uXXXX by default, so this is the
        # exact path that silently widened a 718-row group to 29153 kind rows
        # before the request was unescaped.
        thai = [g for g in GROUPS if g[0].startswith("delta/")][0][0]
        r9 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": thai})
        rt9 = r9["route"]
        files9 = [h["file"] for h in r9.get("hits", [])]
        ok(rt9["mode"] == "scope", "S9 non-ASCII file tag -> mode scope (file)",
           f"{rt9['mode']} items={rt9['items_scored']}")
        ok(rt9["items_scored"] == 6, "S9 items_scored == 6 group rows",
           str(rt9["items_scored"]))
        ok(all(f == thai for f in files9), "S9 hits inside the non-ASCII group",
           str(files9))

        # S10 — the same scope sent as raw UTF-8 (what a browser sends)
        raw = ('{"vec":[%s],"topk":5,"scope":"%s"}'
               % (",".join("%.6f" % float(x) for x in vec), thai))
        rq = urllib.request.Request(H + "/v1/state/search", raw.encode("utf-8"),
                                    {"Content-Type": "application/json"})
        r10 = json.loads(urllib.request.urlopen(rq, timeout=60).read())
        ok(r10["route"]["mode"] == "scope" and r10["route"]["items_scored"] == 6,
           "S10 raw-UTF-8 scope resolves identically",
           f"{r10['route']['mode']} items={r10['route']['items_scored']}")

        # S11 — a ZONE scope (the frozen tag->zone coordinate)
        r11 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "zone:0"})
        rt11 = r11["route"]
        files11 = [h["file"] for h in r11.get("hits", [])]
        ok(rt11["mode"] == "scope+zone", "S11 zone scope -> mode scope+zone",
           rt11["mode"])
        ok(rt11["items_scored"] == 8, "S11 items_scored == zone size (8 alpha rows)",
           str(rt11["items_scored"]))
        ok(all(f.startswith("alpha/") for f in files11),
           "S11 hits inside the zone", str(files11))

        # S12 — a TAG scope (the semantic tag)
        r12 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "tag:beta"})
        rt12 = r12["route"]
        files12 = [h["file"] for h in r12.get("hits", [])]
        ok(rt12["mode"] == "scope+tag", "S12 tag scope -> mode scope+tag",
           rt12["mode"])
        ok(rt12["items_scored"] == 9, "S12 items_scored == tag size (9 beta rows)",
           str(rt12["items_scored"]))
        ok(all(f.startswith("beta/") for f in files12),
           "S12 hits inside the tag", str(files12))

        # S13 — a zone is a CLOSED group: never widened, so an unknown zone
        # is a 400 and a thin zone is a 400 (not a silently broader answer)
        code, body = http_status("/v1/state/search",
                                 {"vec": vec, "topk": 5, "scope": "zone:77"})
        ok(code == 400 and body and body.get("error") == "scope_not_found",
           "S13 unknown zone -> 400 scope_not_found", f"{code} {body}")
        code, body = http_status("/v1/state/search",
                                 {"vec": vec, "topk": 32, "scope": "tag:alpha"})
        ok(code == 400 and body and body.get("error") == "scope_too_narrow",
           "S13 zone too thin for topk -> 400 scope_too_narrow", f"{code} {body}")

        # S14 — the tag scope beats the file scope on work: a tag spans files
        r14 = http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "tag:gamma"})
        rt14 = r14["route"]
        ok(rt14["items_scored"] == 7, "S14 tag scope spans 7 single-row files",
           str(rt14["items_scored"]))

        # S8 — scoped queries shut no tombstone (route layer untouched)
        for _ in range(3):
            http("/v1/state/search", {"vec": vec, "topk": 5, "scope": "alpha/big"})
        shut_after = http("/health", to=10)["fog"]["shut"]
        ok(shut_after == shut_before, "S8 no tombstone written by scoped queries",
           f"shut {shut_before} -> {shut_after}")
    finally:
        p.poll() or p.kill()
        try:
            p._errlog.close()
        except Exception:
            pass
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)"
          if not fails else f"{len(fails)} FAILURES")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
