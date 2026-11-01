p = r'src\ann_search.c'
s = open(p, encoding='utf-8').read()

old = '''static void lz_zone_rows_free(void);
static void lz_zone_rows_build(int nent);'''
new = '''static void lz_zone_rows_free(void);
static void lz_zone_rows_build(int nent);
/* ANN_ZONE_ROWS=0 restores the two-pass filter, so the prebuilt index can be
 * A/B'd against the code it replaced on the SAME binary (a stored old exe is
 * not portable across checkouts). */
static int lz_zone_rows_on(void);'''
assert old in s
s = s.replace(old, new, 1)

old2 = '''static void lz_zone_rows_free(void) {'''
new2 = '''static int lz_zone_rows_on(void) {
    const char *e = getenv("ANN_ZONE_ROWS");
    return (e && e[0] == '0') ? 0 : 1;
}

static void lz_zone_rows_free(void) {'''
assert old2 in s
s = s.replace(old2, new2, 1)

old3 = '''            } else if (r == 4 && lz_zone_rows(scope_zone, NULL) > 0) {'''
new3 = '''            } else if (r == 4 && lz_zone_rows_on() && lz_zone_rows(scope_zone, NULL) > 0) {'''
assert old3 in s
s = s.replace(old3, new3, 1)
open(p, 'w', encoding='utf-8', newline='').write(s)
print('flag added')
