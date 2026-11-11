import json, random, time, urllib.request, math
INDEX = "data/memcore/ann_index.jsonl"
N = 20
K = 10
NOISE = 0.05
rows = [json.loads(l) for l in open(INDEX, encoding="utf-8") if l.strip()]
sample = random.sample(rows, N)
hits10 = 0
lats = []
for r in sample:
    vec = [x + random.gauss(0, NOISE) for x in r["vec"]]
    body = {"vec": vec, "topk": K, "scope": ""}
    req = urllib.request.Request("http://127.0.0.1:8096/v1/state/search",
                                data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.load(resp)
    lats.append((time.perf_counter()-t0)*1000)
    sids = [h.get("sid") for h in out.get("hits", [])]
    if r["sid"] in sids:
        hits10 += 1
print(f"N={N} K={K} noise_std={NOISE}")
print(f"Recall@{K}={hits10/N:.3f}")
print(f"latency avg={sum(lats)/len(lats):.1f}ms min={min(lats):.1f} max={max(lats):.1f}")