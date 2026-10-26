#!/usr/bin/env python3
"""probe_door_exit.py — is ANN_DOOR_EXIT actually reachable?

The live A/B showed the EXIT arm scoring exactly as many rows as the plain
door (33,574 = 33,574), i.e. the early exit never fired. Two possible
causes, and they need different fixes:
  (a) the exit code is unreachable (a bug) -> it must be fixed or removed
  (b) the exit threshold is never cleared in this corpus -> the feature is
      correct but has no work to do here

This probe decides between them: with a very LOW exit floor (0.05) the
walk must stop after the first row that scores above it. If items_scored
stays at the full corpus, the exit is dead code (a). If it drops, the exit
works and the live A/B just found no rows worth stopping at (b).

Scratch server on port 8097 only. NEVER the live :8096.
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
EXE = os.environ.get("ANN_TEST_EXE") or os.path.join(REPO, "build", "ann_search_batch.exe")
SRC = os.path.join(REPO, "data", "memcore")
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-door-exit-probe")
PORT = 8097
H = "http://127.0.0.1:%d" % PORT

fails = []


def ok(c, n, d=""):
    if not c:
        fails.append(n)
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=300):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = Request(H + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def start(env_extra):
    idx = os.path.join(SCRATCH, "index")
    os.makedirs(idx, exist_ok=True)
    for fn in ("ann_index.jsonl", "ann_index.order", "ann_anchors.bin",
               "ann_anchors-state.json", "ann_index.jsonl.stamp"):
        s = os.path.join(SRC, fn)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(idx, fn))
    env = {**os.environ,
           "ANN_INDEX_PATH": os.path.join(idx, "ann_index.jsonl"),
           "ANN_ANCH_PATH": os.path.join(idx, "ann_anchors.bin"),
           "ANN_ORDER_PATH": os.path.join(idx, "ann_index.order"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOTEM_WARM": "1",
           "ANN_WALK_PRUNE": "1",
           "ANN_FLOAT_KERNEL": "1",
           "ANN_WALK_FLOOR": "2.1",     # force every query below the floor
           **env_extra}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "a", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    for _ in range(120):
        time.sleep(0.5)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start")


def query(q):
    return http("/v1/state/search", {"query": q, "topk": 5})


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)

    print("== baseline: door, no exit ==")
    p = start({"ANN_DOOR": "1", "ANN_DOOR_EXIT": "0"})
    try:
        r1 = query("quantum chromodynamics lattice gauge")
        r2 = query("quantum chromodynamics lattice gauge")   # 2nd: bucket is SHUT
        m2 = r2["route"]["mode"]
        s2 = r2["route"]["items_scored"]
        print("   mode=%s items_scored=%d" % (m2, s2))
        ok(m2 == "door", "baseline takes the door path", m2)
        base_scored = s2
    finally:
        p.poll() or p.kill()

    print("== exit arm: door + ANN_DOOR_EXIT=1, exit floor 0.05 ==")
    p = start({"ANN_DOOR": "1", "ANN_DOOR_EXIT": "1", "ANN_DOOR_EXIT_FLOOR": "0.05"})
    try:
        q1 = query("quantum chromodynamics lattice gauge")
        r2 = query("quantum chromodynamics lattice gauge")
        m2 = r2["route"]["mode"]
        s2 = r2["route"]["items_scored"]
        print("   mode=%s items_scored=%d" % (m2, s2))
        ok(m2 == "door", "exit arm takes the door path", m2)
        ok(s2 < base_scored,
           "the early exit actually stops the walk (scored < baseline)",
           "exit=%d baseline=%d" % (s2, base_scored))
        if s2 >= base_scored:
            print("   -> the exit code is DEAD (never reached): the feature")
            print("      must be fixed or removed, not shipped as a knob.")
    finally:
        p.poll() or p.kill()
        try:
            err = open(os.path.join(SCRATCH, "srv_err.log"), encoding="utf-8").read()
            for line in err.splitlines()[-8:]:
                print("   ", line)
        except Exception:
            pass
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)"
          if not fails else f"{len(fails)} FAILURES")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
