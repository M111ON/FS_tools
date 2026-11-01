p = r'bench_zone_ab.py'
s = open(p, encoding='utf-8').read()
s = s.replace('WORK = os.path.join(REPO, "build", "_zone_ab")',
              'WORK = os.path.join(os.environ.get("TEMP", REPO), "ann_zone_ab")')
open(p, 'w', encoding='utf-8', newline='').write(s)
print('work dir moved out of build/')
