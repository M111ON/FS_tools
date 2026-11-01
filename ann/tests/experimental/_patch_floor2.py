p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
old = '''    print(f"dim {dim}: {len(zones)} zones, rows {zones[0]['rows']} .. {zones[-1]['rows']}")
    for _ in range(a.queries):
        v = [rng.gauss(0, 1) for _ in range(dim)]
        n = sum(x * x for x in v) ** 0.5
        qs.append([x / n for x in v])'''
new = '''    print(f"dim {dim}: {len(zones)} zones, rows {zones[0]['rows']} .. {zones[-1]['rows']}")
    # REAL query distribution: sample rows from the index and use their own
    # vectors. Random gaussians do not land anywhere near the trained route
    # (measured: they trip the full-scan fallback, 63,853 rows, 62 ms), so a
    # gaussian probe measures the fallback, not the routed path.
    rows = []
    with open(r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if rng.random() < 0.02:
                r = json.loads(line)
                v = r.get("vec") or []
                if len(v) == dim:
                    rows.append([float(x) for x in v])
            if len(rows) >= a.queries * 4:
                break
    rng.shuffle(rows)
    qs = rows[:a.queries]
    print(f"queries: {len(qs)} real row vectors (self-included, latency only)")'''
assert old in s
s = s.replace(old, new, 1)
s = s.replace('run("global topk=50", lambda v: {"vec": v, "topk": 50})',
              'run("global topk=32 (cap)", lambda v: {"vec": v, "topk": 32})')
open(p, 'w', encoding='utf-8', newline='').write(s)
print('ok')
