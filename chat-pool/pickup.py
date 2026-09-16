"""pickup.py — session-start handoff reader (fresh process, no prior state).
Reads pool tail + SPEC + git log, prints everything a new session needs."""
import os
import subprocess
import sys
sys.path.insert(0, r"I:\tools\chat-pool")
import pool

print("=== POOL TAIL (last 30) ===")
for m in pool.tail(30):
    print(f"[{m['app']}/{m['kind']}] {m['text'][:120]}")
print("\n=== SPEC HEAD ===")
spec = r"I:\tools\chat-pool\SPEC.md"
print("\n".join(open(spec, encoding="utf-8").read().splitlines()[:8]))
print("\n=== REPO ===")
r = subprocess.run(["git", "log", "--oneline", "-3"], capture_output=True,
                   text=True, cwd=r"I:\tools\chat-pool")
print(r.stdout.strip())
print("\n=== SHARED MEMO ===")
print("ctx 946-950 (framework/geometry/rules/constraints/ops) + cloud opencode-proof-2026-09-16")
