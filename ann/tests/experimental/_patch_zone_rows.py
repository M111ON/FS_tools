p = r'src\ann_search.c'
s = open(p, encoding='utf-8').read()

# 1) zone-rows index, built once per tag load
anchor = '''/* reload only when the sidecar or the index actually moved */'''
zone_block = r'''/* ── ZONE ROWS (2026-10-26, card t4fbea5cf slice 1) ─────────────────────
 * The zone every row belongs to is already in the tags sidecar (frozen ids,
 * never renumbered), so a zone scope does not need to re-scan the index to find
 * its members: one pass at tag load builds zone -> ascending row indices, and
 * every later zone query jumps straight to them. Rows are in the SAME
 * ascending order the filter produced, so the walk list is byte-identical —
 * that is the gate (walk_hash), because a set-equal walk can still be
 * reordered and hide behind equal top-k.
 *
 * Size: n x 4 B (255 KB live). It is a pure function of the sidecar, so it is
 * rebuilt with the tags and never persisted. */
static uint32_t *g_zr_rows = NULL;
static int *g_zr_off = NULL;
static int *g_zr_cnt = NULL;
static int g_zr_zone_n = 0;
static int g_zr_total = 0;

static void lz_zone_rows_free(void) {
    free(g_zr_rows); g_zr_rows = NULL;
    free(g_zr_off); g_zr_off = NULL;
    free(g_zr_cnt); g_zr_cnt = NULL;
    g_zr_zone_n = 0; g_zr_total = 0;
}

static void lz_zone_rows_build(int nent) {
    lz_zone_rows_free();
    if (!g_ent_zone || nent <= 0) return;
    int nz = 0;
    for (int i = 0; i < nent; i++) {
        int z = g_ent_zone[i];
        if (z >= 0 && z < 4096 && z + 1 > nz) nz = z + 1;
    }
    if (nz <= 0) return;
    int *cnt = (int *)calloc((size_t)nz, sizeof(int));
    int *off = (int *)calloc((size_t)(nz + 1), sizeof(int));
    if (!cnt || !off) { free(cnt); free(off); return; }
    for (int i = 0; i < nent; i++) {
        int z = g_ent_zone[i];
        if (z >= 0 && z < nz) cnt[z]++;
    }
    int total = 0;
    for (int z = 0; z < nz; z++) { off[z] = total; total += cnt[z]; }
    off[nz] = total;
    uint32_t *rows = total > 0 ? (uint32_t *)malloc((size_t)total * sizeof(uint32_t)) : NULL;
    if (total > 0 && !rows) { free(cnt); free(off); return; }
    int *fill = (int *)calloc((size_t)nz, sizeof(int));
    if (total > 0 && !fill) { free(rows); free(cnt); free(off); return; }
    for (int i = 0; i < nent; i++) {
        int z = g_ent_zone[i];
        if (z >= 0 && z < nz) rows[off[z] + fill[z]++] = (uint32_t)i;
    }
    free(fill); free(cnt);
    g_zr_rows = rows; g_zr_off = off; g_zr_cnt = (int *)malloc((size_t)nz * sizeof(int));
    if (!g_zr_cnt) { free(rows); free(off); g_zr_rows = NULL; g_zr_off = NULL; return; }
    for (int z = 0; z < nz; z++) g_zr_cnt[z] = off[z + 1] - off[z];
    g_zr_zone_n = nz; g_zr_total = total;
}

/* zone -> its rows in ascending order; returns the count (0 = unknown zone) */
static int lz_zone_rows(int zone, const uint32_t **rows) {
    if (rows) *rows = NULL;
    if (!g_zr_rows || !g_zr_cnt || zone < 0 || zone >= g_zr_zone_n) return 0;
    if (g_zr_cnt[zone] <= 0) return 0;
    if (rows) *rows = g_zr_rows + g_zr_off[zone];
    return g_zr_cnt[zone];
}

'''
assert anchor in s
s = s.replace(anchor, zone_block + anchor, 1)

# 2) build it wherever the tags are loaded/freed
s = s.replace('''    free(g_ent_zone); g_ent_zone = NULL;''',
              '''    free(g_ent_zone); g_ent_zone = NULL;
    lz_zone_rows_free();''', 1)

old_tail = '''    fclose(f);
    g_tag_unlabeled = 0;
    for (int i = 0; i < nent; i++) if (g_ent_tag[i] < 0) g_tag_unlabeled++;
}'''
new_tail = '''    fclose(f);
    g_tag_unlabeled = 0;
    for (int i = 0; i < nent; i++) if (g_ent_tag[i] < 0) g_tag_unlabeled++;
    lz_zone_rows_build(nent);
}'''
assert old_tail in s
s = s.replace(old_tail, new_tail, 1)

# 3) zone scope: jump to the prebuilt rows instead of two full passes
old_scope = '''            } else if (r > 0) {
                int cnt = 0;
                for (int i = 0; i < nent; i++)'''
new_scope = '''            } else if (r == 4 && lz_zone_rows(scope_zone, NULL) > 0) {
                /* ZONE ROWS: the members are already indexed, in the same
                 * ascending order the filter produced, so the walk list and
                 * the walk_hash are unchanged — only the two full passes are
                 * gone. */
                const uint32_t *zr = NULL;
                int zcnt = lz_zone_rows(scope_zone, &zr);
                uint32_t *arr = (uint32_t *)malloc((size_t)(zcnt ? zcnt : 1) * sizeof(uint32_t));
                if (arr && zcnt > 0) {
                    memcpy(arr, zr, (size_t)zcnt * sizeof(uint32_t));
                    ord_own = arr; ord = arr; scope_n = zcnt;
                    budget = zcnt;
                    snprintf(rm, sizeof(rm), "%s", "scope+zone");
                } else {
                    free(arr);
                    scope_bad = 1;
                }
            } else if (r > 0) {
                int cnt = 0;
                for (int i = 0; i < nent; i++)'''
assert old_scope in s
s = s.replace(old_scope, new_scope, 1)

# 4) /health receipt
s = s.replace('"\\"roster\\":{\\"on\\":%d,\\"doors\\":%d,\\"rows\\":%d,\\"bytes\\":%ld,"',
              '"\\"roster\\":{\\"on\\":%d,\\"doors\\":%d,\\"rows\\":%d,\\"bytes\\":%ld,"', 1)
open(p, 'w', encoding='utf-8', newline='').write(s)
print("zone rows patched")
