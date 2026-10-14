#!/usr/bin/env python3
"""test_p3_compact.py — tests for scripts/p3_compact.py (P3 route-log compactor).

Run:  python tests/test_p3_compact.py        (stdlib only)
      make p3-test                           (wired into `make test`)

The compactor is lossless BY CONSTRUCTION: replay is last-event-wins per
(name_hash, slot) and CLEAR is the only reopen, so superseded records carry no
information. These tests pin that claim, the refusals, and — via the REAL C
replay (tests/p3_compact_verify.c, the same wl_replay_log_file the server runs
at boot) — that a compacted file replays bit-identically.

Exit 0 = all pass.
"""
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ANN = os.path.dirname(HERE)
HDR = b"IR\x01"
WL = 0x77616E676C617463          # "wanglatc" — the latch name_hash
WRITE, CLEAR, MOVE, ANCHOR = 0, 1, 2, 3
GCC = next((g for g in (r"C:\mingw64\bin\gcc.exe", r"C:\msys64\mingw64\bin\gcc.exe")
            if os.path.exists(g)), "gcc")

_fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (("  -- " + detail) if detail else ""))
    if not ok:
        _fails.append(name)


def load_compactor():
    """Import scripts/p3_compact.py by path (it is a script, not a package)."""
    path = os.path.join(ANN, "scripts", "p3_compact.py")
    spec = importlib.util.spec_from_file_location("p3_compact", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rec(slot, action, nh=WL, rnd=0, tick=0, corrupt=False):
    b = struct.pack("<IQIIIII", slot, nh, rnd, tick, action, 0, 0)
    crc = zlib.crc32(b[:28]) & 0xFFFFFFFF
    return b[:28] + struct.pack("<I", crc ^ (0xFFFFFFFF if corrupt else 0))


def main():
    m = load_compactor()

    # ── replay-state preservation (the lossless claim) ───────────────────
    print("[edge cases] compaction must preserve replay state")
    cases = {
        "repeat WRITE collapses":     [rec(72, WRITE)] * 5,
        "WRITE then CLEAR":           [rec(72, WRITE), rec(72, CLEAR)],
        "lone CLEAR on virgin slot":  [rec(72, CLEAR)],
        "WRITE,CLEAR,WRITE":          [rec(72, WRITE), rec(72, CLEAR), rec(72, WRITE)],
        "ANCHOR carries no state":    [rec(72, ANCHOR)],
        "mixed slots + ANCHOR":       [rec(1, WRITE), rec(2, ANCHOR), rec(1, WRITE), rec(2, WRITE)],
        "MOVE counts as a write":     [rec(9, MOVE)],
        "foreign name_hash kept":     [rec(1, WRITE, nh=0xDEAD)],
    }
    for name, recs in cases.items():
        before = m.replay_state(HDR, recs)
        kept = m.compact(HDR, recs)
        check(name, before == m.replay_state(HDR, kept), f"{len(recs)}->{len(kept)}")

    # ── refusals (never silently drop corruption) ────────────────────────
    print("[refusals] bad input must raise, not compact")
    for name, raw in (("bad CRC", HDR + rec(72, WRITE) + rec(72, WRITE, corrupt=True)),
                      ("bad magic", b"XX\x01" + rec(72, WRITE)),
                      ("trailing junk", HDR + rec(72, WRITE) + b"junk")):
        try:
            m.parse(raw)
            check(f"refuse {name}", False, "did not raise")
        except ValueError:
            check(f"refuse {name}", True)

    # ── idempotence + shrink (data-independent) ──────────────────────────
    print("[properties] compaction shrinks a bloated log and is idempotent")
    bloated = HDR + b"".join(rec(72, WRITE) for _ in range(500))
    h2, r2 = m.parse(bloated)
    once = m.compact(h2, r2)
    twice = m.compact(h2, once)
    check("bloated log shrinks", len(once) < len(r2), f"{len(r2)} -> {len(once)} records")
    check("compaction is idempotent", once == twice, f"{len(once)} == {len(twice)}")

    # ── real C replay round-trip (the load-bearing check) ────────────────
    print("[C replay] compacted file must replay bit-identically")
    log = os.path.join(ANN, "data", "memcore", "lz_route.log")
    if not os.path.exists(log):
        check("real log present", False, f"missing {log}")
    else:
        with tempfile.TemporaryDirectory(prefix="p3test-") as td:
            exe = os.path.join(td, "verify.exe")
            build = subprocess.run(
                [GCC, "-O2", "-std=c11", "-Wall", "-Wno-unused-parameter",
                 "-Iinclude", "-o", exe, "tests/p3_compact_verify.c", "-lm"],
                cwd=ANN, capture_output=True, text=True)
            check("p3_compact_verify builds", build.returncode == 0,
                  (build.stderr or "").strip()[:160])
            if build.returncode == 0:
                raw = open(log, "rb").read()
                header, recs = m.parse(raw)
                comp = os.path.join(td, "compacted.log")
                with open(comp, "wb") as f:
                    f.write(header + b"".join(m.compact(header, recs)))
                r = subprocess.run([exe, log, comp], capture_output=True, text=True)
                check("LATCH IDENTICAL (C wl_replay_log_file)",
                      r.returncode == 0 and "LATCH IDENTICAL : YES" in r.stdout,
                      f"rc={r.returncode}")

    print("\n" + ("ALL PASS" if not _fails else f"FAILURES: {_fails}"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
