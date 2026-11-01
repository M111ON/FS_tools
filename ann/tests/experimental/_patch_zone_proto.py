p = r'src\ann_search.c'
s = open(p, encoding='utf-8').read()
i = s.index('static void lz_tags_free(void)')
# find start of that function's preceding comment or just insert before it
proto = '''static void lz_zone_rows_free(void);
static void lz_zone_rows_build(int nent);
static int  lz_zone_rows(int zone, const uint32_t **rows);

'''
s = s[:i] + proto + s[i:]
open(p, 'w', encoding='utf-8', newline='').write(s)
print('prototypes added')
