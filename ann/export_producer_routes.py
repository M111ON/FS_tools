"""Export producer-owned route metadata from a separate SQLite database.

No inference is performed. Missing metadata is an explicit safe-baseline row.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def export(index, metadata_db, output):
    index_rows = [json.loads(x) for x in Path(index).read_bytes().splitlines()]
    db = sqlite3.connect("file:" + str(metadata_db) + "?mode=ro", uri=True)
    try:
        cols = {r[1] for r in db.execute("pragma table_info(route_metadata)")}
        if not {"sid", "intent", "value_routes_json", "shape_routes_json",
                "junction_routes_json", "producer_seq"} <= cols:
            raise ValueError("route_metadata schema incomplete")
        meta = {r[0]: r for r in db.execute(
            "select sid,intent,value_routes_json,shape_routes_json,junction_routes_json,producer_seq "
            "from route_metadata")}
        edges = {}
        for r in db.execute("select source_sid,target_sid,kind,direction from relation_edges"):
            edges.setdefault(r[0], []).append({"to": r[1], "kind": r[2], "dir": r[3]})
    finally:
        db.close()
    rows = []
    for i, item in enumerate(index_rows):
        sid = item["sid"]
        r = meta.get(sid)
        if r is None:
            row = {"v": 1, "sid": sid, "intent": "unknown", "value_routes": [],
                   "shape_routes": [], "relation_edges": [], "junction_routes": [],
                   "producer_seq": i}
        else:
            row = {"v": 1, "sid": sid, "intent": r[1],
                   "value_routes": sorted(set(json.loads(r[2]))),
                   "shape_routes": sorted(set(json.loads(r[3]))),
                   "relation_edges": sorted(edges.get(sid, []),
                                             key=lambda x: (x["dir"], x["kind"], x["to"])),
                   "junction_routes": sorted(set(json.loads(r[4]))),
                   "producer_seq": r[5]}
        rows.append(row)
    data = b"".join(json.dumps(x, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                    for x in rows)
    out = Path(output)
    out.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    out.with_suffix(out.suffix + ".manifest.json").write_text(json.dumps(
        {"v": 1, "rows": len(rows), "producer_epoch": "sqlite-routes-v1", "sha256": digest},
        indent=2) + "\n", encoding="utf-8")
    return len(rows), digest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("metadata_db")
    ap.add_argument("output")
    a = ap.parse_args()
    n, digest = export(a.index, a.metadata_db, a.output)
    print(f"exported rows={n} sha256={digest}")


if __name__ == "__main__":
    main()
