p = r'bench_zone_ab_live.py'
s = open(p, encoding='utf-8').read()
s = s.replace('REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))',
              'REPO = os.path.dirname(os.path.abspath(__file__))')
open(p, 'w', encoding='utf-8', newline='').write(s)
print('repo fixed')
