"""bench_zone_scratch_ab.py — ANN_ZONE_ROWS off vs on, on a COPY of the live
index (the live server keeps serving on :8096 untouched).

Two servers are never up at once: the same copy is served with the flag off,
measured, killed, then with the flag on, measured. Same bytes, same box, same
query list — the only difference is the env flag. Port 8103 is not supervised by
ann_keepalive, so nothing restarts under the measurement.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(REPO, "data", "memcore")
EXE = os.path.join(REPO, "build", "ann_search_bench.exe")
WORK = os.path.join(os.environ.get("TEMP", REPO), "ann_zone_scratch")
PORT = 8103
H = f"http://127.0.0.1:{PORT}"
FILES = ("ann_index.jsonl", "ann_tags.tsv", "ann_anchors.bin",
         "ann_anchors-roster.bin", "ann_index.order", "ann_tags.tsv.prev")
NQ = 40


def setup():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    for f in FILES:
        s = os.path.join(LIVE, f)
        if os.path.exists(s):
            shutil.copyfile(s, os.path.join(WORK, f))
    print(f"copied {sum(1 for f in FILES if os.path.exists(os.path.join(LIVE, f)))} files -> {WORK}",
          flush=True)


def kill_port():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$p=(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
                    "if($p){Stop-Process -Id $p -Force}"], capture_output=True)
    time.sleep(2)


def start(roster):
    env = os.environ.copy()
    env.update({
        "ANN_PORT": str(PORT),
        "ANN_INDEX_PATH": os.path.join(WORK, "ann_index.jsonl"),
        "ANN_TAGS_PATH": os.path.join(WORK, "ann_tags.tsv"),
        "ANN_ANCH_PATH": os.path.join(WORK, "ann_anchors.bin"),
        "ANN_ROSTER_PATH": os.path.join(WORK, "ann_anchors-roster.bin"),
        "ANN_ORDER_PATH": os.path.join(WORK, "ann_index.order"),
        "ANN_ROUTE_LOG": os.path.join(WORK, "route.log"),
        "ANN_ZONE_ROWS": "1",
        "ANN_ROSTER": "1" if roster else "0",
        # production knobs, same as ann_keepalive._start_ann
        "ANN_TOTEM_WARM": "1", "ANN_FLOAT_KERNEL": "1",
        "LZ_ROUTE_BUCKETS": "5", "LZ_SEARCH_BUDGET": "24000",
        "ANN_WALK_PRUNE": "1", "ANN_WALK_FLOOR": "0.5",
        "ANN_DOOR": "0", "ANN_DOOR_EXIT": "0",
    })
    log = open(os.path.join(WORK, f"server_roster_{int(roster)}.log"), "ab")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], cwd=WORK, env=env,
                         stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log)
    p._log = log
    t0 = time.time()
    while time.time() - t0 < 240:
        try:
            h = json.loads(urllib.request.urlopen(H + "/health", timeout=5).read())
            if h.get("entries", 0) > 1000:
                return p, h
        except Exception:
            pass
        time.sleep(2)
    p.kill()
    raise RuntimeError("scratch server did not answer /health")


def stop(p):
    p.poll() or p.kill()
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    p._log.close()


def bench():
    r = subprocess.run([sys.executable, os.path.join(REPO, "bench_zone_floor.py"),
                        "--queries", str(NQ), "--url", H],
                       capture_output=True, text=True, timeout=2400,
                       env={**os.environ,
                            "ANN_BENCH_INDEX": os.path.join(WORK, "ann_index.jsonl")})
    return r.stdout + r.stderr


def main():
    if not os.path.exists(EXE):
        print("missing exe")
        return 2
    setup()
    kill_port()
    out = {}
    for arm in (False, True):
        p, h = start(arm)
        try:
            print(f"\n### ANN_ROSTER={int(arm)}  entries {h['entries']}  "
                  f"tags {h.get('tags', {}).get('kinds')}", flush=True)
            out[arm] = bench()
            print(out[arm], flush=True)
        finally:
            stop(p)
            time.sleep(2)
    kill_port()
    open(os.path.join(REPO, "build", "_zone_scratch_ab.txt"), "w", encoding="utf-8").write(
        "\n".join(f"=== ANN_ROSTER={int(k)} ===\n{v}" for k, v in out.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
