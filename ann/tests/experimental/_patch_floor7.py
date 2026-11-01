p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
s = s.replace('''    with open(r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl", "r", encoding="utf-8") as f:
        r = json.loads(f.readline())''',
              '''    idx = os.environ.get("ANN_BENCH_INDEX", r"I:\\tools\\ann\\data\\memcore\\ann_index.jsonl")
    with open(idx, "r", encoding="utf-8") as f:
        r = json.loads(f.readline())''')
open(p, 'w', encoding='utf-8', newline='').write(s)
import py_compile; py_compile.compile(p, doraise=True)
print("idx fixed")
