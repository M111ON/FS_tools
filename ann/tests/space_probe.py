"""Space-preservation probe: if a binary drops U+0020, "alpha beta" and
"alphabeta" embed to the same string and return the same hits."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_text_parity import boot, search  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--exe", required=True)
ap.add_argument("--data", required=True)
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()

p, h = boot(a.exe, a.data, a.port, "I:/tmp/space_probe.log")
try:
    for qa, qb in (("what is the source path", "whatisthesourcepath"),
                   ("sha warma", "shawarma")):
        ra, rb = search(a.port, qa), search(a.port, qb)
        print("%s | '%s' vs '%s' -> %s" % (
            os.path.basename(a.exe), qa, qb,
            "IDENTICAL (spaces dropped)" if ra == rb else "different (spaces kept)"))
finally:
    p.kill()
