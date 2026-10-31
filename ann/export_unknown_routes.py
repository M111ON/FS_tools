"""Export a safe route sidecar from the current index.

This intentionally emits only ``unknown`` records. It is a bridge for schema
rollout, not a semantic classifier; kind/term_hint/content are never promoted
to route meaning here.
"""
import argparse
import hashlib
import json
from pathlib import Path


def export(index, output):
    rows = []
    for raw in Path(index).read_bytes().splitlines():
        obj = json.loads(raw)
        rows.append({"v": 1, "sid": obj["sid"], "intent": "unknown",
                     "value_routes": [], "shape_routes": [],
                     "relation_edges": [], "junction_routes": [],
                     "producer_seq": len(rows)})
    data = b"".join(json.dumps(x, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                    for x in rows)
    out = Path(output)
    out.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    out.with_suffix(out.suffix + ".manifest.json").write_text(
        json.dumps({"v": 1, "rows": len(rows), "producer_epoch": "unknown-only-v1",
                    "sha256": digest}, indent=2) + "\n", encoding="utf-8")
    return len(rows), digest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("output")
    a = ap.parse_args()
    rows, digest = export(a.index, a.output)
    print(f"exported unknown rows={rows} sha256={digest}")


if __name__ == "__main__":
    main()
