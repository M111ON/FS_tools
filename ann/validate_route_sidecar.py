"""Offline validator for route sidecar v1; never loads or changes ANN state."""
import argparse
import hashlib
import json
from pathlib import Path

INTENTS = {"value", "relation", "shape", "composite", "unknown"}


def validate(path):
    seen = set()
    rows = 0
    canonical = bytearray()
    for no, raw in enumerate(Path(path).read_bytes().splitlines(), 1):
        obj = json.loads(raw)
        if obj.get("v") != 1 or not isinstance(obj.get("sid"), str):
            raise ValueError(f"line {no}: bad version or sid")
        sid = obj["sid"]
        if sid in seen:
            raise ValueError(f"line {no}: duplicate sid")
        seen.add(sid)
        if obj.get("intent") not in INTENTS:
            raise ValueError(f"line {no}: bad intent")
        for key in ("value_routes", "shape_routes", "junction_routes"):
            values = obj.get(key, [])
            if values != sorted(set(values)) or any(not isinstance(x, int) or x < 0 for x in values):
                raise ValueError(f"line {no}: invalid {key}")
        edges = obj.get("relation_edges", [])
        keys = [(e.get("dir"), e.get("kind"), e.get("to")) for e in edges]
        if keys != sorted(keys) or any(set(e) != {"to", "kind", "dir"} for e in edges):
            raise ValueError(f"line {no}: invalid relation_edges")
        canonical.extend(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        rows += 1
    return rows, hashlib.sha256(canonical).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sidecar")
    a = ap.parse_args()
    rows, digest = validate(a.sidecar)
    print(f"valid rows={rows} sha256={digest}")


if __name__ == "__main__":
    main()
