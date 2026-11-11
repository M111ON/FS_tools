import json, glob, statistics

jev = [json.loads(l) for l in open('data/jev_audit.jsonl', encoding='utf-8') if l.strip()]
xr = [json.loads(l) for l in open('data/xray_audit.jsonl', encoding='utf-8') if l.strip()]

print("=== JEV AUDIT ===")
print("queries:", len(jev))
bests = [r.get("best_jev") or 0 for r in jev]
print("avg best_jev:", round(sum(bests)/len(bests),3) if bests else 0)
print("best>=0.5:", sum(1 for b in bests if b >= 0.5), "/", len(bests))
# per-candidate jev distribution
alljev = [c.get("jev") or 0 for r in jev for c in (r.get("results") or [])]
print("candidates:", len(alljev), "avg jev:", round(sum(alljev)/len(alljev),3) if alljev else 0)
print("max jev per query avg:", round(sum(max((c.get('jev') or 0) for c in (r.get('results') or [{}])) for r in jev)/len(jev),3) if jev else 0)

print("\n=== X-RAY AUDIT ===")
print("queries:", len(xr))
vars_ = [r.get("avg_variance") or 0 for r in xr]
print("avg variance:", round(sum(vars_)/len(vars_),1) if vars_ else 0)
pruned = sum(r.get("pruned_count") or 0 for r in xr)
cands = sum(r.get("k") or 0 for r in xr)
print("candidates:", cands, "pruned:", pruned, "prune rate:", round(pruned/cands,3) if cands else 0)
# flags distribution
flags = {}
for r in xr:
    for c in r.get("candidates") or []:
        f = c.get("xray",{}).get("flags",0)
        flags[f] = flags.get(f,0)+1
print("flag counts:", flags)

print("\n=== LATENCY (query log) ===")
lats = []
for path in glob.glob('data/query_log/ann_query-*.jsonl'):
    for line in open(path, encoding='utf-8'):
        line=line.strip()
        if not line: continue
        r=json.loads(line)
        if isinstance(r.get("latency_us"), (int,float)):
            lats.append(r["latency_us"]/1000.0)
if lats:
    lats_sorted = sorted(lats)
    print("n:", len(lats))
    print("avg:", round(sum(lats)/len(lats),1), "ms")
    print("p50:", round(lats_sorted[len(lats_sorted)//2],1), "ms")
    print("p95:", round(lats_sorted[int(len(lats_sorted)*0.95)],1), "ms")
    print("max:", round(max(lats),1), "ms")