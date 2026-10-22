"""Map + verify cycle for magic-context memory mappings.

One round = map every unmapped memory, then verify every unverified mapping.
A verification failure is a rejection: the mapping is dropped and the file is
recorded in map_rejects.json so the next round cannot pick it again, so each
round strictly shrinks the candidate set and the run converges on
file-independent instead of spinning.

Usage:
  python mc_cycle.py --state          # counters only
  python mc_cycle.py --rounds 6       # run until stable / 6 rounds
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = r"I:\cortexkit\magic-context\context.db"


def state():
    """Counters use the plugin's own predicates (status + expires_at) so the
    printed numbers match what magic-context's dashboard shows."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    now = time.time() * 1000
    live = "m.status IN ('active','permanent') AND (m.expires_at IS NULL OR m.expires_at > ?)"
    unmapped = con.execute(
        f"SELECT COUNT(*) FROM memories m WHERE {live} "
        "AND NOT EXISTS (SELECT 1 FROM memory_verifications v WHERE v.memory_id=m.id)",
        (now,)).fetchone()[0]
    unverified = con.execute(
        f"SELECT COUNT(DISTINCT m.id) FROM memories m JOIN memory_verifications v "
        f"ON v.memory_id=m.id WHERE {live} AND v.file_path <> '' AND v.verified_at = 0",
        (now,)).fetchone()[0]
    verified = con.execute(
        f"SELECT COUNT(DISTINCT m.id) FROM memories m JOIN memory_verifications v "
        f"ON v.memory_id=m.id WHERE {live} AND v.file_path <> '' AND v.verified_at > 0",
        (now,)).fetchone()[0]
    independent = con.execute(
        f"SELECT COUNT(DISTINCT m.id) FROM memories m JOIN memory_verifications v "
        f"ON v.memory_id=m.id WHERE {live} AND v.file_path = ''",
        (now,)).fetchone()[0]
    con.close()
    return unmapped, unverified, verified, independent


def show(tag):
    u, v, ok, ind = state()
    print(f"{tag}: unmapped={u} unverified={v} verified={ok} independent={ind}", flush=True)
    return u, v


def run(script, extra=()):
    t = time.time()
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *extra],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-3:]
    for ln in tail:
        print(f"    {ln}", flush=True)
    if r.returncode != 0:
        print(f"    EXIT {r.returncode}: {(r.stderr or '').strip()[:400]}")
    print(f"    {script} took {round(time.time() - t)}s", flush=True)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="store_true")
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()

    if args.state:
        show("state")
        return 0

    prev = None
    for r in range(1, args.rounds + 1):
        print(f"\n=== round {r} ===", flush=True)
        run("map_standalone.py")
        u, v = show(f"  after map {r}")
        if v:
            run("verify_standalone.py")
            u, v = show(f"  after verify {r}")
        if (u, v) == prev:
            print("stable — stopping")
            break
        if u == 0 and v == 0:
            print("clean — stopping")
            break
        prev = (u, v)
    show("final")
    return 0


if __name__ == "__main__":
    sys.exit(main())
