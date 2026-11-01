import json, random, urllib.request, urllib.error
dim = 0
with open(r"I:\tools\ann\data\memcore\ann_index.jsonl", "r", encoding="utf-8") as f:
    r = json.loads(f.readline()); dim = int(r.get("dim") or len(r["vec"]))
print("dim", dim, "file", r.get("file"))
rng = random.Random(1)
v = [rng.gauss(0, 1) for _ in range(dim)]
n = sum(x*x for x in v) ** 0.5
v = [x/n for x in v]
for body in ({"vec": v, "topk": 5},
             {"vec": v, "topk": 5, "scope": "zone:0"}):
    req = urllib.request.Request("http://127.0.0.1:8096/v1/state/search",
                                 json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        print("OK", json.dumps({k: d[k] for k in d if k != "hits"})[:220])
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:300])
