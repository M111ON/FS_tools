#!/usr/bin/env python3
"""test_stamp_preserve.py — routing-stamp preservation contract (2026-10-20).

THE DEFECT THIS PINS
--------------------
The live index on 2026-10-20 served `items_scored: 4` out of 62,204 rows
while /health reported `routed: 1` (false green). Root cause chain:

  1. scripts/build_index.py REBUILDS ann_index.jsonl from the memcore store
     and writes rows WITHOUT the anchor/anchor2 fields (by design: the
     server's regrow is supposed to add them).
  2. The server's incremental regrow copies the PREFIX rows' stamps from the
     jsonl it just parsed (`rows[i] = a[i].anchor`, ann_search.c) — so after a
     rebuild every prefix stamp is -1.
  3. The incremental path then writes those (-1,-1) stamps into the
     <index>.stamp sidecar, wiping the routing table of 62k rows.
  4. The pruned walk can only find the few rows that were routed AFTER the
     rebuild -> search quality collapses silently (health stays green).

So the contract is: a rebuild-without-stamps followed by an append must NOT
be served as an incremental regrow. Either the stamps are carried over
(append-only index) or the regrow must fall back to the full path and say so.

Cases (scratch server on port 8097 only):
  S1 fresh index (no stamps) -> regrow full -> the jsonl now CARRIES stamps.
  S2 REBUILD the same rows WITHOUT stamps (build_index.py shape) -> append
     4 new rows -> regrow: the mode must NOT be "incremental" with a wiped
     prefix; the prefix stamps must survive (sidecar or jsonl).
  S3 after S2 the search must score a real fraction of the index
     (items_scored >> the 4-row collapse), i.e. the routing table is alive.
  S4 a full regrow (force) must leave the served stamps consistent with the
     index it just wrote (no stale sidecar overriding fresh jsonl stamps).

Expected values come from the contract (independent of the code under test):
the prefix stamps must be the ones the FULL path computed in S1, and the
search must score more than the handful of post-rebuild rows.
"""
import json, os, shutil, socket, subprocess, sys, time, urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
EXE = os.path.join(REPO, "build", "ann_search_isolate.exe")
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-isolate-scratch", "stamp")
PORT = 8097
H = f"http://127.0.0.1:{PORT}"
INDEX = os.path.join(SCRATCH, "data", "memcore", "index.jsonl")
STAMP = INDEX + ".stamp"
N_ROWS = 400
N_APPEND = 4

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=120):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = Request(H + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def row(i, with_stamp=None):
    """One index row. with_stamp=None -> build_index.py shape (NO anchor
    fields); with_stamp=(a,a2) -> server-rewritten shape."""
    step = 0.4 / N_ROWS
    x = step * i
    v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
    vec = ",".join("%.6f" % t for t in v)
    if with_stamp is None:
        return ('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                '"vec":[%s]}' % (i, vec))
    return ('{"sid":"seed-%04d","file":"_seed","npast":1,"anchor":%d,"anchor2":%d,'
            '"dim":384,"vec":[%s]}' % (i, with_stamp[0], with_stamp[1], vec))


def write_rows(path, rows, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(r + "\n")


def read_stamps(path):
    """-> list of (anchor, anchor2) from a .stamp sidecar, or None."""
    if not os.path.exists(path):
        return None
    import struct
    out = []
    with open(path, "rb") as f:
        data = f.read()
    for i in range(len(data) // 8):
        out.append(struct.unpack_from("<ii", data, i * 8))
    return out


def md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonl_stamps(path):
    """-> list of (anchor, anchor2) from the jsonl's own anchor fields
    (missing field == (-1,-1), which is what the parser sees)."""
    import re
    pat = re.compile(r'"anchor":(-?\d+),"anchor2":(-?\d+)')
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = pat.search(line)
            out.append((int(m.group(1)), int(m.group(2))) if m else (-1, -1))
    return out


def start():
    return start_extra({})


def start_extra(env_extra):
    """The tree's files (anchors + order + the regrow state derived from
    ANN_ANCH_PATH) must live TOGETHER — splitting them across dirs leaves
    the server anchorless (no route -> brute -> the door never fires).

    The health probe is tied to THIS process: a leftover instance still
    holding the port would answer /health while our own server had already
    exited on "bind failed" — the test would then measure the wrong
    instance and still report green.
    """
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOMBSTONE_WARM": "1",
           **env_extra}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "w", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    for _ in range(80):
        time.sleep(0.3)
        if p.poll() is not None:
            # our server died (bind failed?) — do NOT accept another
            # instance's answer as ours
            raise RuntimeError("scratch server exited on start (rc=%s)"
                               % p.returncode)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)


def stop(p):
    """Stop a scratch server AND wait until it released the port.

    p.kill() only asks the OS to terminate it; the listening socket can
    outlive it for a moment. The next start_extra() would then hit
    "bind 8097 failed" and — worse — talk to the dying instance.
    """
    if p.poll() is None:
        p.kill()
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    for _ in range(60):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", PORT))
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("port %d still held after stopping the server" % PORT)


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)

    # ── S1: fresh index, no stamps -> full regrow stamps the jsonl ────────
    write_rows(INDEX, [row(i) for i in range(N_ROWS)])
    p = start()
    try:
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] == "full", "S1 fresh base mode full", g["regrow"]["mode"])
        # TREE-SIDE SEPARATION: after a full regrow the FRUIT (the jsonl
        # rows) carries NO tree state — the labels live in the tree's
        # channel (the sidecar). The old code wrote anchor/anchor2 back
        # into the fruit, which put tree state into the fruit and made a
        # fruit rebuild indistinguishable from a tree change.
        js = jsonl_stamps(INDEX)
        fruit_labels = sum(1 for a, b in js if a >= 0)
        ok(fruit_labels == 0,
           "S1 the fruit carries no tree state after a full regrow",
           f"labels_in_fruit={fruit_labels}")
        side1 = read_stamps(STAMP) or []
        live1 = sum(1 for a, b in side1 if a >= 0)
        ok(live1 >= N_ROWS // 2, "S1 the tree's sidecar carries the labels",
           f"live={live1}/{len(side1)}")
        full_stamps = list(js)
    finally:
        stop(p)

    # ── S2: REBUILD without stamps (build_index.py shape) + append ────────
    write_rows(INDEX, [row(i) for i in range(N_ROWS)])          # rebuild: no stamps
    write_rows(INDEX, [row(i) for i in range(N_ROWS, N_ROWS + N_APPEND)], mode="a")
    n_after = N_ROWS + N_APPEND
    p = start()
    try:
        g = http("/v1/state/regrow", {})
        mode = g["regrow"]["mode"]
        # the contract: an incremental regrow is only legal if the prefix
        # stamps are still routable. A rebuild wiped them -> the server must
        # not claim a working incremental path over a dead prefix.
        served = read_stamps(STAMP) or []
        if mode == "incremental":
            prefix_ok = len(served) >= N_ROWS and all(
                a >= 0 for a, b in served[:N_ROWS])
            ok(prefix_ok, "S2 incremental keeps the prefix routable",
               f"mode=incremental prefix_routed="
               f"{sum(1 for a,b in served[:N_ROWS] if a>=0)}/{N_ROWS}")
        else:
            ok(mode in ("full", "error", "race"),
               "S2 rebuild+append is not a wiped incremental", f"mode={mode}")
        # whichever mode ran, the SERVED stamps for the prefix must be live
        js2 = jsonl_stamps(INDEX)
        side2 = read_stamps(STAMP) or []
        eff = side2[:len(js2)] if len(side2) == len(js2) else js2
        live = sum(1 for a, b in eff[:N_ROWS] if a >= 0)
        ok(live >= N_ROWS // 2, "S2 served prefix stamps are routable",
           f"live={live}/{N_ROWS} (mode={mode})")

        # ── S3: the search must score a real fraction of the index ────────
        r = http("/v1/state/search", {"query": "geometric", "topk": 5})
        rt = r["route"]
        ok(rt["items_scored"] > N_APPEND,
           "S3 search scores more than the post-rebuild rows",
           f"items_scored={rt['items_scored']} mode={rt['mode']}")
        ok(not rt["mode"].startswith("brute"), "S3 not brute", rt["mode"])
        ok(len(r.get("hits", [])) >= 1, "S3 hits>=1", str(len(r.get("hits", []))))

        # ── S4: a forced FULL regrow must leave served == index ──────────
        g = http("/v1/state/regrow", {"force": 1})
        ok(g["regrow"]["mode"] == "full", "S4 forced mode full", g["regrow"]["mode"])
        js4 = jsonl_stamps(INDEX)
        side4 = read_stamps(STAMP) or []
        eff4 = side4[:len(js4)] if len(side4) == len(js4) else js4
        live4 = sum(1 for a, b in eff4 if a >= 0)
        ok(live4 >= len(js4) // 2, "S4 after a full regrow the served stamps are live",
           f"live={live4}/{len(js4)} sidecar={len(side4)} jsonl={len(js4)}")
        r = http("/v1/state/search", {"query": "geometric", "topk": 5})
        rt4 = r["route"]
        ok(rt4["items_scored"] > N_APPEND, "S4 search still scores the index",
           f"items_scored={rt4['items_scored']} mode={rt4['mode']}")

        # ── S5: the RESTAMP repair fixes a wiped routing table WITHOUT
        #        rewriting the jsonl (the vectors must not change) ───────
        write_rows(INDEX, [row(i) for i in range(n_after)])   # rebuild: no stamps
        os.remove(STAMP) if os.path.exists(STAMP) else None
        size_before = os.path.getsize(INDEX)
        mtime_before = os.stat(INDEX).st_mtime
        h0 = http("/health", to=10)
        ok(int(h0.get("stamps_live", -1)) == 0, "S5 rebuilt index has no live stamps",
           f"stamps_live={h0.get('stamps_live')}")
        ok(int(h0.get("route_dead", 0)) == 1, "S5 health reports route_dead",
           f"route_dead={h0.get('route_dead')}")
        r0 = http("/v1/state/search", {"query": "geometric", "topk": 5})
        ok(r0["route"]["mode"] == "dead-route",
           "S5 a dead route fails loud instead of serving a few rows",
           r0["route"]["mode"])
        rs = http("/v1/state/restamp", {})
        ok(rs.get("status") == "ok", "S5 restamp repaired", str(rs))
        ok(os.path.getsize(INDEX) == size_before, "S5 restamp did not rewrite the jsonl",
           f"{os.path.getsize(INDEX)} vs {size_before}")
        ok(os.stat(INDEX).st_mtime == mtime_before, "S5 restamp left the jsonl mtime",
           "")
        side5 = read_stamps(STAMP) or []
        live5 = sum(1 for a, b in side5 if a >= 0)
        ok(live5 >= n_after // 2, "S5 the sidecar now carries live stamps",
           f"live={live5}/{len(side5)}")
        h5 = http("/health", to=10)
        ok(int(h5.get("route_dead", 1)) == 0, "S5 health no longer reports route_dead",
           f"route_dead={h5.get('route_dead')} stamps_live={h5.get('stamps_live')}")
        r5 = http("/v1/state/search", {"query": "geometric", "topk": 5})
        ok(r5["route"]["items_scored"] > N_APPEND,
           "S5 the search scores the index again",
           f"items_scored={r5['route']['items_scored']} mode={r5['route']['mode']}")

        # ── S6: TREE-SIDE SEPARATION (2026-10-20) ───────────────────────
        # The INCREMENTAL regrow must NOT re-derive the prefix labels from
        # the fruit's vectors: the labels belong to the TREE (the sidecar),
        # the fruit carries no tree state. An append whose tree labels are
        # gone must fail LOUD (no_tree) — it must not silently read 62k
        # vectors and re-place the whole corpus inside the regrow.
        # The repair is explicit: POST /v1/state/restamp.
        os.remove(STAMP) if os.path.exists(STAMP) else None
        write_rows(INDEX, [row(i) for i in range(n_after)])   # no stamps in the fruit
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(row(n_after) + "\n")                       # append one NEW row
        g = http("/v1/state/regrow", {})
        mode6 = g["regrow"]["mode"]
        ok(mode6 == "no_tree",
           "S6 an append with no tree labels fails loud (no_tree)",
           f"mode={mode6}")
        ok(not os.path.exists(STAMP),
           "S6 the failed regrow wrote no labels", "")
        js6 = jsonl_stamps(INDEX)
        ok(all(a == -1 and b == -1 for a, b in js6),
           "S6 the fruit still carries no tree state", "")
        # the explicit repair puts the fruit back on the tree
        rs6 = http("/v1/state/restamp", {})
        ok(rs6.get("status") == "ok", "S6 restamp re-places the fruit", str(rs6))
        side6 = read_stamps(STAMP) or []
        live6 = sum(1 for a, b in side6 if a >= 0)
        ok(live6 >= (n_after + 1) // 2, "S6 the tree owns the labels again",
           f"live={live6}/{len(side6)}")

        # ── S7: the FRUIT is never rewritten (2026-10-20) ───────────────
        # A FULL regrow retrains the tree (it reads the fruit's vectors —
        # that IS training) and may re-place every row. But the fruit's
        # BYTES must not change: the old jsonl rewrite re-serialized every
        # vector with "%.6g" (6 significant digits) while build_index.py
        # wrote round(x, 6) (6 decimals) — a lossy re-encoding.
        import hashlib
        with open(INDEX, "rb") as f:
            md_before = hashlib.md5(f.read()).hexdigest()
        st_before = os.stat(INDEX)
        g = http("/v1/state/regrow", {"force": 1})
        ok(g["regrow"]["mode"] == "full", "S7 forced full regrow ran",
           g["regrow"]["mode"])
        with open(INDEX, "rb") as f:
            md_after = hashlib.md5(f.read()).hexdigest()
        ok(md_before == md_after,
           "S7 the fruit is byte-identical after a full regrow",
           f"{md_before} vs {md_after}")
        ok(os.stat(INDEX).st_mtime == st_before.st_mtime,
           "S7 the fruit's mtime is untouched", "")
        side7 = read_stamps(STAMP) or []
        live7 = sum(1 for a, b in side7 if a >= 0)
        ok(live7 >= (n_after + 1) // 2,
           "S7 the tree carries the fresh labels", f"live={live7}/{len(side7)}")
        r7 = http("/v1/state/search", {"query": "geometric", "topk": 5})
        ok(r7["route"]["items_scored"] > N_APPEND,
           "S7 the search still scores the index",
           f"items_scored={r7['route']['items_scored']} mode={r7['route']['mode']}")

        # ── S8: THE APPEND WINDOW (2026-10-20) ──────────────────────────
        # Between ann-sync's append and its regrow the sidecar covers FEWER
        # rows than the index. The tree's labels for the covered rows are
        # still the tree's — the read path must apply them (the old rule
        # "overlay only when size == n*8" threw the whole routing table
        # away in this window: measured live 2026-10-20, stamps_live 0 of
        # 62,323 while the labels were on disk).
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(row(n_after + 1) + "\n")          # one more NEW row
        n8 = n_after + 2
        side8 = read_stamps(STAMP) or []
        ok(len(side8) < n8, "S8 the sidecar is shorter than the index",
           f"sidecar_rows={len(side8)} index_rows={n8}")
        h8 = http("/health", to=10)
        live8 = int(h8.get("stamps_live", 0))
        ok(live8 >= len(side8) - 1 and live8 > 0,
           "S8 the covered rows keep their tree labels in the append window",
           f"stamps_live={live8} sidecar_rows={len(side8)} index_rows={n8}")
        ok(int(h8.get("route_dead", 1)) == 0,
           "S8 the append window is not a dead route",
           f"route_dead={h8.get('route_dead')}")

        # ── S9: A REORDERED FRUIT (2026-10-20) ───────────────────────────
        # The sidecar's labels are indexed by ROW POSITION. If a rebuild
        # reorders the fruit's rows, the tree's labels would silently
        # point at the wrong rows (silent mis-routing, no error anywhere).
        # The append contract must catch it: the boundary sid no longer
        # matches -> NOT incremental -> the full path re-places every row
        # from its own vector. The fruit is still never rewritten.
        #
        # S9 needs its OWN server: the one started for S2 still holds the
        # port (it serves S3..S8), so it must be stopped first — and its
        # handle must NOT be rebound, or the outer finally would stop the
        # wrong process and leave the port held.
        stop(p)
        p = None
        p9 = start_extra({"ANN_WALK_FLOOR": "0.5"})
        try:
            g = http("/v1/state/regrow", {"force": 1})
            ok(g["regrow"]["mode"] == "full", "S9 the reorder base regrow ran",
               g["regrow"]["mode"])
        finally:
            stop(p9)
        with open(INDEX, encoding="utf-8") as f:
            rows9 = f.readlines()
        write_rows(INDEX, list(reversed(rows9)))     # same rows, new positions
        md9_before = md5(INDEX)
        p9 = start_extra({"ANN_WALK_FLOOR": "0.5"})
        try:
            g = http("/v1/state/regrow", {})
            mode9 = g["regrow"]["mode"]
            ok(mode9 != "incremental",
               "S9 a reordered fruit is not served as an incremental append",
               f"mode={mode9}")
            ok(md5(INDEX) == md9_before,
               "S9 the reordered fruit is not rewritten", "")
            r9 = http("/v1/state/search", {"query": "geometric", "topk": 5})
            ok(int(r9["route"]["items_scored"]) > N_APPEND,
               "S9 the search scores the index after the reorder",
               f"items_scored={r9['route']['items_scored']} mode={r9['route']['mode']}")
            h9 = http("/health", to=10)
            ok(int(h9.get("route_dead", 1)) == 0,
               "S9 the route survives the reorder",
               f"route_dead={h9.get('route_dead')}")
        finally:
            stop(p9)
    finally:
        if p is not None:
            stop(p)
        try:
            p._errlog.close()
        except Exception:
            pass
        err = open(os.path.join(SCRATCH, "srv_err.log"), encoding="utf-8").read()
        for line in err.splitlines()[-6:]:
            print("   ", line)
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)" if not fails
          else f"{len(fails)} FAILURES")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
