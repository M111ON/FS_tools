p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
s = s.replace('H = "http://127.0.0.1:8096"', 'H = "http://127.0.0.1:8096"  # overridden by --url')
s = s.replace('''    ap.add_argument("--seed", type=int, default=11)''',
              '''    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--url", default=H)''')
s = s.replace('''    a = ap.parse_args()
''', '''    a = ap.parse_args()
    global H
    H = a.url.rstrip("/")
''')
# index path for query vectors must follow --url's data dir; allow override
s = s.replace('''    with open(r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl", "r", encoding="utf-8") as f:
        r = json.loads(f.readline()); dim = int(r.get("dim") or len(r["vec"]))''',
              '''    idx = os.environ.get("ANN_BENCH_INDEX", r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl")
    with open(idx, "r", encoding="utf-8") as f:
        r = json.loads(f.readline()); dim = int(r.get("dim") or len(r["vec"]))''')
s = s.replace('''    with open(r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl", "r", encoding="utf-8") as f:
        for line in f:''',
              '''    with open(idx, "r", encoding="utf-8") as f:
        for line in f:''')
s = s.replace('import statistics', 'import os\nimport statistics')
open(p, 'w', encoding='utf-8', newline='').write(s)
print('bench url patched')
