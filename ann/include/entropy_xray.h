/* entropy_xray.h — Transient & Stateless "Entropy X-ray" ranking layer.
 *
 * X-ray scans a geometric cell in RAM only: no state is saved, no index is
 * rewritten, no log is appended. All entropy metrics are integer (bitwise).
 * Floating point is reserved for the final exact rerank (not performed here).
 */
#ifndef ENTROPY_XRAY_H
#define ENTROPY_XRAY_H

#include <stdint.h>

#define XRAY_CELL_WORDS      8u     /* words scanned per cell (stack/static) */
#define XRAY_WORD_BITS      64u
#define XRAY_MAX_CANDIDATES 4096u

#define XRAY_FLAG_PRUNE_LOW   0x01u /* entropy too low  (collapsed data)   */
#define XRAY_FLAG_PRUNE_HIGH  0x02u /* entropy too high (noise)            */
#define XRAY_FLAG_PRUNED      0x03u

/* entropy (variance) thresholds on the 0..(XRAY_CELL_WORDS*1024) scale */
#define XRAY_ENTROPY_LOW   0u
#define XRAY_ENTROPY_HIGH  8192u
#define XRAY_ENTROPY_TARGET 4096u

/* density (set_bits*1000/total_bits) prune band: cells whose bit density is
 * too collapsed or too noisy for the observed corpus are pruned. */
#define XRAY_DENSITY_LOW   470u
#define XRAY_DENSITY_HIGH  530u
#define XRAY_DENSITY_TARGET 500u

typedef struct {
    uint32_t cell_id;
    uint32_t total_bits;
    uint32_t set_bits;
    uint32_t density;        /* set_bits * 1000 / total_bits  (0..1000)   */
    uint32_t variance;       /* sum popcount*(W-popcount)                  */
    uint32_t bit_variance;   /* alias of variance                          */
    uint32_t density_score;  /* alias of density                           */
    uint32_t entropy;        /* alias of variance                          */
    uint32_t score;          /* x-ray score (alias)                        */
    uint32_t xray_score;     /* alias of variance                          */
    uint32_t flags;          /* XRAY_FLAG_*                                */
    uint32_t pruned;         /* 1 if pruned                                */
} XrayResult;

typedef struct {
    uint32_t center_cell;
    uint32_t query_hash;
    uint32_t radius;
    uint32_t k;
} Query;

typedef struct {
    uint32_t id;
    uint32_t cell_id;
    uint32_t score;
    uint32_t raw_score;
    uint8_t  pruned;
} Candidate;

typedef Candidate XrayCandidate;

typedef struct {
    Candidate items[XRAY_MAX_CANDIDATES];
    uint32_t count;
} CandidateList;

/* Transient scan: fills *out from cell_id only. No allocation, no I/O. */
void xray_scan_cell(uint32_t cell_id, XrayResult *out);

/* Rank candidates in place using X-ray scores. No allocation, no I/O. */
void entropy_rank_candidates(Query *q, CandidateList *candidates);

/* Convenience: integer entropy score for a cell. */
uint32_t xray_entropy_score(uint32_t cell_id);

#endif /* ENTROPY_XRAY_H */
