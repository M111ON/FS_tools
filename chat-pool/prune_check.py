"""prune_check.py — does prune reach PAST the map, over HTTP?

pool.py's own demo proves the library layer. This proves the thing a
reader actually talks to: a server, on a real log, answering over HTTP.

The real corpus is genuinely clean (0 lines pruned), so it can never
show whether a stranded edge is held back. This builds a sandbox with
noise on purpose, then asks the running server the only questions that
matter:

  * a bond whose far end was pruned — gone by default, present with
    &raw=1 and marked `dangling`
  * a sighting into an emptied address — same contract
  * a memory link whose LOG end was pruned — same contract
  * and the count of what was held back is IN the answer, so a client
    never has to diff two requests to notice something was hidden

Run:  python prune_check.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COPIES = ("pool.py", "pool_server.py", "ctx_plane.py")


# ── harness ──────────────────────────────────────────────────────────────
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(port, path, timeout=8):
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # An endpoint that 500s is a finding, not an unknown: say what it said.
        body = e.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError("GET %s -> HTTP %d: %s" % (path, e.code, body)) from None


def wait_health(port, proc, deadline=25.0):
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if get(port, "/health", timeout=2).get("ok"):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    return False


# ── the sandbox log ──────────────────────────────────────────────────────
def build_log(box):
    """Write a log whose noise is REAL noise, then return the addresses of
    lines that prune actually emptied — so the assertions target cases the
    rules genuinely produced instead of cases the test hoped for."""
    sys.path.insert(0, box)
    for mod in [m for m in ("pool", "ctx_plane") if m in sys.modules]:
        del sys.modules[mod]
    import pool

    genesis = {
        "v": 3, "t": time.time(), "app": "system", "kind": "CHAT",
        "text": "prune_check genesis", "to": "all", "tags": [], "prev": "",
        "rkey": pool.rkey(b"prune-check-genesis"), "enc": 0,
    }
    import hashlib
    genesis["hash"] = hashlib.sha256(
        ("" + "system" + "CHAT" + "all" + genesis["text"]).encode("utf-8")
    ).hexdigest()
    with open(os.path.join(box, "log.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(genesis, ensure_ascii=False) + "\n")

    # real work: these must survive prune
    anchors = []
    for i, text in enumerate([
        "DECISION: prune must reach the edge layer, not just the plane map",
        "HANDOFF: bonds and sightings are addressed, so they can be stranded",
        "SESSION: verifying that a hidden edge is still counted, never silent",
    ]):
        anchors.append(pool.append("opencode", "chat", text))

    # noise: several shapes so at least one lands in a cell of its own
    noise = []
    for i in range(24):
        noise.append(pool.append("probe", "chat", ["test", "test probe",
                                                   "test probe %d" % i,
                                                   "???"][i % 4]))

    # which noise lines actually emptied their address?
    d = pool.derived()
    clean = set(d["cells_clean"])
    empty = [n for n in noise if n["rkey"] not in clean]

    # strand an edge on every emptied address we can
    for n in empty[:6]:
        pool.append("opencode", "bond",
                    "%s produces %s" % (anchors[0]["hash"], n["hash"]))
    # strand sightings the same way. They must come from DIFFERENT apps:
    # pointers_at() keeps the latest sighting per app, so four reads by one
    # app are one current read, not four — a test that ignored that would
    # be measuring the wrong thing.
    for i, n in enumerate(empty[:4]):
        pool.sighting(n["rkey"], level=0, app="watcher%d" % i)

    return pool, {
        "anchors": anchors,
        "noise": noise,
        "empty": empty,
        "stats": pool.plane_prune_stats(),
    }


# ── checks ───────────────────────────────────────────────────────────────
def main():
    box = tempfile.mkdtemp(prefix="prune-check-")
    results = []
    proc = None
    try:
        for name in COPIES:
            src = os.path.join(HERE, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(box, name))

        _pool, made = build_log(box)
        n_empty = len(made["empty"])
        st = made["stats"]
        print("prune_check: sandbox %s" % box)
        print("  log: %d lines · pruned %d · cells %d -> %d · emptied addresses %d"
              % (st["lines"], st["pruned"], st["cells_all"],
                 st["cells_clean"], n_empty))

        if st["pruned"] == 0 or n_empty == 0:
            print("  FAIL harness: the noise did not prune — cannot test")
            print("\nprune_check: 0/1 PASS")
            return 1
        results.append(("harness-prunes", True,
                        "pruned %d lines, emptied %d addresses"
                        % (st["pruned"], n_empty)))

        port = free_port()
        env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8")
        out = open(os.path.join(box, "server.out"), "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(box, "pool_server.py")],
            cwd=box, env=env, stdout=out, stderr=subprocess.STDOUT)
        if not wait_health(port, proc):
            print("  FAIL server: did not come up on %d" % port)
            tail = open(os.path.join(box, "server.out"), encoding="utf-8",
                        errors="ignore").read()[-800:]
            print("  --- server output ---\n%s" % tail)
            return 1

        # 1 the report names what was stranded
        cur = get(port, "/curator?prune=1")
        plane = cur.get("plane", {})
        results.append((
            "stats-count-dangling",
            plane.get("dangling_bonds", 0) >= 6
            and plane.get("dangling_sightings", 0) >= 4,
            "bonds=%s sightings=%s"
            % (plane.get("dangling_bonds"), plane.get("dangling_sightings"))))

        # 2 the plane map really shrank
        results.append((
            "map-shrinks",
            plane.get("cells_clean", 0) < plane.get("cells_all", 0),
            "%s -> %s cells" % (plane.get("cells_all"), plane.get("cells_clean"))))

        # 3 edges: default holds them back, raw=1 shows them marked
        live_e = get(port, "/bonds")["edges"]
        all_e = get(port, "/bonds?raw=1")["edges"]
        gone = [e for e in all_e if e.get("dangling")]
        results.append((
            "bonds-held-back",
            len(live_e) < len(all_e) and not any(e.get("dangling") for e in live_e),
            "%d live of %d · %d dangling" % (len(live_e), len(all_e), len(gone))))
        results.append((
            "bonds-raw-marked",
            len(gone) >= 6 and all(e.get("hash") for e in gone),
            "every stranded edge carries `dangling` and its hash"))

        # 4 sightings: same contract
        live_s = get(port, "/pointers")["pointers"]
        all_s = get(port, "/pointers?raw=1")["pointers"]
        gone_s = [s for s in all_s if s.get("dangling")]
        results.append((
            "sightings-held-back",
            len(live_s) < len(all_s) and len(gone_s) >= 4,
            "%d live of %d · %d into emptied addresses"
            % (len(live_s), len(all_s), len(gone_s))))

        # 5 a sighting into a LIVE address is never falsely stranded
        live_rkeys = [n["rkey"] for n in made["noise"]
                      if n["rkey"] in set(_pool.derived()["cells_clean"])]
        if live_rkeys:
            keep = get(port, "/pointers?rkey=%d&level=0" % live_rkeys[0])
            results.append((
                "no-false-positive",
                all(not s.get("dangling") for s in keep["pointers"]),
                "a read into a live address stays live"))

        # 5b a read that declares itself. This is the only honest source of a
        # sighting, so the properties that make it safe to wire into a view
        # are worth proving on a live server: re-reading the SAME address
        # writes nothing, a NEW address supersedes, and superseding must also
        # stop the OLD point reporting that reader — a filtered read that
        # walked back through an app's history would keep a stale circle on
        # the plane.
        live_cells = sorted(_pool.derived()["cells_clean"])
        # pick by fraction, not by index: the sandbox is small and a fixed
        # index would be testing the harness's arithmetic, not the server
        a = live_cells[len(live_cells) // 4]
        b = live_cells[len(live_cells) * 3 // 4]
        r1 = get(port, "/neighbors?rkey=%d&level=0&record=1&as=reader1" % a)
        results.append((
            "read-declares-itself",
            r1.get("sighting_written") is True,
            "a deliberate read writes a sighting"))
        r2 = get(port, "/neighbors?rkey=%d&level=0&record=1&as=reader1" % a)
        results.append((
            "re-read-is-idempotent",
            r2.get("sighting_written") is False,
            "the same address twice writes nothing the second time"))
        get(port, "/neighbors?rkey=%d&level=0&record=1&as=reader1" % b)
        at_a = [s for s in get(port, "/pointers?rkey=%d&level=0" % a)["pointers"]
                if s.get("app") == "reader1"]
        at_b = [s for s in get(port, "/pointers?rkey=%d&level=0" % b)["pointers"]
                if s.get("app") == "reader1"]
        results.append((
            "superseded-read-is-gone",
            not at_a and len(at_b) == 1,
            "after moving, the old point no longer reports that reader"))
        mine = [s for s in get(port, "/pointers")["pointers"]
                if s.get("app") == "reader1"]
        results.append((
            "one-read-per-app",
            len(mine) == 1 and mine[0]["point"] == b,
            "three reads by one app = one current pointer, not three"))

        # 6 the memory layer follows the same rule. The cross-link DANGLING
        # logic itself is proven in ctx_plane's own demo with synthetic lifted
        # rows (it needs rows in context.db, which this test must not write).
        # What is worth proving over HTTP is the contract around it: a
        # cross-system question answers even with the index cache unbuilt,
        # and the count of what was held back is IN the answer.
        links = get(port, "/ctx/links")
        results.append((
            "ctx-answers-unbuilt-index",
            all(k in links for k in ("links", "total", "dangling", "cross"))
            and links["dangling"] == links["total"] - len(links["links"]),
            "no 500 with no index.db · total=%s live=%d dangling=%s"
            % (links.get("total"), len(links.get("links", [])), links.get("dangling"))))
        raw_links = get(port, "/ctx/links?raw=1")
        results.append((
            "ctx-raw-returns-everything",
            len(raw_links.get("links", [])) == raw_links.get("total", -1)
            and len(raw_links.get("links", [])) >= len(links.get("links", [])),
            "raw=%d total=%s vs live=%d" % (len(raw_links.get("links", [])),
                                            raw_links.get("total"),
                                            len(links.get("links", [])))))

    except Exception as exc:  # noqa: BLE001 — report, do not hide
        results.append(("no-traceback", False, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(box, ignore_errors=True)

    ok = 0
    for name, passed, note in results:
        print("  %s %-28s %s" % ("PASS" if passed else "FAIL", name, note))
        ok += bool(passed)
    print("\nprune_check: %d/%d PASS" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
