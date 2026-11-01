p = r'bench_zone_floor.py'
s = open(p, encoding='utf-8').read()
s = s.replace('    ap.add_argument("--url", default=H)',
              '    ap.add_argument("--url", default=None)')
s = s.replace('''    a = ap.parse_args()
    global H
    H = a.url.rstrip("/")''',
              '''    a = ap.parse_args()
    if a.url:
        globals()["H"] = a.url.rstrip("/")''')
open(p, 'w', encoding='utf-8', newline='').write(s)
import py_compile; py_compile.compile(p, doraise=True)
print("bench syntax ok")
