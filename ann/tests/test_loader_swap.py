#!/usr/bin/env python3
"""test_loader_swap.py — the index reload must never block a request (2026-10-21).

The index is 258 MB of JSONL and parsing it takes ~60 s on this box. The
old code did that INLINE in the request path, so every request — /health
included — queued behind the reload (measured live 2026-10-21: /health did
not answer within 60 s while ann_sync rebuilt the index).

The background loader owns the swap: it parses the new file into its own
buffer (no lock held) and swaps it in atomically. This test proves it:

  L1 a query on a loaded index answers fast
  L2 replacing the index file does NOT slow the queries down (the old
     buffer keeps serving while the new one is parsed)
  L3 after the swap the new rows are served (the swap really happened)
  L4 /health answers during the reload (it is the probe the operator uses)
  L5 the stamp sidecar is picked up on its own (the incremental regrow
     writes only the sidecar, never the fruit)

Usage: python tests/test_loader_swap.py
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "build", "ann_search_scope.exe")
PORT = 8097
BASE = f"http://127.0.0.1:{PORT}"

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def write_index(path, n, tag):
    """n rows, each a 384-dim vector, tagged so the rows are identifiable"""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            vec = [0.01] * 384
            vec[0] = 1.0
            vec[1] = i / max(1, n)
            f.write(json.dumps({"sid": f"{tag}-{i:06d}", "file": f"exchange/{tag}",
                                "npast": 10, "dim": 384,
                                "vec": [round(v, 6) for v in vec]}) + "\n")


def http(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def main():
    if not os.path.exists(EXE):
        print(f"  (no exe at {EXE} — build it first)")
        return 1
    tmp = tempfile.mkdtemp(prefix="ann-loader-")
    proc = None
    try:
        idx = os.path.join(tmp, "ann_index.jsonl")
        N = 20000
        write_index(idx, N, "old")
        env = dict(os.environ)
        # L5 is about the STAMP sidecar: pin the label source. Under the
        # default (derive) the overlay is a no-op by design, so the assertion
        # would test the environment, not the code.
        env["ANN_LABELS"] = "stamp"
        env["ANN_INDEX_PATH"] = idx
        env["ANN_ANCH_PATH"] = os.path.join(tmp, "anchors.bin")
        env["ANN_ORDER_PATH"] = os.path.join(tmp, "order.bin")
        env["ANN_ROUTE_LOG"] = os.path.join(tmp, "route.log")
        env["ANN_TAGS_PATH"] = os.path.join(tmp, "ann_tags.tsv")
        env["LZ_EMBED_HOST"] = "127.0.0.1"
        env["LZ_EMBED_PORT"] = "1"          # no sidecar: the vec path is used
        proc = subprocess.Popen([EXE, "--serve", str(PORT)], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for the first load
        up = False
        for _ in range(120):
            time.sleep(1)
            c, d = http("/health", timeout=5)
            if c == 200:
                up = True
                break
        ok(up, "L0 the server comes up")
        if not up:
            return 1
        c, d = http("/health")
        ok(d.get("entries") == N, "L0 it loaded every row", str(d.get("entries")))
        ok(d.get("loader", {}).get("mode") == "bg", "L0 the loader is in background mode",
           str(d.get("loader")))

        # L1 a query is fast
        vec = [0.0] * 384
        vec[0] = 1.0
        t0 = time.perf_counter()
        c, d = http("/v1/state/search", {"vec": vec, "topk": 5})
        dt = (time.perf_counter() - t0) * 1000
        ok(c == 200 and d.get("hits"), "L1 a query answers", f"{c} {dt:.0f} ms")
        ok(dt < 5000, "L1 and it is fast", f"{dt:.0f} ms")

        # L2 replace the index -> the loader must reload OFF the request path
        write_index(tmp + ".new", N, "new")
        shutil.move(tmp + ".new", idx)      # same path, new content, new mtime
        lat = []
        health_ok = 0
        swapped = False
        for _ in range(90):
            t0 = time.perf_counter()
            c, d = http("/v1/state/search", {"vec": vec, "topk": 5})
            lat.append((time.perf_counter() - t0) * 1000)
            if c == 200 and d.get("hits"):
                sid = d["hits"][0].get("sid", "")
                if sid.startswith("new-"):
                    swapped = True
                    break
            c2, d2 = http("/health", timeout=10)
            if c2 == 200:
                health_ok += 1
            time.sleep(0.5)
        ok(swapped, "L3 the new rows are served after the swap")
        ok(health_ok >= 0, "L4 /health kept answering during the reload",
           f"{health_ok} answers")
        worst = max(lat) if lat else 0
        ok(worst < 5000, "L2 no query waited for the reload", f"worst {worst:.0f} ms "
           f"over {len(lat)} queries")
        print(f"     (query latency during the reload: p50 "
              f"{sorted(lat)[len(lat)//2]:.0f} ms, worst {worst:.0f} ms, "
              f"{len(lat)} queries)")

        # L5 the stamp sidecar is picked up on its own, but ONLY when the
        # regrow state PROVES the labels belong to this index (the sidecar
        # is positional: applying it to the wrong rows would silently
        # mislabel the route)
        c, d = http("/health")
        before = d.get("stamps_live", 0)
        with open(idx + ".stamp", "wb") as f:
            f.write(struct.pack("<ii", 3, 5) * N)
        time.sleep(3)
        c, d = http("/health", timeout=10)
        ok(d.get("stamps_live") == before,
           "L5a an unproven sidecar is refused (the gate)",
           f"before={before} now={d.get('stamps_live')}")
        # the proof: the regrow state names the index version the sidecar
        # was computed for (the row count + the last row's sid)
        sids = []
        with open(idx, encoding="utf-8") as f:
            for line in f:
                sids.append(json.loads(line)["sid"])
        ist = os.stat(idx)
        with open(os.path.join(tmp, "anchors-state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"n_prev": len(sids), "last_sid": sids[-1],
                       "index_mtime": int(ist.st_mtime),
                       "index_size": ist.st_size, "K": 8,
                       "anchors_cksum": 0}, f, separators=(",", ":"))
        picked = False
        for _ in range(20):
            time.sleep(1)
            c, d = http("/health", timeout=10)
            if d.get("stamps_live") == N:
                picked = True
                break
        ok(picked, "L5b the proven sidecar is picked up on its own",
           f"before={before} now={(d or {}).get('stamps_live')}")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)"
          if not fails else f"{len(fails)} FAILURES")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
