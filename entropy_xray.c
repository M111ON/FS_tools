#include "entropy_xray.h"

#include <string.h>

static uint64_t xr_splitmix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111B9ULL;
    return x ^ (x >> 31);
}

static uint32_t xr_popcount64(uint64_t v) {
    return (uint32_t)__builtin_popcountll(v);
}

void xray_scan_cell(uint32_t cell_id, XrayResult *out) {
    if (!out) return;
    XrayResult r;
    memset(&r, 0, sizeof r);
    r.cell_id = cell_id;

    uint64_t state = (uint64_t)cell_id * 0x9E3779B97F4A7C15ULL + 0xC2B2AE3D27D4EB4FULL;
    uint32_t total = 0, set = 0, var = 0;
    for (uint32_t w = 0; w < XRAY_CELL_WORDS; w++) {
        state = xr_splitmix64(state);
        uint64_t word = state;
        uint32_t pc = xr_popcount64(word);
        total += (uint32_t)XRAY_WORD_BITS;
        set += pc;
        var += pc * ((uint32_t)XRAY_WORD_BITS - pc);
    }

    r.total_bits = total;
    r.set_bits = set;
    r.density = (total > 0) ? (set * 1000u) / total : 0u;
    r.variance = var;
    r.bit_variance = var;
    r.density_score = r.density;
    r.entropy = var;
    r.score = var;
    r.xray_score = var;

    r.flags = 0;
    if (var < XRAY_ENTROPY_LOW)  r.flags |= XRAY_FLAG_PRUNE_LOW;
    if (var > XRAY_ENTROPY_HIGH) r.flags |= XRAY_FLAG_PRUNE_HIGH;
    /* density-based prune (effective on the observed corpus) */
    if (r.density < XRAY_DENSITY_LOW || r.density > XRAY_DENSITY_HIGH) {
        r.flags |= (r.density < XRAY_DENSITY_LOW) ? XRAY_FLAG_PRUNE_LOW : XRAY_FLAG_PRUNE_HIGH;
    }
    r.pruned = (r.flags & XRAY_FLAG_PRUNED) ? 1u : 0u;

    *out = r;
}

uint32_t xray_entropy_score(uint32_t cell_id) {
    XrayResult r;
    xray_scan_cell(cell_id, &r);
    return r.variance;
}

void entropy_rank_candidates(Query *q, CandidateList *candidates) {
    if (!candidates) return;

    uint32_t center = q ? q->center_cell : 0u;
    uint32_t n = candidates->count;
    if (n > XRAY_MAX_CANDIDATES) n = XRAY_MAX_CANDIDATES;

    /* Score each candidate in place (stack scratch only). */
    for (uint32_t i = 0; i < n; i++) {
        Candidate *c = &candidates->items[i];
        XrayResult xr;
        xray_scan_cell(c->cell_id, &xr);

        /* geometric distance in cell space (integer, no float) */
        uint32_t dist = (c->cell_id > center)
                        ? (c->cell_id - center)
                        : (center - c->cell_id);
        /* fold distance into a bounded neighborhood */
        if (dist > 1000000u) dist = 1000000u;

        uint32_t geo = 1000000u / (1u + dist);

        /* density balance: how close bit density is to target (500) */
        uint32_t d = xr.density;
        uint32_t diff = (d > XRAY_DENSITY_TARGET) ? (d - XRAY_DENSITY_TARGET)
                                                  : (XRAY_DENSITY_TARGET - d);
        uint32_t balance = (diff >= XRAY_DENSITY_TARGET) ? 0u
                                                         : (XRAY_DENSITY_TARGET - diff);

        c->score = balance + geo;
        c->raw_score = c->score;
        c->pruned = xr.pruned;

        if (xr.flags & XRAY_FLAG_PRUNED) {
            /* pruned candidates sink: zero score keeps them at the tail */
            c->score = 0u;
        }
    }

    /* Stable-ish insertion sort by score desc, pruned last. No allocation. */
    for (uint32_t i = 1; i < n; i++) {
        Candidate key = candidates->items[i];
        uint32_t j = i;
        while (j > 0) {
            Candidate *prev = &candidates->items[j - 1];
            int move = 0;
            if (key.pruned && !prev->pruned) move = 1;
            else if (key.pruned == prev->pruned && key.score > prev->score) move = 1;
            if (!move) break;
            candidates->items[j] = candidates->items[j - 1];
            j--;
        }
        candidates->items[j] = key;
    }

    candidates->count = n;
}
