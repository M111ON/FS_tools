p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
old = '''    rng = random.Random(a.seed)
    qs = []
    import numpy as np

    # learn dim from one search echo, then make unit-ish random queries
    t, d = post({"vec": [0.0] * DIM, "topk": 1})
    dim = d.get("dim") or DIM'''
new = '''    rng = random.Random(a.seed)
    qs = []
    # dim from the real index (a zero vec is rejected by the sanitizer, so it
    # cannot be used to probe the echo)
    dim = DIM
    with open(r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl", "r", encoding="utf-8") as f:
        r = json.loads(f.readline())
        dim = int(r.get("dim") or len(r.get("vec") or []) or DIM)'''
assert old in s
s = s.replace(old, new, 1)
s = s.replace('print(f"dim probe: {len(zones)} zones', 'print(f"dim {dim}: {len(zones)} zones')
# move the print after dim is known
s = s.replace('''    print(f"dim {dim}: {len(zones)} zones, rows {zones[0]['rows']} .. {zones[-1]['rows']}")''', '')
s = s.replace('''    for _ in range(a.queries):''', '''    print(f"dim {dim}: {len(zones)} zones, rows {zones[0]['rows']} .. {zones[-1]['rows']}")
    for _ in range(a.queries):''')
open(p, 'w', encoding='utf-8', newline='').write(s)
print('ok')
