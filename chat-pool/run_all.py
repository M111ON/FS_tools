"""run_all.py — run every test suite and report combined results.

7 suites, one command:
  python run_all.py

Exit 0 = all green. Exit 1 = at least one failure, reported with details.
No dependencies beyond stdlib.
"""
import os
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ("pool.py demo",      [sys.executable, "pool.py", "demo"]),
    ("ctx_plane",         [sys.executable, "ctx_plane.py", "demo"]),
    ("ui_check",          [sys.executable, "ui_check.py"]),
    # Drives the real dashboard in a real browser and grades what it RENDERED
    # against the corpus. Eight wiring bugs reached a green run_all because
    # every other check here is arithmetic or a regex; this one clicks.
    ("ui_browser_check",  [sys.executable, "ui_browser_check.py"]),
    ("stream_check",      [sys.executable, "stream_check.py"]),
    ("prune_check",       [sys.executable, "prune_check.py"]),
    ("reconnect_check",   [sys.executable, "reconnect_check.py"]),
    ("error_check",       [sys.executable, "error_check.py"]),
    ("curator_check",     [sys.executable, "curator_check.py"]),
    ("latency_check",     [sys.executable, "latency_check.py"]),
    ("taskboard demo",    [sys.executable, "taskboard.py"]),
    ("taskboard_check",   [sys.executable, "taskboard_check.py"]),
    ("keepalive demo",    [sys.executable, "pool_keepalive.py", "demo"]),
]


def main():
    results = []
    t0 = time.time()

    for name, cmd in SUITES:
        t1 = time.time()
        try:
            r = subprocess.run(
                cmd, cwd=DIR, capture_output=True, text=True, timeout=300)
            elapsed = time.time() - t1
            out = (r.stdout + r.stderr).strip()
            ok = r.returncode == 0
            # last line is usually the summary
            summary = out.splitlines()[-1] if out else "(no output)"
            results.append((name, ok, elapsed, summary))
            tag = "PASS" if ok else "FAIL"
            print("  %s  %-20s  %5.1fs  %s" % (tag, name, elapsed, summary))
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t1
            results.append((name, False, elapsed, "TIMEOUT (300s)"))
            print("  FAIL  %-20s  %5.1fs  TIMEOUT (300s)" % (name, elapsed))
        except Exception as e:
            elapsed = time.time() - t1
            results.append((name, False, elapsed, str(e)))
            print("  FAIL  %-20s  %5.1fs  %s" % (name, elapsed, e))

    total = len(results)
    passed = sum(1 for _, ok, _, _ in results if ok)
    failed = total - passed
    wall = time.time() - t0

    print("\n  ── summary ──")
    print("  %d/%d suites PASS  (%.1fs wall time)" % (passed, total, wall))
    if failed:
        print("  FAILED: %s" % ", ".join(n for n, ok, _, _ in results if not ok))
        print("\n  details:")
        for name, ok, elapsed, summary in results:
            if not ok:
                print("    %s: %s" % (name, summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
