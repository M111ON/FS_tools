/*
 * ann_search.c — standalone ANN semantic-search server (fork of the DWGLS
 * anchor-route search core).
 *
 * WHAT THIS IS
 *   A minimal HTTP server that answers POST /v1/state/search over a
 *   build/kv_index.jsonl vector index, using the anchor-bucket route +
 *   geo_wang_latch tombstone from DWGLS. It is the SEARCH half of
 *   tools/gguf_lazy_serve.c, extracted whole and cut free of every GGUF /
 *   llama / model-load dependency. No llama.h, no ggml, no backend DLLs, no
 *   .gguf file: the embedder is an EXTERNAL sidecar reached over HTTP.
 *
 * FORKED FROM
 *   repo:   I:/DWGLS-native-fs  (origin https://github.com/M111ON/DWGLS.git)
 *   commit: 8c85f9f  (docs: handoff for next session — ANN warm path pickup)
 *   source: tools/gguf_lazy_serve.c  L1-216 (helpers), L240-586 (search core),
 *           L1907-2021 (/v1/state/search handler)
 *   vendored headers (exact copies): core/anchor_route.h, core/geo_wang_latch.h,
 *           core/geo_id_route_log.h, core/rdh_capture.h, core/rdh_addr.h
 *   See FORK.md for the exact cut and how to re-sync.
 *
 * THE ONE BEHAVIOUR CHANGE
 *   topk is bounded at ANN_MAXK (32) instead of 8. The original clamps to 8
 *   because gguf_lazy_serve's generate path reuses 8-row stack arrays; a
 *   standalone search server has no such caller, so the cap is raised. Set
 *   ANN_MAXK back to 8 to be byte-for-byte the original contract.
 *
 * BUILD:  make        (or: gcc -O2 -std=c11 -Iinclude -o ann_search src/ann_search.c -lws2_32 -lm)
 * RUN:    ./ann_search.exe            (defaults: --serve 8096)
 *   env LZ_EMBED_HOST / LZ_EMBED_PORT  embed sidecar (default 127.0.0.1:18095)
 *   env ANN_INDEX_PATH / ANN_ANCH_PATH / ANN_ORDER_PATH / ANN_ROUTE_LOG
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <psapi.h>
#include <sys/stat.h>
#include <time.h>
#pragma comment(lib, "ws2_32.lib")
typedef SOCKET sock_t;
#define SOCK_INVALID INVALID_SOCKET
#define sock_close(s) closesocket(s)

#include "anchor_route.h"
#include "geo_wang_latch.h"

/* ── paths (overridable so the fork is not hard-wired to one checkout) ── */
static const char *PATH_INDEX;   /* kv_index.jsonl  */
static const char *PATH_ANCH;    /* kv_anchors.bin  */
static const char *PATH_ORDER;   /* kv_index.order  */
static const char *PATH_LOG;     /* P3 route log    */
static void paths_init(void) {
    const char *e;
    e = getenv("ANN_INDEX_PATH"); PATH_INDEX = (e && e[0]) ? e : "data/kv_index.jsonl";
    e = getenv("ANN_ANCH_PATH");  PATH_ANCH  = (e && e[0]) ? e : "data/kv_anchors.bin";
    e = getenv("ANN_ORDER_PATH"); PATH_ORDER = (e && e[0]) ? e : "data/kv_index.order";
    e = getenv("ANN_ROUTE_LOG");  PATH_LOG   = (e && e[0]) ? e : "data/lz_route.log";
}

/* ── minimal HTTP helpers (from gguf_lazy_serve.c L44-116) ── */
static int lz_http_recv(sock_t fd, char *buf, int cap) {
    int total = 0;
    int hdr_end = -1, want = 0;
    while (total < cap - 1) {
        int n = recv(fd, buf + total, cap - total - 1, 0);
        if (n <= 0) break;
        total += n;
        buf[total] = '\0';
        char *hh = strstr(buf, "\r\n\r\n");
        if (!hh) continue;
        if (hdr_end < 0) {
            hdr_end = (int)(hh + 4 - buf);
            char *cl = strstr(buf, "Content-Length:");
            if (!cl || cl > hh) cl = strstr(buf, "content-length:");
            if (cl && cl < hh) { cl = strchr(cl, ':'); if (cl) want = atoi(cl + 1); }
            if (want < 0) want = 0;
            if (want == 0) break;
        }
        if (hdr_end >= 0 && total >= hdr_end + want) break;
    }
    return total;
}
static void lz_http_send(sock_t fd, const char *status, const char *ctype,
                         const char *body, int blen) {
    char hdr[1024];
    int hlen = snprintf(hdr, sizeof(hdr),
        "HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
        "Connection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n",
        status, ctype, blen);
    send(fd, hdr, hlen, 0);
    if (blen > 0 && body) send(fd, body, blen, 0);
}
static int lz_json_int(const char *json, const char *key, int def) {
    char pat[128], val[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return def;
    p = strchr(p + strlen(pat), ':');
    if (!p) return def;
    int i = 0;
    for (p++; *p && *p != ',' && *p != '}' && i < 31; p++) val[i++] = *p;
    val[i] = '\0';
    return atoi(val);
}
static void lz_json_escape(char *dst, size_t cap, const char *src) {
    size_t di = 0;
    for (size_t i = 0; src[i] && di + 6 < cap; i++) {
        char c = src[i];
        if (c == '"' || c == '\\') { dst[di++] = '\\'; dst[di++] = c; }
        else if (c == '\n') { dst[di++] = '\\'; dst[di++] = 'n'; }
        else if (c == '\r') { dst[di++] = '\\'; dst[di++] = 'r'; }
        else if (c == '\t') { dst[di++] = '\\'; dst[di++] = 't'; }
        else dst[di++] = c;
    }
    dst[di] = '\0';
}
/* decode a JSON string body (bytes between the quotes) into dst: handles
 * \uXXXX (incl. surrogate pairs) and \n \t \r \b \f \" \\ \/ . src points at
 * the first char AFTER the opening quote; stops at the closing quote.
 * Standard JSON clients send non-ASCII as \uXXXX, so this is what makes a
 * Python/curl/urllib body embed the same text the caller meant. */
static int lz_json_unescape(char *dst, int cap, const char *src) {
    int di = 0;
    while (*src && *src != '"' && di < cap - 1) {
        if (*src == '\\' && src[1]) {
            src++;
            char c = *src;
            if (c == 'n') { dst[di++] = '\n'; src++; }
            else if (c == 't') { dst[di++] = '\t'; src++; }
            else if (c == 'r') { dst[di++] = '\r'; src++; }
            else if (c == 'b') { dst[di++] = '\b'; src++; }
            else if (c == 'f') { dst[di++] = '\f'; src++; }
            else if (c == '"' || c == '\\' || c == '/') { dst[di++] = c; src++; }
            else if (c == 'u') {
                unsigned cp = 0; int ok = 1;
                for (int h = 0; h < 4; h++) {
                    char x = src[1 + h]; cp <<= 4;
                    if (x >= '0' && x <= '9') cp |= (unsigned)(x - '0');
                    else if (x >= 'a' && x <= 'f') cp |= (unsigned)(x - 'a' + 10);
                    else if (x >= 'A' && x <= 'F') cp |= (unsigned)(x - 'A' + 10);
                    else { ok = 0; break; }
                }
                if (!ok) { dst[di++] = c; src++; continue; }
                src += 5;   /* past \uXXXX */
                if (cp >= 0xD800 && cp <= 0xDBFF && src[0] == '\\' && src[1] == 'u') {
                    unsigned lo = 0; int ok2 = 1;
                    for (int h = 0; h < 4; h++) {
                        char x = src[2 + h]; lo <<= 4;
                        if (x >= '0' && x <= '9') lo |= (unsigned)(x - '0');
                        else if (x >= 'a' && x <= 'f') lo |= (unsigned)(x - 'a' + 10);
                        else if (x >= 'A' && x <= 'F') lo |= (unsigned)(x - 'A' + 10);
                        else { ok2 = 0; break; }
                    }
                    if (ok2 && lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000u + ((cp - 0xD800u) << 10) + (lo - 0xDC00u);
                        src += 6;
                    }
                }
                if (cp < 0x80) { if (di < cap - 1) dst[di++] = (char)cp; }
                else if (cp < 0x800) {
                    if (di < cap - 2) { dst[di++] = (char)(0xC0 | (cp >> 6)); dst[di++] = (char)(0x80 | (cp & 0x3F)); }
                } else if (cp < 0x10000) {
                    if (di < cap - 3) { dst[di++] = (char)(0xE0 | (cp >> 12)); dst[di++] = (char)(0x80 | ((cp >> 6) & 0x3F)); dst[di++] = (char)(0x80 | (cp & 0x3F)); }
                } else {
                    if (di < cap - 4) { dst[di++] = (char)(0xF0 | (cp >> 18)); dst[di++] = (char)(0x80 | ((cp >> 12) & 0x3F)); dst[di++] = (char)(0x80 | ((cp >> 6) & 0x3F)); dst[di++] = (char)(0x80 | (cp & 0x3F)); }
                }
            }
            else { dst[di++] = c; src++; }   /* unknown escape: keep the char */
        } else {
            dst[di++] = *src++;
        }
    }
    dst[di] = '\0';
    return di;
}
/* ── minimal HTTP POST (for embedding sidecar) ── */
static int lz_http_post(const char *host, int port, const char *path,
                        const char *body, char *resp, int cap) {
    sock_t fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd == SOCK_INVALID) return -1;
    struct sockaddr_in sa;
    sa.sin_family = AF_INET;
    sa.sin_port = htons((uint16_t)port);
    sa.sin_addr.s_addr = inet_addr(host);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { sock_close(fd); return -2; }
    char hdr[512];
    int hlen = snprintf(hdr, sizeof(hdr),
        "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
        "Content-Length: %d\r\nConnection: close\r\n\r\n",
        path, host, (int)strlen(body));
    send(fd, hdr, hlen, 0);
    send(fd, body, (int)strlen(body), 0);
    int total = 0, n;
    while (total < cap - 1 && (n = recv(fd, resp + total, cap - total - 1, 0)) > 0)
        total += n;
    resp[total < 0 ? 0 : total] = '\0';
    sock_close(fd);
    return total;
}
/* fetch embedding for text; returns dim or -1. Sidecar: llama-server
 * /embedding with {"content": "..."} (multilingual-e5-small, 384-dim). */
static int lz_embed(const char *text, float *vec, int maxdim) {
    const char *eh = getenv("LZ_EMBED_HOST");
    const char *ep = getenv("LZ_EMBED_PORT");
    const char *host = (eh && eh[0]) ? eh : "127.0.0.1";
    int port = (ep && atoi(ep)) ? atoi(ep) : 18095;
    char body[8192];
    char esc[8000];
    lz_json_escape(esc, sizeof(esc), text);
    snprintf(body, sizeof(body), "{\"content\":\"%s\"}", esc);
    static char resp[65536];
    int nr = lz_http_post(host, port, "/embedding", body, resp, sizeof(resp));
    if (nr <= 0) return -1;
    const char *p = strstr(resp, "\"embedding\"");
    if (!p) return -1;
    p = strchr(p, '[');
    if (!p) return -1;
    p++;
    int n = 0;
    while (*p && *p != ']' && n < maxdim) {
        while (*p == ' ' || *p == ',' || *p == '[') p++;
        if (*p == ']' || !*p) break;
        vec[n++] = (float)atof(p);
        while (*p && *p != ',' && *p != ']') p++;
    }
    return n;
}

/* ── fog latch over the anchor-bucket space (from L219-256) ──
 * The ROUTE WALKED is what shuts the latch. Buckets collapse to a wang-latch
 * cell = bucket % 144. Reload from the P3 route log on boot. */
static wl_latch_t g_fog;
static int g_fog_ready = 0;
static wl_log_t g_fog_log;
static int g_fog_log_open = 0;
static int g_fog_replayed = 0;
typedef struct { int open; int shut; int lit; int replayed; } LZFog;
static int g_warm_on = 0;       /* tombstone warm path (env ANN_TOMBSTONE_WARM) */
static void lz_fog_init(void) {
    memset(&g_fog, 0, sizeof(g_fog));
    wl_reset(&g_fog);
    const char *lp = PATH_LOG;
    long replayed = 0;
    if (lp && lp[0]) replayed = wl_replay_log_file(&g_fog, lp);
    g_fog_replayed = (int)(replayed > 0 ? replayed : 0);
    if (lp && lp[0]) {
        g_fog_log_open = (wl_log_open(&g_fog_log, lp) == 0);
    }
    g_fog_ready = 1;
    const char *we = getenv("ANN_TOMBSTONE_WARM");
    g_warm_on = (we && atoi(we)) ? 1 : 0;
}
static inline uint32_t lz_fog_id(int bucket) {
    int c = bucket % 144;
    if (c < 0) c += 144;
    return wl_id((uint32_t)c, 0u);
}

/* ── anchor-bucket routing over the index ── */
#define LZ_ANCH_MIN 16
#define LZ_ANCH_MAXK 8
#define LZ_OVERLAP_TOPB 2
#define LZ_ROUTE_HEADROOM_DEF 4
#define LZ_SEARCH_BUDGET_DEF 512
#define ANN_MAXK 32   /* standalone topk bound (original serve: 8) */
typedef struct { char sid[64]; char file[256]; int npast; int dim; int anchor; int anchor2; float *vec; double norm; int goff; int gn; } LZEntry;

/* graft bias hooks (defined after lz_mem_search; used inside it) */
static void graft_init(LZEntry *ents, int nent);
static float graft_bias(const LZEntry *e, const int *qkeys, int qn, int warm);
static void graft_query_keys(const char *q);
static int graft_candidates(uint32_t *out, int cap);   /* graph → candidate entries */
static int g_graft_on = 0;                 /* master switch (env ANN_GRAFT) */
static int g_qkeys[16], g_qn = 0;          /* query-side file keys, per request */
static int g_graft_ready = 0;              /* bias tables loaded (or off) */
static void graft_ensure_init(void);       /* loads the tables once */

/* ── tombstone × graft amplifier ──────────────────────────────────────
 * The tombstone (fog latch) is the hot/cold axis: it knows which entries sit
 * in a bucket this query just walked warm. The graft bias is the structural
 * axis: a static weight toward the query's file cluster. Composed, a WARM
 * entry — one whose bucket the tombstone already shut — may carry a STRONGER
 * structural weight, because a route we keep hot is the route we trust.
 * The boost MULTIPLIES the graft bias only (never the cosine score), so with
 * the tombstone off or the graft bias off it is a strict no-op. */
static int  g_amp_boost = 0;               /* ANN_AMP_BOOST (default 0 = off) */
static float g_amp_k = 2.0f;               /* ANN_AMP_K, warm-bucket weight multiplier */
static unsigned char *g_warm_flag = NULL;  /* [nent] 1 if entry is in a warm bucket */
static int g_warm_flag_n = 0;
static int g_warm_ord = 0;                 /* set by lz_mem_search: a warm path ran */
static int g_amp_applied = 0;              /* entries whose bias was boosted (telemetry) */
static int g_backward = 0;                 /* ANN_GRAFT_BACKWARD: expand candidates via in-edges */

static const char *lz_json_val(const char *line, const char *key) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *pp = strstr(line, pat);
    if (!pp) return NULL;
    pp = strchr(pp + strlen(pat), ':');
    if (!pp) return NULL;
    pp++;
    while (*pp == ' ') pp++;
    return pp;
}
static int lz_entry_parse(const char *line, LZEntry *e) {
    memset(e, 0, sizeof(*e));
    e->anchor = -1; e->anchor2 = -1;
    const char *pp = lz_json_val(line, "sid");
    if (pp && *pp == '"') { pp++; int ii = 0; while (*pp && *pp != '"' && ii < 63) e->sid[ii++] = *pp++; e->sid[ii] = 0; }
    pp = lz_json_val(line, "file");
    if (pp && *pp == '"') { pp += 1; int ii = 0; while (*pp && *pp != '"' && ii < 255) e->file[ii++] = *pp++; e->file[ii] = 0; }
    pp = lz_json_val(line, "npast"); if (pp) e->npast = atoi(pp);
    pp = lz_json_val(line, "anchor"); if (pp) e->anchor = atoi(pp);
    pp = lz_json_val(line, "anchor2"); if (pp) e->anchor2 = atoi(pp);
    pp = lz_json_val(line, "vec");
    if (!pp || *pp != '[') return -1;
    pp += 1;
    e->vec = (float *)malloc(1024 * sizeof(float));
    if (!e->vec) return -1;
    while (*pp == '[') pp++;
    while (*pp && *pp != ']' && e->dim < 1024) {
        while (*pp == ' ' || *pp == ',') pp++;
        if (*pp == ']' || !*pp) break;
        e->vec[e->dim++] = (float)atof(pp);
        while (*pp && *pp != ',' && *pp != ']') pp++;
    }
    double n2 = 0;
    for (int wi = 0; wi < e->dim; wi++) n2 += (double)e->vec[wi] * e->vec[wi];
    e->norm = sqrt(n2);
    return 0;
}
static int lz_index_load(LZEntry **out, int *n_out) {
    *out = NULL; *n_out = 0;
    FILE *ix = fopen(PATH_INDEX, "r");
    if (!ix) return 0;
    int cap = 0, n = 0;
    LZEntry *a = NULL;
    char line[16384];
    while (fgets(line, sizeof(line), ix)) {
        if (n == cap) {
            cap = cap ? cap * 2 : 16;
            LZEntry *na = (LZEntry *)realloc(a, (size_t)cap * sizeof(LZEntry));
            if (!na) break;
            a = na;
        }
        if (lz_entry_parse(line, &a[n]) == 0) n++;
    }
    fclose(ix);
    *out = a; *n_out = n;
    return 0;
}
static void lz_index_free(LZEntry *a, int n) {
    if (!a) return;
    for (int i = 0; i < n; i++) free(a[i].vec);
    free(a);
}
/* index cache: load-once, reload on mtime/size change (single-threaded). */
static LZEntry *g_idx_cache = NULL;
static int g_idx_n = 0;
static long g_idx_mtime = 0;
static long g_idx_size = 0;
static void lz_index_cache_invalidate(void) {
    lz_index_free(g_idx_cache, g_idx_n);
    g_idx_cache = NULL; g_idx_n = 0; g_idx_mtime = 0; g_idx_size = 0;
}
static int lz_index_cached(LZEntry **out, int *n_out) {
    struct stat st;
    if (stat(PATH_INDEX, &st) == 0) {
        long mt = (long)st.st_mtime, sz = (long)st.st_size;
        if (g_idx_cache && mt == g_idx_mtime && sz == g_idx_size) {
            *out = g_idx_cache; *n_out = g_idx_n; return 1;
        }
        lz_index_cache_invalidate();
        if (lz_index_load(&g_idx_cache, &g_idx_n) == 0 && g_idx_cache) {
            g_idx_mtime = mt; g_idx_size = sz;
            *out = g_idx_cache; *n_out = g_idx_n; return 1;
        }
    }
    *out = NULL; *n_out = 0;
    return 0;
}
/* anchors + order cache */
static float *g_anch_cache = NULL;
static int g_anch_k = 0, g_anch_dim = 0, g_anch_ntr = 0;
static uint32_t *g_ord_cache = NULL;
static int g_ord_n = 0;
static long g_anch_key[4] = {0, 0, 0, 0};
static void lz_route_cache_invalidate(void) {
    free(g_anch_cache); g_anch_cache = NULL;
    free(g_ord_cache);  g_ord_cache = NULL;
    g_anch_k = g_anch_dim = g_anch_ntr = 0; g_ord_n = 0;
    for (int i = 0; i < 4; i++) g_anch_key[i] = 0;
}
static int lz_route_cached(int nent) {
    struct stat sa, so;
    if (stat(PATH_ANCH, &sa) != 0 || stat(PATH_ORDER, &so) != 0) return 0;
    long key[4] = { (long)sa.st_mtime, (long)sa.st_size,
                    (long)so.st_mtime, (long)so.st_size };
    int fresh = g_anch_cache && g_ord_cache && g_ord_n == nent;
    for (int i = 0; i < 4 && fresh; i++) if (g_anch_key[i] != key[i]) fresh = 0;
    if (fresh) return 1;
    lz_route_cache_invalidate();
    float *C = (float *)malloc((size_t)ANCHR_MAXK * ANCHR_MAXD * sizeof(float));
    uint32_t *ord = (uint32_t *)malloc((nent ? (size_t)nent : 1) * sizeof(uint32_t));
    if (!C || !ord) { free(C); free(ord); return 0; }
    int adim = 0, antr = 0;
    int K = anch_load(PATH_ANCH, C, ANCHR_MAXK, ANCHR_MAXD, &adim, &antr);
    if (K <= 0 || antr != nent) { free(C); free(ord); return 0; }
    FILE *of = fopen(PATH_ORDER, "rb");
    if (!of) { free(C); free(ord); return 0; }
    size_t nr = fread(ord, sizeof(uint32_t), (size_t)nent, of);
    fclose(of);
    if (nr != (size_t)nent) { free(C); free(ord); return 0; }
    g_anch_cache = C; g_anch_k = K; g_anch_dim = adim; g_anch_ntr = antr;
    g_ord_cache = ord; g_ord_n = nent;
    for (int i = 0; i < 4; i++) g_anch_key[i] = key[i];
    return 1;
}

/* ── lz-mem-search core (from L435-586, topk bound -> ANN_MAXK) ──
 * Returns hit count 0..topk, or -1 when the query is unusable. */
static int lz_mem_search(const float *qv, int qd,
                         const char *sid_filter, int topk,
                         char top_sid[][64], char top_file[][256],
                         int top_np[], double top_sc[],
                         char route_mode[16], int route_bk[], int *route_nb_out,
                         int *block_reads_out, int *items_scored_out, int *nent_out,
                         int *budget_out, int *truncated_out, double *us_per_item_out,
                         LZFog *fog_out) {
    if (top_sc) for (int ti = 0; ti < ANN_MAXK; ti++) top_sc[ti] = -2;
    if (top_sid) for (int ti = 0; ti < ANN_MAXK; ti++) top_sid[ti][0] = '\0';
    if (top_file) for (int ti = 0; ti < ANN_MAXK; ti++) top_file[ti][0] = '\0';
    if (!qv || qd <= 0) return -1;
    if (topk < 1) topk = 1;
    if (topk > ANN_MAXK) topk = ANN_MAXK;
    if (!sid_filter) sid_filter = "";
    double qn = 0;
    for (int qi = 0; qi < qd; qi++) qn += (double)qv[qi] * qv[qi];
    qn = sqrt(qn);
    LZEntry *ents = NULL;
    int nent = 0;
    int use_cache = lz_index_cached(&ents, &nent);
    if (!g_graft_ready) graft_init(ents, nent);   /* load bias tables once */
    /* the warm-entry flags are per request; lz_mem_search is serialized under
     * g_lock, so a single reusable buffer is safe. */
    g_warm_ord = 0;
    g_amp_applied = 0;
    if (g_warm_flag_n < nent) {
        unsigned char *nf = (unsigned char *)realloc(g_warm_flag, (size_t)(nent ? nent : 1));
        if (nf) { g_warm_flag = nf; g_warm_flag_n = nent; }
    }
    char rm[16];
    snprintf(rm, sizeof(rm), "%s", "brute");
    int rb[ANN_MAXK];
    for (int ri = 0; ri < ANN_MAXK; ri++) rb[ri] = 0;
    int rnb = 0, breads = 1, scored = 0;
    int truncated = 0;
    int budget = LZ_SEARCH_BUDGET_DEF;
    { const char *be = getenv("LZ_SEARCH_BUDGET");
      if (be && atoi(be) > 0) budget = atoi(be);
      else if (nent > budget) budget = nent; }
    double us_per_item = 0;
    if (ents && qn > 0) {
        const uint32_t *ord = NULL;
        uint32_t *ord_own = NULL;
        const float *C = NULL;
        int routed = 0, K = 0;
        if (lz_route_cached(nent) && g_anch_dim == qd) {
            C = g_anch_cache; ord = g_ord_cache; K = g_anch_k;
            int topb;
            { const char *beke = getenv("LZ_ROUTE_BUCKETS");
              if (beke && atoi(beke) > 0) topb = atoi(beke);
              else { const char *hre = getenv("LZ_ROUTE_HEADROOM");
                     int hr = (hre && atoi(hre) >= 0) ? atoi(hre) : LZ_ROUTE_HEADROOM_DEF;
                     topb = topk + hr; } }
            if (topb > K) topb = K;
            if (topb < 1) topb = 1;
            rnb = anch_route(qv, C, K, qd, topb, rb);
            routed = 1;
            snprintf(rm, sizeof(rm), "%s", "anchor");
            if (!g_fog_ready) lz_fog_init();

            /* ── tombstone warm path (env ANN_TOMBSTONE_WARM=1) ──────────
             * Read the latch BEFORE walking: a bucket walked by an earlier
             * query is SHUT. Order the walk so entries in SHUT buckets are
             * scored first, then stop after them (budget = their count). This
             * is the hot/cold axis: it never prunes a route that was not walked
             * before. NOTE: an EXACT repeat routes to the same buckets, which
             * the previous run just shut, so the warm set equals the walked set
             * (no reduction); the gain appears when a query's route OVERLAPS
             * previously-walked buckets. Measured — see WARM-PATH.md.
             * The warm set is also handed to the graft bias (below) as the
             * "tombstone × graft" amplifier. */
            if (g_warm_on && rnb > 0) {
                int shut[ANN_MAXK];
                int nshut = 0;
                for (int i = 0; i < rnb; i++) {
                    shut[i] = !wl_is_open(&g_fog, lz_fog_id(rb[i]));
                    if (shut[i]) nshut++;
                }
                if (nshut > 0 && nshut < rnb) {   /* some shut, some open */
                    int cov = 0;
                    for (int i = 0; i < nent; i++) {
                        int a = ents[i].anchor, a2 = ents[i].anchor2;
                        for (int b = 0; b < rnb; b++)
                            if (shut[b] && (a == rb[b] || a2 == rb[b])) { cov++; break; }
                    }
                    if (cov > 0 && cov < nent) {
                        uint32_t *o2 = (uint32_t *)malloc((size_t)nent * sizeof(uint32_t));
                        if (o2) {
                            int w = 0;
                            for (int i = 0; i < nent; i++) {
                                int a = ents[i].anchor, a2 = ents[i].anchor2, in = 0;
                                for (int b = 0; b < rnb; b++)
                                    if (shut[b] && (a == rb[b] || a2 == rb[b])) { in = 1; break; }
                                if (g_warm_flag) g_warm_flag[i] = (unsigned char)in;
                                if (in) o2[w++] = (uint32_t)i;
                            }
                            for (int i = 0; i < nent; i++) {
                                int a = ents[i].anchor, a2 = ents[i].anchor2, in = 0;
                                for (int b = 0; b < rnb; b++)
                                    if (shut[b] && (a == rb[b] || a2 == rb[b])) { in = 1; break; }
                                if (!in) o2[w++] = (uint32_t)i;
                            }
                            if (ord_own) free(ord_own);
                            ord_own = o2; ord = o2;
                            budget = cov;
                            snprintf(rm, sizeof(rm), "%s", "warm");
                            g_warm_ord = 1;   /* a warm path ran: the amplifier is live */
                        }
                    }
                }
            }

            /* the route WALKED shuts the latch (monotone), then persist it */
            for (int rbi = 0; rbi < rnb; rbi++)
                wl_traverse_log(&g_fog, lz_fog_id(rb[rbi]),
                                g_fog_log_open ? &g_fog_log : NULL);
            if (g_fog_log_open && g_fog_log.fp) fflush(g_fog_log.fp);
        } else {
            /* no trained anchor route (or dim mismatch): the file-relationship
             * graph draws a rough map. If the query names files the graph knows,
             * score only the entries the graph proposes (guided subset) instead
             * of the whole index. Falls back to a full scan when the graph
             * cannot propose — so a novel query keeps exact behaviour. This is
             * the "train-free" route: structure proposes, the tombstone pins. */
            int gcap = nent;
            { const char *gc = getenv("ANN_GRAFT_CAND");
              if (gc && atoi(gc) > 0) gcap = atoi(gc); }
            uint32_t *gcand = NULL;
            int gn = 0;
            if (g_graft_on && g_qn > 0) {
                gcand = (uint32_t *)malloc((size_t)(gcap ? gcap : 1) * sizeof(uint32_t));
                if (gcand) gn = graft_candidates(gcand, gcap);
            }
            if (gn > 0) {
                if (ord_own) free(ord_own);
                ord_own = gcand; ord = gcand;
                routed = 1;
                rnb = 0;
                budget = gn;
                snprintf(rm, sizeof(rm), "%s", "graph");
            } else {
                if (gcand) free(gcand);
                ord_own = (uint32_t *)malloc((nent ? (size_t)nent : 1) * sizeof(uint32_t));
                if (ord_own) { for (int i = 0; i < nent; i++) ord_own[i] = (uint32_t)i; ord = ord_own; }
            }
        }
        LARGE_INTEGER qf, qt0, qt1;
        QueryPerformanceFrequency(&qf);
        QueryPerformanceCounter(&qt0);
        int in_run = 0;
        breads = 0;
        for (int oi = 0; ord && oi < nent; oi++) {
            int ei = (int)ord[oi];
            if (ei < 0 || ei >= nent) continue;
            int sel = 1;
            if (sid_filter[0] && strcmp(ents[ei].sid, sid_filter) != 0) sel = 0;
            if (sel && routed && rnb > 0) {
                sel = (ents[ei].anchor < 0);
                for (int rbi = 0; rbi < rnb && !sel; rbi++)
                    if (ents[ei].anchor == rb[rbi] || ents[ei].anchor2 == rb[rbi]) sel = 1;
            }
            if (!sel) { in_run = 0; continue; }
            if (!in_run) { breads++; in_run = 1; }
            if (ents[ei].dim != qd) continue;
            double dot = 0;
            for (int wi = 0; wi < qd; wi++) dot += (double)qv[wi] * ents[ei].vec[wi];
            double sc = (ents[ei].norm > 0) ? dot / (qn * ents[ei].norm) : -1;
            /* Confident Guideline: a soft cluster bias (never a filter). It only
             * ADDS to the score, so recall cannot drop; ranking is nudged toward
             * structurally-related entries. Off unless ANN_GRAFT=1.
             * Tombstone × graft amplifier: if this entry sits in a bucket the
             * tombstone kept warm, its structural weight is multiplied. */
            if (g_graft_on) {
                int warm = (g_warm_ord && g_warm_flag && ei < g_warm_flag_n) ? g_warm_flag[ei] : 0;
                float b = graft_bias(&ents[ei], g_qkeys, g_qn, warm);
                sc += b;
                /* count only entries whose bias was actually multiplied */
                if (warm && g_amp_boost && b != 0.0f) g_amp_applied++;
            }
            scored++;
            for (int ti = 0; ti < topk; ti++) {
                if (sc > top_sc[ti]) {
                    for (int tj = topk - 1; tj > ti; tj--) {
                        top_sc[tj] = top_sc[tj-1];
                        if (top_sid) snprintf(top_sid[tj], 64, "%s", top_sid[tj-1]);
                        if (top_file) snprintf(top_file[tj], 256, "%s", top_file[tj-1]);
                        if (top_np) top_np[tj] = top_np[tj-1];
                    }
                    top_sc[ti] = sc;
                    if (top_sid) snprintf(top_sid[ti], 64, "%s", ents[ei].sid);
                    if (top_file) snprintf(top_file[ti], 256, "%s", ents[ei].file);
                    if (top_np) top_np[ti] = ents[ei].npast;
                    break;
                }
            }
            if (scored >= budget) { truncated = 1; break; }
        }
        QueryPerformanceCounter(&qt1);
        if (scored > 0 && qf.QuadPart > 0)
            us_per_item = (double)(qt1.QuadPart - qt0.QuadPart) * 1e6 / (double)qf.QuadPart / scored;
        free(ord_own);
        if (!routed) breads = nent ? 1 : 0;
    }
    if (!use_cache) lz_index_free(ents, nent);
    if (route_mode) snprintf(route_mode, 16, "%s", rm);
    if (route_bk) for (int ri = 0; ri < ANN_MAXK; ri++) route_bk[ri] = rb[ri];
    if (route_nb_out) *route_nb_out = rnb;
    if (block_reads_out) *block_reads_out = breads;
    if (items_scored_out) *items_scored_out = scored;
    if (nent_out) *nent_out = nent;
    if (budget_out) *budget_out = budget;
    if (truncated_out) *truncated_out = truncated;
    if (us_per_item_out) *us_per_item_out = us_per_item;
    if (fog_out) {
        if (!g_fog_ready) lz_fog_init();
        int sh = 0;
        for (unsigned wi = 0; wi < WL_WORDS; wi++) {
            uint64_t w = g_fog.w[wi];
            while (w) { sh += (int)(w & 1ull); w >>= 1; }
        }
        fog_out->open = (int)WL_FREE - sh;
        fog_out->shut = sh;
        fog_out->lit = rnb;
        fog_out->replayed = g_fog_replayed;
    }
    int nh = 0;
    if (top_sc) for (int ti = 0; ti < topk && top_sc[ti] > -2; ti++) nh++;
    return nh;
}

/* ── Confident Guideline (graft cluster bias) ─────────────────────────
 * A SOFT bias built from graft's file-relationship graph: entries that mention
 * a file in the query's neighborhood get score + lambda*affinity, so the
 * ordering is nudged toward the structurally-related cluster — NEVER a filter,
 * so recall cannot drop. Disabled by default; tune at runtime via env:
 *   ANN_GRAFT=0|1        master switch (default 0)
 *   ANN_GRAFT_DIR        dir with nb.tsv / keys.tsv (default data/graft)
 *   ANN_GRAFT_LAMBDA     bias strength (default 0.05; 0 = off)
 *   ANN_GRAFT_MAXN       max file keys per entry (default 4)
 * Weights live in data/graft/bias.json, produced by scripts/graft_bias.py —
 * change the model there and re-run, no rebuild. */
static float g_graft_lambda = 0.05f;
static int g_graft_maxn = 4;
/* neighbor table: CSR by file id — rows g_nb_off[f]..g_nb_off[f+1] -> (id,score) */
static int *g_nb_off = NULL;          /* size g_nb_nf+1 */
static int *g_nb_id = NULL;
static float *g_nb_sc = NULL;
static int g_nb_nf = 0, g_nb_n = 0;
/* entry -> file keys: parallel arrays indexed by entry position */
static int *g_ent_goff = NULL, *g_ent_gn = NULL, *g_ent_gkeys = NULL, g_ent_n = 0;
/* path -> file id, from files.tsv */
static char **g_fnames = NULL;
static int g_fn_cap = 0;
/* sid -> entry position hash (built once) */
static int *g_sid_hash = NULL;
static int g_sid_cap = 0;
static LZEntry *g_ents_ref = NULL;

/* ── graph → candidate proposal ───────────────────────────────────────
 * When there is no trained anchor route (or a query would fall back to a
 * full "brute" scan), the file-relationship graph proposes the candidate
 * entries to score. Per file key we gather the entries that carry it (CSR
 * below) plus the entries carrying a NEIGHBOUR file, and return the top
 * by structural score. This is "the graph draws a rough map" so the walk is
 * a guided subset, not an O(N) scan — the tombstone then pins the routes
 * actually walked. */
static int *g_file_ent_off = NULL;    /* size g_file_nf+1: entries per file id */
static int *g_file_ent_ids = NULL;    /* entry positions, grouped by file id */
static int g_file_ent_n = 0;
static int g_file_nf = 0;             /* number of file slots (files.tsv space) */
/* reverse adjacency: for file f, the files that point AT f (in-edges). The
 * graph is a DAG, so a leaf/header has forward degree 0 but may have many
 * in-edges — the backward direction is where its context lives. */
static int *g_rev_off = NULL;         /* size g_file_nf+1 */
static int *g_rev_id = NULL;          /* source file ids, grouped by dst file id */
static int g_rev_n = 0;

static int graft_file_id(const char *path) {
    for (int i = 0; i < g_fn_cap; i++)
        if (g_fnames[i] && strcmp(g_fnames[i], path) == 0) return i;
    return -1;
}

static void graft_load_files(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        *tab = '\0';
        int id = atoi(line);
        char *name = tab + 1;
        size_t l = strlen(name);
        while (l && (name[l-1] == '\n' || name[l-1] == '\r')) name[--l] = '\0';
        if (id >= g_fn_cap) {
            int nc = g_fn_cap ? g_fn_cap : 1024;
            while (nc <= id) nc *= 2;
            g_fnames = (char **)realloc(g_fnames, (size_t)nc * sizeof(char *));
            for (int i = g_fn_cap; i < nc; i++) g_fnames[i] = NULL;
            g_fn_cap = nc;
        }
        free(g_fnames[id]);
        g_fnames[id] = strdup(name);
    }
    fclose(f);
}

/* extract repo-path-ish tokens from a query string -> file ids. Matches the
 * same shape scripts/graft_bias.py keys on (core/x.h, tools/y.c, docs/z.md). */
static void graft_query_keys(const char *q) {
    g_qn = 0;
    if (!g_graft_on || !q) return;
    static const char *exts[] = {".h", ".c", ".py", ".md", ".jsonl", NULL};
    for (const char *p = q; *p && g_qn < 16; p++) {
        if (!(p == q || p[-1] == ' ' || p[-1] == '(' || p[-1] == '`' || p[-1] == '"')) continue;
        const char *slash = strchr(p, '/');
        if (!slash || slash - p > 24) continue;
        const char *e = p;
        while (*e && *e != ' ' && *e != ')' && *e != ',' && *e != '"' && *e != '`' && *e != '\t' && *e != '\n') e++;
        int ok = 0;
        for (int i = 0; exts[i]; i++) {
            size_t l = strlen(exts[i]);
            if ((size_t)(e - p) > l && strncmp(e - l, exts[i], l) == 0) { ok = 1; break; }
        }
        if (!ok) continue;
        char tok[256];
        int tl = (int)(e - p); if (tl > 255) tl = 255;
        memcpy(tok, p, (size_t)tl); tok[tl] = '\0';
        int fid = graft_file_id(tok);
        if (fid >= 0) {
            int dup = 0;
            for (int i = 0; i < g_qn; i++) if (g_qkeys[i] == fid) dup = 1;
            if (!dup) g_qkeys[g_qn++] = fid;
        }
        p = e - 1;
    }
}

static void graft_free(void) {
    free(g_nb_off); g_nb_off = NULL;
    free(g_nb_id); g_nb_id = NULL;
    free(g_nb_sc); g_nb_sc = NULL;
    free(g_ent_goff); g_ent_goff = NULL;
    free(g_ent_gn); g_ent_gn = NULL;
    free(g_ent_gkeys); g_ent_gkeys = NULL;
    free(g_sid_hash); g_sid_hash = NULL;
    free(g_file_ent_off); g_file_ent_off = NULL;
    free(g_file_ent_ids); g_file_ent_ids = NULL;
    g_file_ent_n = 0; g_file_nf = 0;
    free(g_rev_off); g_rev_off = NULL;
    free(g_rev_id); g_rev_id = NULL; g_rev_n = 0;
    free(g_warm_flag); g_warm_flag = NULL; g_warm_flag_n = 0;
    for (int i = 0; i < g_fn_cap; i++) free(g_fnames[i]);
    free(g_fnames); g_fnames = NULL;
    g_nb_nf = g_nb_n = g_ent_n = g_sid_cap = g_fn_cap = 0;
    g_ents_ref = NULL;
    g_graft_ready = 0;
}

typedef struct { int fid, dst; float sc; } NbTriple;
static int _nb_cmp(const void *a, const void *b) {
    return ((const NbTriple *)a)->fid - ((const NbTriple *)b)->fid;
}

/* load nb.tsv: rows "<fid>\t<dst>:<score>\t..." -> CSR by fid */
static void graft_load_nb(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[65536];
    int cap = 0, n = 0, maxf = 0;
    NbTriple *tr = NULL;
    while (fgets(line, sizeof(line), f)) {
        int fid = atoi(line);
        if (fid > maxf) maxf = fid;
        const char *p = strchr(line, '\t');
        while (p) {
            p++;
            int dst = atoi(p);
            const char *c = strchr(p, ':');
            float sc = c ? (float)atof(c + 1) : 0.0f;
            if (n == cap) { cap = cap ? cap * 2 : 4096;
                tr = (NbTriple *)realloc(tr, (size_t)cap * sizeof(NbTriple)); }
            tr[n].fid = fid; tr[n].dst = dst; tr[n].sc = sc; n++;
            p = strchr(p, '\t');
        }
    }
    fclose(f);
    qsort(tr, (size_t)n, sizeof(NbTriple), _nb_cmp);
    g_nb_nf = maxf + 1;
    g_nb_off = (int *)calloc((size_t)g_nb_nf + 1, sizeof(int));
    g_nb_id = (int *)malloc((size_t)(n ? n : 1) * sizeof(int));
    g_nb_sc = (float *)malloc((size_t)(n ? n : 1) * sizeof(float));
    g_nb_n = n;
    for (int i = 0; i < n; i++) { g_nb_id[i] = tr[i].dst; g_nb_sc[i] = tr[i].sc; }
    /* CSR offsets: off[f] = first index with fid>=f (dense, handles gaps) */
    int j = 0;
    for (int f = 0; f < g_nb_nf; f++) {
        while (j < n && tr[j].fid < f) j++;
        g_nb_off[f] = j;
    }
    g_nb_off[g_nb_nf] = n;
    free(tr);
}

/* sid -> entry position hash (open addressing). Built once from the entries. */
static unsigned _fnv(const char *s) {
    unsigned h = 2166136261u;
    for (; *s; s++) { h ^= (unsigned char)*s; h *= 16777619u; }
    return h;
}

static void graft_build_sid_hash(LZEntry *ents, int nent) {
    int cap = 1; while (cap < nent * 2) cap <<= 1;
    g_sid_cap = cap;
    g_ents_ref = ents;
    g_sid_hash = (int *)malloc((size_t)cap * sizeof(int));
    for (int i = 0; i < cap; i++) g_sid_hash[i] = -1;
    for (int e = 0; e < nent; e++) {
        unsigned m = (unsigned)cap - 1, i = _fnv(ents[e].sid) & m;
        for (unsigned k = 0; k < (unsigned)cap; k++) {
            unsigned s = (i + k) & m;
            if (g_sid_hash[s] < 0) { g_sid_hash[s] = e; break; }
        }
    }
}

static int graft_sid_pos(const char *sid) {
    if (!g_sid_hash || !g_ents_ref) return -1;
    unsigned m = (unsigned)g_sid_cap - 1, i = _fnv(sid) & m;
    for (unsigned k = 0; k < (unsigned)g_sid_cap; k++) {
        unsigned s = (i + k) & m;
        if (g_sid_hash[s] < 0) return -1;
        if (strcmp(g_ents_ref[g_sid_hash[s]].sid, sid) == 0) return g_sid_hash[s];
    }
    return -1;
}

/* load keys.tsv: rows "<sid>\t<fid>\t<fid>..." -> per-entry key runs */
static void graft_load_keys(const char *path, LZEntry *ents, int nent) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    graft_build_sid_hash(ents, nent);
    int cap = 0, n = 0;
    int *gkeys = NULL;
    char line[65536];
    while (fgets(line, sizeof(line), f)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        *tab = '\0';
        char *sid = line;
        int kstart = n;
        char *p = tab + 1;
        while (*p) {
            int id = atoi(p);
            if (n == cap) { cap = cap ? cap * 2 : 4096;
                gkeys = (int *)realloc(gkeys, (size_t)cap * sizeof(int)); }
            gkeys[n++] = id;
            p = strchr(p, '\t');
            if (!p) break;
            p++;
        }
        int kn = n - kstart;
        if (kn > g_graft_maxn) kn = g_graft_maxn;
        int pos = graft_sid_pos(sid);
        if (pos >= 0) { ents[pos].goff = kstart; ents[pos].gn = kn; }
    }
    fclose(f);
    g_ent_gkeys = gkeys; g_ent_n = n;
}

static void graft_init(LZEntry *ents, int nent) {
    graft_free();
    /* the amplifier flags are read unconditionally so the (graft-off) path
     * still knows the user's intent; it stays inert without a graft bias. */
    { const char *ab = getenv("ANN_AMP_BOOST"); g_amp_boost = (ab && atoi(ab)) ? 1 : 0; }
    { const char *ak = getenv("ANN_AMP_K"); if (ak && atof(ak) > 0) g_amp_k = (float)atof(ak); }
    { const char *bw = getenv("ANN_GRAFT_BACKWARD"); g_backward = (bw && atoi(bw)) ? 1 : 0; }
    const char *on = getenv("ANN_GRAFT");
    if (!on || !atoi(on)) { g_graft_ready = 1; return; }   /* off by design */
    g_graft_on = 1;
    const char *le = getenv("ANN_GRAFT_LAMBDA");
    if (le) g_graft_lambda = (float)atof(le);
    const char *me = getenv("ANN_GRAFT_MAXN");
    if (me && atoi(me) > 0) g_graft_maxn = atoi(me);
    const char *dir = getenv("ANN_GRAFT_DIR");
    char nbp[512], kp[512], fp[512];
    snprintf(nbp, sizeof(nbp), "%s/nb.tsv", dir && dir[0] ? dir : "data/graft");
    snprintf(kp, sizeof(kp), "%s/keys.tsv", dir && dir[0] ? dir : "data/graft");
    snprintf(fp, sizeof(fp), "%s/files.tsv", dir && dir[0] ? dir : "data/graft");
    graft_load_files(fp);
    graft_load_nb(nbp);
    graft_load_keys(kp, ents, nent);
    /* build the sid→entry CSR used by the graph candidate proposal (needs the
     * per-entry keys loaded above). Indexed by file id over the files.tsv
     * space (g_fn_cap), which is a superset of the nb.tsv space. */
    if (g_ent_n > 0 && g_fn_cap > 0) {
        g_file_nf = g_fn_cap;
        g_file_ent_off = (int *)calloc((size_t)g_file_nf + 1, sizeof(int));
        g_file_ent_ids = (int *)malloc((size_t)g_ent_n * sizeof(int));
        if (g_file_ent_off && g_file_ent_ids) {
            for (int i = 0; i < nent; i++)
                for (int k = 0; k < ents[i].gn; k++) {
                    int fid = g_ent_gkeys[ents[i].goff + k];
                    if (fid >= 0 && fid < g_file_nf) g_file_ent_off[fid + 1]++;
                }
            for (int f = 0; f < g_file_nf; f++) g_file_ent_off[f + 1] += g_file_ent_off[f];
            { int *cur = (int *)malloc((size_t)g_file_nf * sizeof(int));
              if (cur) {
                  for (int f = 0; f < g_file_nf; f++) cur[f] = g_file_ent_off[f];
                  for (int i = 0; i < nent; i++)
                      for (int k = 0; k < ents[i].gn; k++) {
                          int fid = g_ent_gkeys[ents[i].goff + k];
                          if (fid >= 0 && fid < g_file_nf) g_file_ent_ids[cur[fid]++] = i;
                      }
                  free(cur);
              } }
            g_file_ent_n = nent;
        }
    }
    /* reverse adjacency from the nb.tsv edges (transpose): for each dst, the
     * srcs that point at it. Used by the backward candidate expansion. */
    if (g_nb_nf > 0 && g_nb_n > 0) {
        g_rev_off = (int *)calloc((size_t)g_file_nf + 1, sizeof(int));
        g_rev_id = (int *)malloc((size_t)g_nb_n * sizeof(int));
        if (g_rev_off && g_rev_id) {
            /* nb.tsv is CSR by src: row src = [g_nb_off[src], g_nb_off[src+1]) */
            for (int src = 0; src < g_nb_nf && src < g_file_nf; src++)
                for (int t = g_nb_off[src]; t < g_nb_off[src + 1]; t++) {
                    int dst = g_nb_id[t];
                    if (dst >= 0 && dst < g_file_nf) g_rev_off[dst + 1]++;
                }
            for (int f = 0; f < g_file_nf; f++) g_rev_off[f + 1] += g_rev_off[f];
            { int *cur = (int *)malloc((size_t)g_file_nf * sizeof(int));
              if (cur) {
                  for (int f = 0; f < g_file_nf; f++) cur[f] = g_rev_off[f];
                  for (int src = 0; src < g_nb_nf && src < g_file_nf; src++)
                      for (int t = g_nb_off[src]; t < g_nb_off[src + 1]; t++) {
                          int dst = g_nb_id[t];
                          if (dst >= 0 && dst < g_file_nf) g_rev_id[cur[dst]++] = src;
                      }
                  free(cur);
              } }
            g_rev_n = g_nb_n;
        }
    }
    g_graft_ready = 1;
    fprintf(stderr, "[ann_search] graft bias ON lambda=%.3f files=%d entries_with_keys=%d route=%s rev=%d\n",
            g_graft_lambda, g_fn_cap, g_ent_n, g_file_ent_n ? "ready" : "off", g_rev_n);
}

/* called at request time, before query keys are needed (index may not have
 * been loaded at boot, and env must be read before the first query). */
static void graft_ensure_init(void) {
    if (g_graft_ready) return;
    LZEntry *ents = NULL; int nent = 0;
    lz_index_cached(&ents, &nent);
    graft_init(ents, nent);
}

/* soft bias for one entry: sum over the entry's file keys of the neighborhood
 * score of any key shared with the query's keys. Returns 0 when off/absent.
 * When `warm` and the amplifier is on, the result is scaled by g_amp_k — the
 * tombstone strengthens the structural weight of a route it keeps hot. */
static float graft_bias(const LZEntry *e, const int *qkeys, int qn, int warm) {
    if (!g_graft_on || !g_graft_ready || e->gn <= 0 || qn <= 0) return 0.0f;
    float best = 0.0f;
    for (int i = 0; i < e->gn; i++) {
        int k = g_ent_gkeys[e->goff + i];
        for (int j = 0; j < qn; j++) {
            if (k == qkeys[j]) { best = g_graft_lambda; i = e->gn; break; }  /* exact file match */
            /* neighbor of the query key? */
            int a = g_nb_off[k], b = g_nb_off[k + 1];
            for (int t = a; t < b; t++) if (g_nb_id[t] == qkeys[j]) {
                float v = g_graft_lambda * g_nb_sc[t];
                if (v > best) best = v;
            }
        }
    }
    if (warm && g_amp_boost) best *= g_amp_k;   /* tombstone × graft amplifier */
    return best;
}

/* graph → candidate entries. The map the graph draws when there is no trained
 * anchor route: take the entries carrying the query's file keys (score 1.0)
 * plus the entries carrying a NEIGHBOUR file (score = edge affinity), then
 * return the top-`cap` by score. This is a guided subset of the index built
 * from structure alone — no training, no geometry. Returns the candidate
 * count (0 when the graph cannot propose). The caller falls back to the full
 * scan only when this is 0. */
static int graft_candidates(uint32_t *out, int cap) {
    if (!g_graft_on || !g_graft_ready || !g_file_ent_off || !g_file_ent_ids ||
        !g_ents_ref || g_qn <= 0 || cap <= 0) return 0;
    static int escore[ANCHR_MAXK];
    static int eseen[ANCHR_MAXK];
    for (int i = 0; i < ANCHR_MAXK; i++) { escore[i] = 0; eseen[i] = -1; }
    int nseen = 0;
    for (int j = 0; j < g_qn; j++) {
        int fid = g_qkeys[j];
        if (fid < 0 || fid >= g_file_nf) continue;
        /* exact file: entries carrying it score full */
        for (int t = g_file_ent_off[fid]; t < g_file_ent_off[fid + 1]; t++) {
            int ei = g_file_ent_ids[t];
            int idx = -1;
            for (int s = 0; s < nseen; s++) if (eseen[s] == ei) { idx = s; break; }
            if (idx < 0) { if (nseen >= ANCHR_MAXK) continue; idx = nseen; eseen[nseen++] = ei; }
            if (escore[idx] < 1) escore[idx] = 1;
        }
        /* neighbours: entries carrying a related file score by affinity */
        for (int t = g_nb_off[fid]; t < g_nb_off[fid + 1]; t++) {
            int nb = g_nb_id[t];
            if (nb < 0 || nb >= g_file_nf) continue;
            int w = (int)(g_nb_sc[t] * 10.0f);
            if (w < 1) continue;
            for (int u = g_file_ent_off[nb]; u < g_file_ent_off[nb + 1]; u++) {
                int ei = g_file_ent_ids[u];
                int idx = -1;
                for (int s = 0; s < nseen; s++) if (eseen[s] == ei) { idx = s; break; }
                if (idx < 0) { if (nseen >= ANCHR_MAXK) continue; idx = nseen; eseen[nseen++] = ei; }
                if (escore[idx] < w) escore[idx] = w;
            }
        }
        /* backward: entries carrying a file that POINTS AT this file (in-edges).
         * The graph is a DAG, so a leaf/header has no forward degree but its
         * context is exactly who points at it. Weight 1 (same as an exact file
         * hit) because "a file that uses this file" is a strong structural tie. */
        if (g_backward && g_rev_off) {
            for (int t = g_rev_off[fid]; t < g_rev_off[fid + 1]; t++) {
                int src = g_rev_id[t];
                if (src < 0 || src >= g_file_nf) continue;
                for (int u = g_file_ent_off[src]; u < g_file_ent_off[src + 1]; u++) {
                    int ei = g_file_ent_ids[u];
                    int idx = -1;
                    for (int s = 0; s < nseen; s++) if (eseen[s] == ei) { idx = s; break; }
                    if (idx < 0) { if (nseen >= ANCHR_MAXK) continue; idx = nseen; eseen[nseen++] = ei; }
                    if (escore[idx] < 1) escore[idx] = 1;
                }
            }
        }
    }
    int n = 0;
    for (int c = 0; c < cap && n < cap; c++) {
        int best = -1, bi = -1;
        for (int s = 0; s < nseen; s++) if (escore[s] > best) { best = escore[s]; bi = s; }
        if (bi < 0 || best <= 0) break;
        out[n++] = (uint32_t)eseen[bi];
        escore[bi] = -1;
    }
    return n;
}

/* ── anchor regrow hook (ported from gguf_lazy_serve.c L632-707) ──
 * Trains anchors over the current index, writes PATH_ANCH (atomic + checksum),
 * posts every entry into its top-2 buckets, writes the placement perm to
 * PATH_ORDER, and rewrites PATH_INDEX with anchor/anchor2 fields added.
 * This is what makes a fresh base index (sid/file/npast/dim/vec) routable. */
static int lz_anchor_refresh(int *Kout, int *nout) {
    if (Kout) *Kout = 0;
    LZEntry *a = NULL;
    int n = 0;
    lz_index_load(&a, &n);
    if (nout) *nout = n;
    int rc = 1;
    if (n >= LZ_ANCH_MIN && a[0].dim > 0 && a[0].dim <= ANCHR_MAXD) {
        int dim = a[0].dim, K = n >= 64 ? 8 : n >= 32 ? 4 : 2;
        if (K > LZ_ANCH_MAXK) K = LZ_ANCH_MAXK;
        int m = 0;
        for (int i = 0; i < n; i++) if (a[i].dim == dim) m++;
        if (m >= K) {
            float *X = (float *)malloc((size_t)m * dim * sizeof(float));
            float *C = (float *)malloc((size_t)K * dim * sizeof(float));
            int *rows = (int *)malloc((size_t)n * sizeof(int));
            int *rows2 = (int *)malloc((size_t)n * sizeof(int));
            if (X && C && rows && rows2) {
                int w = 0;
                for (int i = 0; i < n; i++)
                    if (a[i].dim == dim) { memcpy(X + (size_t)w * dim, a[i].vec, (size_t)dim * sizeof(float)); w++; }
                if (anch_train(X, m, dim, K, C, NULL) == 0 &&
                    anch_save_atomic(PATH_ANCH, C, K, dim, n) == 0) {
                    int okv = 0;
                    float *V = (float *)malloc((size_t)K * dim * sizeof(float));
                    if (V) {
                        int vd = 0, vn = 0;
                        okv = (anch_load(PATH_ANCH, V, K, dim, &vd, &vn) == K &&
                               vd == dim && vn == n &&
                               memcmp(V, C, (size_t)K * dim * sizeof(float)) == 0);
                        free(V);
                    }
                    if (okv) {
                        int t2[LZ_ANCH_MAXK];
                        for (int i = 0; i < n; i++) {
                            if (a[i].dim != dim) { rows[i] = -1; rows2[i] = -1; continue; }
                            int nb = anch_route(a[i].vec, C, K, dim, LZ_OVERLAP_TOPB, t2);
                            rows[i] = nb > 0 ? t2[0] : -1;
                            rows2[i] = nb > 1 ? t2[1] : -1;
                        }
                        uint32_t *perm = (uint32_t *)malloc((size_t)n * sizeof(uint32_t));
                        if (perm && anch_perm(rows, n, K, perm) == 0) {
                            FILE *of = fopen(PATH_ORDER, "wb");
                            if (of) { fwrite(perm, sizeof(uint32_t), (size_t)n, of); fclose(of); }
                            char tmp[512];
                            snprintf(tmp, sizeof(tmp), "%s.tmp", PATH_INDEX);
                            FILE *ix = fopen(tmp, "w");
                            if (ix) {
                                for (int i = 0; i < n; i++) {
                                    char esid[256], efile[1024];
                                    lz_json_escape(esid, sizeof(esid), a[i].sid);
                                    lz_json_escape(efile, sizeof(efile), a[i].file);
                                    fprintf(ix, "{\"sid\":\"%s\",\"file\":\"%s\",\"npast\":%d,\"anchor\":%d,\"anchor2\":%d,\"dim\":%d,\"vec\":[",
                                            esid, efile, a[i].npast, rows[i], rows2[i], a[i].dim);
                                    for (int vi = 0; vi < a[i].dim; vi++)
                                        fprintf(ix, "%s%.6g", vi ? "," : "", (double)a[i].vec[vi]);
                                    fprintf(ix, "]}\n");
                                }
                                fclose(ix);
                                remove(PATH_INDEX);
                                rename(tmp, PATH_INDEX);
                                if (Kout) *Kout = K;
                                rc = 0;
                            }
                            free(perm);
                        }
                    } else rc = -1;
                }
            }
            free(X); free(C); free(rows); free(rows2);
        }
    } else if (n < LZ_ANCH_MIN) {
        remove(PATH_ANCH);
        remove(PATH_ORDER);
    }
    lz_index_free(a, n);
    if (rc == 0) { lz_index_cache_invalidate(); lz_route_cache_invalidate(); }
    return rc;
}

/* ── GET /health ── */
static int handle_health(sock_t cli) {
    LZEntry *ents = NULL; int nent = 0;
    lz_index_cached(&ents, &nent);
    int routed = lz_route_cached(nent);
    if (!g_fog_ready) lz_fog_init();
    int sh = 0;
    for (unsigned wi = 0; wi < WL_WORDS; wi++) {
        uint64_t w = g_fog.w[wi];
        while (w) { sh += (int)(w & 1ull); w >>= 1; }
    }
    /* every search depends on the embed sidecar, so a green /health that does
     * not probe it hides a 100%-broken search path from the supervisor. Ping it
     * (short timeout) and report honestly; the index still reports its state. */
    int embed_ok = (lz_embed("ping", (float[4]){0}, 1) >= 0) ? 1 : 0;
    /* the index path is a Windows path — escape it or the JSON is invalid
     * (\t, \a, \k are not legal escapes). */
    char esc[512];
    lz_json_escape(esc, sizeof(esc), PATH_INDEX);
    char b[768];
    int bl = snprintf(b, sizeof(b),
        "{\"status\":\"%s\",\"service\":\"ann_search\",\"fork_of\":\"DWGLS 8c85f9f\","
        "\"index\":\"%s\",\"entries\":%d,\"routed\":%d,\"maxk\":%d,\"embed\":\"%s\","
        "\"fog\":{\"replayed\":%d,\"shut\":%d,\"free\":%u}}",
        (embed_ok && nent > 0) ? "ok" : "degraded",
        esc, nent, routed, ANN_MAXK, embed_ok ? "ok" : "down",
        g_fog_replayed, sh, (unsigned)WL_FREE);
    lz_http_send(cli, "200 OK", "application/json", b, bl);
    sock_close(cli);
    return 0;
}

/* parse a JSON float array field ("vec":[f,f,...]); returns count or 0.
 * Lets a caller send its OWN query vector instead of having the sidecar embed
 * the text, so ranking uses the caller's model. */
static int lz_json_float_array(const char *json, const char *key, float *out, int cap) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p = strchr(p + strlen(pat), ':');
    if (!p) return 0;
    p++;
    while (*p == ' ' || *p == '[') p++;
    int n = 0;
    while (*p && *p != ']' && n < cap) {
        while (*p == ' ' || *p == ',') p++;
        if (*p == ']' || !*p) break;
        out[n++] = (float)atof(p);
        while (*p && *p != ',' && *p != ']') p++;
    }
    return n;
}

/* ── POST /v1/state/search ── */
static int handle_search(sock_t cli, const char *rq) {
    char *bd3 = strstr(rq, "\r\n\r\n");
    if (!bd3) { sock_close(cli); return 0; }
    bd3 += 4;
    char sq[2048] = "";
    int stopk = 3;
    char sid_filter[64] = "";
    const char *qp = strstr(bd3, "\"query\"");
    if (qp) {
        qp = strchr(qp + 7, ':');
        if (qp) {
            qp++;
            while (*qp == ' ') qp++;
            if (*qp == '"') {
                qp++;
                lz_json_unescape(sq, (int)sizeof(sq), qp);
            }
        }
    }
    if (!sq[0]) {
        /* no "query" text — accept a caller-supplied "vec" instead (the caller
         * embedded it itself, so ranking uses ITS model, not the sidecar's). */
        const char *vp = strstr(bd3, "\"vec\"");
        if (!vp) {
            const char *eb = "{\"status\":\"error\",\"error\":\"missing_query\","
                             "\"detail\":\"send \\\"query\\\" text or a \\\"vec\\\" array\"}";
            lz_http_send(cli, "400 Bad Request", "application/json", eb, (int)strlen(eb));
            sock_close(cli);
            return 0;
        }
    }
    stopk = lz_json_int(bd3, "topk", 3);
    {
        const char *sfp = strstr(bd3, "\"sid\"");
        if (sfp) {
            sfp = strchr(sfp + 5, ':');
            if (sfp) {
                sfp++;
                while (*sfp == ' ' || *sfp == '"') sfp++;
                int sfi = 0;
                while (*sfp && *sfp != '"' && *sfp != ',' && *sfp != '}' && sfi < 63)
                    sid_filter[sfi++] = *sfp++;
                sid_filter[sfi] = '\0';
            }
        }
    }
    if (stopk < 1) stopk = 1;
    if (stopk > ANN_MAXK) {
        char eb[160];
        int el = snprintf(eb, sizeof(eb),
            "{\"status\":\"error\",\"error\":\"topk_out_of_range\","
            "\"requested\":%d,\"max\":%d}", stopk, ANN_MAXK);
        lz_http_send(cli, "400 Bad Request", "application/json", eb, el);
        sock_close(cli);
        return 0;
    }
    static char sresp[16384];
    int srlen = 0;
    float qv[1024];
    int qd = 0;
    /* prefer a caller-supplied vector: same model as the caller's index means
     * the ranking is exactly the caller's (no cross-embedder drift). */
    qd = lz_json_float_array(bd3, "vec", qv, 1024);
    if (qd <= 0 && sq[0]) qd = lz_embed(sq, qv, 1024);
    if (qd > 0) {
        graft_ensure_init();    /* tables must exist before extracting keys */
        graft_query_keys(sq);   /* extract the query's file keys for the bias */
        char top_sid[ANN_MAXK][64]; char top_file[ANN_MAXK][256];
        int top_np[ANN_MAXK]; double top_sc[ANN_MAXK];
        char route_mode[16];
        int route_bk[ANN_MAXK], route_nb = 0, block_reads = 0;
        int items_scored = 0, nent = 0, budget = 0, truncated = 0;
        double us_per_item = 0;
        LZFog fog = { (int)WL_FREE, 0, 0, 0 };
        lz_mem_search(qv, qd, sid_filter, stopk,
            top_sid, top_file, top_np, top_sc,
            route_mode, route_bk, &route_nb,
            &block_reads, &items_scored, &nent,
            &budget, &truncated, &us_per_item, &fog);
        srlen = snprintf(sresp, sizeof(sresp), "{\"status\":\"ok\",\"hits\":[");
        for (int ti = 0; ti < stopk && top_sc[ti] > -2; ti++) {
            char esid[256], efile[1024];
            lz_json_escape(esid, sizeof(esid), top_sid[ti]);
            lz_json_escape(efile, sizeof(efile), top_file[ti]);
            srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
                "%s{\"sid\":\"%s\",\"file\":\"%s\",\"npast\":%d,\"score\":%.4f}",
                ti ? "," : "", esid, efile, top_np[ti], top_sc[ti]);
        }
        srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen, "]");
        srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
            ",\"route\":{\"mode\":\"%s\",\"buckets\":[", route_mode);
        for (int rb = 0; rb < route_nb; rb++)
            srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
                "%s%d", rb ? "," : "", route_bk[rb]);
        srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
            "],\"block_reads\":%d,\"items_scored\":%d,\"items_total\":%d,\"us_per_item\":%.2f,\"budget\":%d,\"truncated\":%d,",
            block_reads, items_scored, nent, us_per_item, budget, truncated);
        srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
            "\"fog\":{\"lit\":%d,\"open\":%d,\"shut\":%d,\"replayed\":%d,\"free\":%u},",
            fog.lit, fog.open, fog.shut, fog.replayed, (unsigned)WL_FREE);
        srlen += snprintf(sresp + srlen, sizeof(sresp) - srlen,
            "\"amp\":{\"on\":%d,\"k\":%.2f,\"applied\":%d}}}",
            (g_amp_boost && g_graft_on) ? 1 : 0, (double)g_amp_k, g_amp_applied);
    } else {
        srlen = snprintf(sresp, sizeof(sresp),
            "{\"status\":\"error\",\"error\":\"embed_unavailable\","
            "\"detail\":\"the embed sidecar could not embed this query\"}");
        lz_http_send(cli, "503 Service Unavailable", "application/json", sresp, srlen);
        sock_close(cli);
        return 0;
    }
    lz_http_send(cli, "200 OK", "application/json", sresp, srlen);
    sock_close(cli);
    return 0;
}

/* ── per-connection handling ──────────────────────────────────────────
 * The server was single-threaded with no socket timeout, so one idle or
 * half-open connection froze the whole service (including /health). Now each
 * connection runs on its own thread with a recv/send timeout, and the search
 * core (shared index/anchor/fog caches) is guarded by a critical section so
 * the caches stay consistent — the same serialization as before, but a slow
 * client can no longer hold the accept loop. */
static CRITICAL_SECTION g_lock;

static void dispatch(sock_t cli, char *rq) {
    char method[16] = "", path[256] = "";
    sscanf(rq, "%15s %255s", method, path);
    if (strcmp(method, "GET") == 0 && strcmp(path, "/health") == 0) {
        handle_health(cli);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/regrow") == 0) {
        int rK = 0, rN = 0;
        int rrc = lz_anchor_refresh(&rK, &rN);
        const char *rmode = rrc == 0 ? "anchor" : (rrc == 1 ? "brute" : "error");
        char rresp[256];
        int rrlen = snprintf(rresp, sizeof(rresp),
            "{\"status\":\"%s\",\"mode\":\"%s\",\"nent\":%d,\"K\":%d}",
            rrc < 0 ? "error" : "ok", rmode, rN, rK);
        lz_http_send(cli, "200 OK", "application/json", rresp, rrlen);
        sock_close(cli);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/search") == 0) {
        handle_search(cli, rq);
    } else {
        const char *b = "{\"error\":\"not_found\",\"try\":\"/health or POST /v1/state/search\"}";
        lz_http_send(cli, "404 Not Found", "application/json", b, (int)strlen(b));
        sock_close(cli);
    }
}

static DWORD WINAPI client_thread(LPVOID arg) {
    sock_t cli = (sock_t)(intptr_t)arg;
    DWORD tv = 5000;   /* ms — a stalled client frees its own thread, not the server */
    setsockopt(cli, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));
    setsockopt(cli, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, sizeof(tv));
    char *rq = (char *)malloc(1 << 20);
    if (!rq) { sock_close(cli); return 0; }
    int n = lz_http_recv(cli, rq, 1 << 20);
    if (n > 0) {
        EnterCriticalSection(&g_lock);
        dispatch(cli, rq);
        LeaveCriticalSection(&g_lock);
    } else {
        sock_close(cli);
    }
    free(rq);
    return 0;
}

int main(int argc, char **argv) {
    paths_init();
    int port = 8096;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--serve") == 0 && i + 1 < argc) port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) port = atoi(argv[++i]);
    }
    const char *pe = getenv("ANN_PORT");
    if (pe && atoi(pe) > 0) port = atoi(pe);

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { fprintf(stderr, "wsa fail\n"); return 1; }
    InitializeCriticalSection(&g_lock);
    lz_fog_init();
    { LZEntry *e; int n; int c = lz_index_cached(&e, &n);
      fprintf(stderr, "[ann_search] index=%s entries=%d cache=%d maxk=%d\n",
              PATH_INDEX, n, c, ANN_MAXK);
      fprintf(stderr, "[ann_search] anchors=%s order=%s foglog=%s replayed=%d\n",
              PATH_ANCH, PATH_ORDER, PATH_LOG, g_fog_replayed); }

    sock_t srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv == SOCK_INVALID) { fprintf(stderr, "socket fail\n"); return 1; }
    int one = 1;
    /* STRESS-REPORT #8 — exclusive bind, verified. On Windows SO_REUSEADDR
     * lets a SECOND instance bind the SAME port (split-brain). The first fix
     * attempt set SO_REUSEADDR *then* SO_EXCLUSIVEADDRUSE, but on Windows the
     * two are mutually exclusive: the exclusive setsockopt fails
     * WSAEINVAL(10022) and the port stays shareable — verified: two live
     * instances on one port. So do NOT set SO_REUSEADDR on Windows; keep only
     * SO_EXCLUSIVEADDRUSE, which makes a second bind fail loudly with
     * WSAEADDRINUSE(10048). Non-Windows keeps SO_REUSEADDR (standard
     * fast-restart semantics; there is no split-brain there). */
#ifdef SO_EXCLUSIVEADDRUSE
    if (setsockopt(srv, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *)&one, sizeof(one)) != 0)
        fprintf(stderr, "[ann_search] SO_EXCLUSIVEADDRUSE setsockopt failed: %d\n", WSAGetLastError());
#else
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof(one));
#endif
    struct sockaddr_in sa;
    sa.sin_family = AF_INET;
    sa.sin_port = htons((uint16_t)port);
    sa.sin_addr.s_addr = INADDR_ANY;
    if (bind(srv, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        fprintf(stderr, "bind %d failed (already in use?)\n", port); return 1;
    }
    listen(srv, 64);
    fprintf(stderr, "[ann_search] listening on %d\n", port);

    for (;;) {
        sock_t cli = accept(srv, NULL, NULL);
        if (cli == SOCK_INVALID) continue;
        HANDLE h = CreateThread(NULL, 0, client_thread, (LPVOID)(intptr_t)cli, 0, NULL);
        if (h) CloseHandle(h);
        else client_thread((LPVOID)(intptr_t)cli);   /* fallback: inline */
    }
    WSACleanup();
    return 0;
}
