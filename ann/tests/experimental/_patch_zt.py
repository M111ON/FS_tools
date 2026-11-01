p = r'tests\test_zone_rows.py'
s = open(p, encoding='utf-8').read()
s = s.replace('''        for i in range(N):
            z = i % 5''', '''        for i in range(N):
            z = i % 5 if i >= 3 else 9   # zone 9 = 3 rows, for the narrow gate''')
s = s.replace('''    small = [(picks[0], "zone:1", 400)]''',
              '''    small = [(picks[0], "zone:9", 5)]   # 3 rows < topk 5''')
open(p, 'w', encoding='utf-8', newline='').write(s)

p2 = r'run_tests.py'
s2 = open(p2, encoding='utf-8').read()
s2 = s2.replace('"tests/test_roster_cold.py",',
                '"tests/test_roster_cold.py", "tests/test_zone_rows.py",')
open(p2, 'w', encoding='utf-8', newline='').write(s2)
print('ok')
