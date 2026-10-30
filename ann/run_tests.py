#!/usr/bin/env python3
"""run_tests.py — canonical test entry point for the ann repo.

WHY THIS EXISTS
  The repo's tests are driven by a Makefile, but `make` is NOT on PATH on this
  box (it lives at C:/msys64/usr/bin/make.exe only). That makes `make test`
  unrunnable for anything that does not already know the msys path — including
  automated verifiers — so the suite had no portable way to be invoked.

  This script runs the SAME set the Makefile runs, in-process, with an explicit
  interpreter for the python parts (bare `python` has no numpy — measured).

USAGE
  python run_tests.py            # everything
  python run_tests.py --list     # list the tests

EXIT 0 = all pass.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
CC = next((c for c in (r"C:\mingw64\bin\gcc.exe", r"C:\msys64\mingw64\bin\gcc.exe")
           if os.path.exists(c)), "gcc")
PY = next((p for p in (r"I:\python3.14.4\python.exe", sys.executable)
           if os.path.exists(p)), sys.executable)
CFLAGS = ["-O2", "-std=c11", "-Wall", "-Wno-unused-parameter", "-Wno-sign-compare",
          "-Wno-format", "-Wno-unknown-pragmas", "-Iinclude"]

# C tests: (name, extra libs)
C_TESTS = ["test_anchor_route", "test_anchor_routed", "test_wang_latch",
           "test_anchor_letter", "test_anchor_lettercube",
           # chain container: address primitive, node, bucket join, head+tail
           "test_chain_addr", "test_chain_node", "test_chain_bucket",
           "test_chain_headtail", "test_chain_ident", "test_lcr_great"]
# python tests (need numpy unless noted)
PY_TESTS = ["tests/test_p3_compact.py", "tests/test_vec_format.py",
            "tests/test_scope.py", "tests/test_tag_lut.py",
            "tests/test_loader_swap.py", "tests/test_reuse_parse.py",
            "tests/test_roster.py", "tests/test_state_tags.py",
            "tests/test_route_watch.py", "tests/test_soft_zone.py",
            "tests/test_roster_cold.py", "tests/test_zone_rows.py",
            "tests/test_query_sanitize.py",
            "dashboard/tests/test_dashboard_core.py"]


def run(name, cmd, cwd=HERE):
    print(f"--- {name} ---")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
    for ln in tail:
        print("   " + ln)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for t in C_TESTS:
            print("c    ", t)
        for t in PY_TESTS:
            print("py   ", t)
        return 0

    os.makedirs(BUILD, exist_ok=True)
    ok = True
    for t in C_TESTS:
        exe = os.path.join(BUILD, t + ".exe")
        if not run(f"build {t}", [CC, *CFLAGS, "-o", exe, f"tests/{t}.c", "-lm"]):
            ok = False
            continue
        ok &= run(t, [exe])
    for t in PY_TESTS:
        ok &= run(os.path.basename(t), [PY, t])

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
