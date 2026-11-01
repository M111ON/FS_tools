"""bench_zone_ab_live.py — zone scope ON vs OFF on the LIVE server, one arm at a
time, started through ann_keepalive._start_ann() so both arms inherit exactly
the production env (TOTEM_WARM, FLOAT_KERNEL, buckets, budget, prune, ...).

One process at a time on purpose: a second server pointed at the live anchors
would rewrite sidecars under the first one. Between arms the server is stopped
and the scheduled task re-armed.

The only thing that differs between arms is ANN_ZONE_ROWS. Measurements come
from bench_zone_floor.py (real row vectors; a gaussian probe would measure the
full-scan fallback instead).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
PORT = 8096


def health(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=5).read())
            if d.get("status") in ("ok", "degraded") and d.get("entries", 0) > 1000:
                return d
        except Exception:
            pass
        time.sleep(1)
    return None


def kill_live():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "$p=(Get-NetTCPConnection -LocalPort 8096 -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
                    "if($p){Stop-Process -Id $p -Force}"], capture_output=True)
    time.sleep(3)


def start_arm(zone_rows):
    import ann_keepalive as ka
    os.environ["ANN_ZONE_ROWS"] = "1" if zone_rows else "0"
    p = ka._start_ann()
    h = health()
    if not h:
        p.kill()
        raise RuntimeError("live server did not come up")
    return p, h


def run_floor(out):
    r = subprocess.run([sys.executable, os.path.join(REPO, "bench_zone_floor.py"),
                        "--queries", "60", "--url", f"http://127.0.0.1:{PORT}"],
                       capture_output=True, text=True, timeout=2400)
    text = r.stdout + r.stderr
    open(out, "w", encoding="utf-8").write(text)
    return text


def main():
    kill_live()
    results = {}
    for arm in (False, True):
        p, h = start_arm(arm)
        try:
            print(f"arm ANN_ZONE_ROWS={int(arm)}: entries {h['entries']} "
                  f"roster {h.get('roster', {}).get('src')}", flush=True)
            results[arm] = run_floor(os.path.join(REPO, "build",
                                                  f"zone_floor_{'on' if arm else 'off'}.txt"))
        finally:
            p.poll() or p.kill()
            try:
                p.wait(timeout=10)
            except Exception:
                pass
            time.sleep(2)
    kill_live()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Start-ScheduledTask -TaskName 'ann-keepalive'"], capture_output=True)
    h = health(240)
    print("live restored:", bool(h), h.get("entries") if h else "-")

    for arm in (False, True):
        print(f"\n=== ANN_ZONE_ROWS={int(arm)} ===")
        for line in results[arm].splitlines():
            if "p50" in line or "floor" in line or line.startswith("zone:"):
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
