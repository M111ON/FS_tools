p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
s = s.replace('raise SystemExit(f"400 on', 'raise RuntimeError(f"refused:')
old = '''    def run(label, make_body):
        lat, scored = [], []
        for v in qs:
            ms, d = post(make_body(v))
            lat.append(ms)
            scored.append(d.get("route", {}).get("items_scored", -1))'''
new = '''    def run(label, make_body):
        lat, scored = [], []
        for v in qs:
            try:
                ms, d = post(make_body(v))
            except RuntimeError as e:
                print(f"{label:<26} REFUSED  {e}")
                return None
            lat.append(ms)
            scored.append(d.get("route", {}).get("items_scored", -1))'''
assert old in s
s = s.replace(old, new, 1)
open(p, 'w', encoding='utf-8', newline='').write(s)
print('ok')
