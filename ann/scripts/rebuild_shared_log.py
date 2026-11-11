import json, glob
out = "data/query_log/all_queries.jsonl"
rows = []
for path in sorted(glob.glob("data/query_log/ann_query-*.jsonl")):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        rows.append({
            "ts": r.get("ts_ms"),
            "latency_ms": (r.get("latency_us") or 0) / 1000.0,
            "hits": len(r.get("top") or []),
            "kinds": ["*"],
            "device": "gpu",
            "error": None,
        })
with open(out, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
print("wrote", len(rows), "rows")
