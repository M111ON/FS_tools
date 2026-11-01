p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
old = '''def post(body, to=120):
    r = urllib.request.Request(H + "/v1/state/search", json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(r, timeout=to).read())
    return (time.perf_counter() - t0) * 1000.0, d'''
new = '''def post(body, to=120):
    r = urllib.request.Request(H + "/v1/state/search", json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        d = json.loads(urllib.request.urlopen(r, timeout=to).read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:200]
        raise SystemExit(f"400 on { {k: (v if k != 'vec' else '<vec>') for k, v in body.items()} }: {msg}")
    return (time.perf_counter() - t0) * 1000.0, d'''
assert old in s
s = s.replace(old, new, 1)
s = s.replace("import urllib.request", "import urllib.request\nimport urllib.error")
open(p, 'w', encoding='utf-8', newline='').write(s)
print('ok')
