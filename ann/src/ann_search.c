/*
 * ann_search.c — standalone ANN semantic-search server (fork of the DWGLS
 * anchor-route search core).
 *
 * WHAT THIS IS
 *   A minimal HTTP server that answers POST /v1/state/search over a
 *   build/ann_index.jsonl vector index, using the anchor-bucket route +
 *   geo_wang_latch tombstone from DWGLS. It is the SEARCH half of
 *   tools/gguf_lazy_serve.c, extracted whole and cut free of every GGUF /
 *   llama / model-load dependency. No llama.h, no ggml, no backend DLLs, no
 *   .gguf file: the embedder is an EXTERNAL sidecar reached over HTTP.
 *
 * KV IS A USER, NOT PART OF THIS
 *   This is the general ANN search core. KV (cold-KV, kv_tess_cache, etc.) is
 *   a separate field that USES it: KV manages its own storage and feeds a
 *   result to ANN. Do not fold KV storage/format into this file, and do not
 *   name the index/anchors "kv_*" — those filenames are a leftover of KV
 *   having been the first consumer (now renamed ann_*; see KV-USER.md).
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
#include <stdarg.h>
#include <time.h>

/* the embed call's wait ceiling in ms (0 = wait forever). The health probe
 * lowers it: /health must answer even when the sidecar is starved. */
static int g_http_timeout_ms = 15000;
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

/* ── paths (overridable so the fork is not hard-wired to one checkout) ──
 * ANN is the search core; KV is a USER of it, not part of it. The default
 * filenames are therefore neutral (ann_*), not kv_*: the kv_ prefix was a
 * leftover of KV being the first consumer and made the core read as "the KV
 * thing". Any existing kv_* checkout still works by setting the env vars. */
static const char *PATH_INDEX;   /* ann_index.jsonl  */
static const char *PATH_ANCH;    /* ann_anchors.bin  */
static const char *PATH_ORDER;   /* ann_index.order  */
static const char *PATH_LOG;     /* P3 route log     */
static void paths_init(void) {
    const char *e;
    e = getenv("ANN_INDEX_PATH"); PATH_INDEX = (e && e[0]) ? e : "data/ann_index.jsonl";
    e = getenv("ANN_ANCH_PATH");  PATH_ANCH  = (e && e[0]) ? e : "data/ann_anchors.bin";
    e = getenv("ANN_ORDER_PATH"); PATH_ORDER = (e && e[0]) ? e : "data/ann_index.order";
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
    /* A BOUNDED CONNECT (2026-10-22): the embed sidecar runs ONE slot, so
     * while it is busy its accept backlog is full and a blocking connect()
     * waits for the SYN to be retried — measured 53 s, and it happened while
     * the request lock was HELD, so every other request queued behind a
     * health probe. A non-blocking connect + select() bounds the wait to
     * the timeout, and the failure is honest ("embed":"down"). */
    int tmo = g_http_timeout_ms;
    if (tmo <= 0) tmo = 15000;
    u_long nb = 1;
    ioctlsocket(fd, FIONBIO, &nb);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        int werr = WSAGetLastError();
        if (werr != WSAEWOULDBLOCK) { sock_close(fd); return -2; }
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(fd, &wfds);
        struct timeval tv;
        tv.tv_sec = tmo / 1000;
        tv.tv_usec = (tmo % 1000) * 1000;
        int sel = select(fd + 1, NULL, &wfds, NULL, &tv);
        if (sel <= 0) { sock_close(fd); return -3; }     /* connect timed out */
        int soerr = 0, slen = sizeof(soerr);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, (char *)&soerr, &slen);
        if (soerr != 0) { sock_close(fd); return -2; }
    }
    nb = 0;
    ioctlsocket(fd, FIONBIO, &nb);
    char hdr[512];
    int hlen = snprintf(hdr, sizeof(hdr),
        "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
        "Content-Length: %d\r\nConnection: close\r\n\r\n",
        path, host, (int)strlen(body));
    /* A BOUNDED WAIT (2026-10-22): the call had no socket timeout, so a
     * starved sidecar (the box is busy with a regrow) held the request
     * lock for as long as the client cared to wait — /health did not answer
     * within 30 s during a scheduled regrow. The timeout turns "waited
     * forever" into an honest failure. */
    { int rtmo = g_http_timeout_ms;
      if (rtmo > 0) {
          setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, (const char *)&rtmo, sizeof(rtmo));
          setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, (const char *)&rtmo, sizeof(rtmo));
      }
    }
    send(fd, hdr, hlen, 0);
    send(fd, body, (int)strlen(body), 0);
    int total = 0, n;
    while (total < cap - 1 && (n = recv(fd, resp + total, cap - total - 1, 0)) > 0)
        total += n;
    resp[total < 0 ? 0 : total] = '\0';
    sock_close(fd);
    return total;
}
/* ── embed-input sanitizer (tokenizer vocab gap) ──────────────────────
 * The multilingual-e5-small GGUF tokenizer has holes: some codepoints make
 * llama-server /embedding return 500 ("invalid unordered_map<K,T> key"). The
 * main one is U+0E33 (Thai SARA AM, the "ำ" in "ทำ"); the Thai block gaps
 * (U+0E3B/0E3C/0E4E/0E5B, U+0E5C..U+0E7F) and several math symbols do the same.
 * The index build side already normalizes with the SAME rule
 * (scripts/build_index.py:sanitize), so a query must be normalized identically
 * or the query text and the indexed text live in different spaces. This is the
 * C twin of that function: NFKC-lite for the codepoints NFKC actually fixes
 * (U+0E33 -> U+0E4D U+0E32, a 1->2 byte expansion) plus an ASCII map for the
 * symbols NFKC leaves alone, and a drop for the remaining Thai-block holes.
 * Meaning-preserving; no-op for English/plain ASCII. */
static int lz_is_thai_hole(unsigned cp) {
    /* Thai block codepoints the e5 GGUF tokenizer 500s on (measured), minus
     * U+0E33 which is decomposed by the NFKC-lite pass above it. */
    if (cp == 0x0E3B || cp == 0x0E3C || cp == 0x0E4E || cp == 0x0E5B) return 1;
    if (cp >= 0x0E5C && cp <= 0x0E7F) return 1;
    return 0;
}
static const char *lz_ascii_map(unsigned cp) {
    switch (cp) {
        case 0x2209: return " not in ";
        case 0x220B: return " contains ";
        case 0x2216: return " minus ";
        case 0x221D: return " proportional to ";
        case 0x2224: return " does not divide ";
        case 0x2245: return " ~= ";
        case 0x22C0: return " AND ";
        case 0x22C1: return " OR ";
        case 0x23ED: return " next ";
        case 0x23F8: return " pause ";
        case 0x27FA: return " <=> ";
        case 0x2B20: return " pentagon ";
        /* single chars whose NFKC EXPANDS — the tokenizer 500s on them raw
         * (measured live: 2160-B and 2150-F all 500, № ℀ ℁ … ½ too).  The
         * corpus build (scripts/build_index.py) NFKC's every row before
         * embedding, so the query side must land in the SAME space. */
        case 0x2026: return "...";
        case 0x2025: return "..";
        case 0x22EF: return "...";
        case 0x2116: return "No";
        case 0x2100: return "a/c";
        case 0x2101: return "a/c";
        case 0x00BC: return "1/4";
        case 0x00BD: return "1/2";
        case 0x00BE: return "3/4";
        /* Letterlike math: NFKC expands each to its plain letter(s). */
        case 0x2102: return "C";  case 0x2103: return "deg C";
        case 0x2107: return "E";  case 0x2109: return "deg F";
        case 0x210A: return "g";  case 0x210B: return "H";
        case 0x210C: return "H";  case 0x210D: return "H";
        case 0x210E: return "h";  case 0x210F: return "hbar";
        case 0x2110: return "I";  case 0x2111: return "I";
        case 0x2112: return "L";  case 0x2113: return "l";
        case 0x2115: return "N";  case 0x2119: return "P";
        case 0x211A: return "Q";  case 0x211B: return "R";
        case 0x211C: return "R";  case 0x211D: return "R";
        case 0x2122: return "TM"; case 0x2124: return "Z";
        case 0x2126: return "ohm"; case 0x212A: return "K";
        case 0x212B: return "A";
        case 0x212C: return "B";  case 0x212D: return "C";
        case 0x212F: return "e";  case 0x2130: return "E";
        case 0x2131: return "F";  case 0x2133: return "M";
        case 0x2134: return "o";
        /* math alphanumeric series fold mechanically: each series is base+26
         * (uppercase) / base+52 (lowercase) / base+78 (digits live at their
         * own bases); fixed by structure, not spelled out per letter. */
        default: return NULL;
    }
}
/* decode one UTF-8 codepoint at s; returns bytes consumed (>=1) and sets *cp.
 * Invalid bytes decode to their byte value (so nothing is lost silently). */
static int lz_utf8_next(const char *s, unsigned *cp) {
    const unsigned char *u = (const unsigned char *)s;
    if (u[0] < 0x80) { *cp = u[0]; return 1; }
    if ((u[0] & 0xE0) == 0xC0 && (u[1] & 0xC0) == 0x80) {
        *cp = ((unsigned)(u[0] & 0x1F) << 6) | (u[1] & 0x3F); return 2;
    }
    if ((u[0] & 0xF0) == 0xE0 && (u[1] & 0xC0) == 0x80 && (u[2] & 0xC0) == 0x80) {
        *cp = ((unsigned)(u[0] & 0x0F) << 12) | ((unsigned)(u[1] & 0x3F) << 6) | (u[2] & 0x3F); return 3;
    }
    if ((u[0] & 0xF8) == 0xF0 && (u[1] & 0xC0) == 0x80 && (u[2] & 0xC0) == 0x80 && (u[3] & 0xC0) == 0x80) {
        *cp = ((unsigned)(u[0] & 0x07) << 18) | ((unsigned)(u[1] & 0x3F) << 12) | ((unsigned)(u[2] & 0x3F) << 6) | (u[3] & 0x3F); return 4;
    }
    *cp = u[0]; return 1;
}
static void lz_utf8_put(char *dst, int *di, int cap, unsigned cp) {
    if (cp < 0x80) { if (*di < cap - 1) dst[(*di)++] = (char)cp; }
    else if (cp < 0x800) {
        if (*di < cap - 2) { dst[(*di)++] = (char)(0xC0 | (cp >> 6)); dst[(*di)++] = (char)(0x80 | (cp & 0x3F)); }
    } else if (cp < 0x10000) {
        if (*di < cap - 3) { dst[(*di)++] = (char)(0xE0 | (cp >> 12)); dst[(*di)++] = (char)(0x80 | ((cp >> 6) & 0x3F)); dst[(*di)++] = (char)(0x80 | (cp & 0x3F)); }
    } else {
        if (*di < cap - 4) { dst[(*di)++] = (char)(0xF0 | (cp >> 18)); dst[(*di)++] = (char)(0x80 | ((cp >> 12) & 0x3F)); dst[(*di)++] = (char)(0x80 | ((cp >> 6) & 0x3F)); dst[(*di)++] = (char)(0x80 | (cp & 0x3F)); }
    }
}
/* math alphanumeric U+1D400..U+1D7FF — series are base+26 upper, +52 lower,
 * +78 digit; fold by structural offset (approximation of exact NFKC — the
 * corpus build runs real NFKC on these, this keeps the query in the same
 * space).  Runs a folding loop and must be called with the EXPAND helper. */
static int lz_math_series(unsigned cp, char *dst, int cap, int *di) {
    struct { unsigned base; unsigned hi; int n; char c0; } s[] = {
        {0x1D400, 0x1D433, 52, 'A'},  /* bold */
        {0x1D434, 0x1D467, 52, 'A'},  /* italic */
        {0x1D468, 0x1D49B, 52, 'A'},  /* bold italic */
        {0x1D49C, 0x1D4CF, 78, 'A'},  /* script + boldscript + fraktur + ... */
        {0x1D538, 0x1D56B, 78, 'A'},  /* double-struck + fraktur */
        {0x1D56C, 0x1D59F, 52, 'A'},  /* bold fraktur */
        {0x1D5A0, 0x1D5D3, 78, 'A'},  /* sans + bold sans */
        {0x1D5D4, 0x1D607, 52, 'A'},  /* sans bold */
        {0x1D608, 0x1D63B, 78, 'A'},  /* sans italic + bold italic */
        {0x1D63C, 0x1D66F, 52, 'A'},  /* sans bold italic */
        {0x1D670, 0x1D6A3, 52, 'A'},  /* monospace */
        {0x1D7CE, 0x1D7FF, 30, '0'},  /* digits: bold/dbl/sans/bsans/mono */
    };
    for (unsigned si = 0; si < sizeof(s) / sizeof(s[0]); si++) {
        if (cp >= s[si].base && cp <= s[si].hi) {
            int k = (int)(cp - s[si].base);
            char c = (s[si].c0 == '0')
                ? (char)('0' + (char)((k % 10)))
                : (char)((k < 26) ? ('A' + k) : (s[si].c0 + (char)(k - 26)));
            if (*di < cap - 1) dst[(*di)++] = c;
            return 1;
        }
    }
    return 0;
}

static int lz_sanitize(char *dst, int cap, const char *src) {
    int di = 0;
    for (const char *p = src; *p && di < cap - 8; ) {
        unsigned cp; int adv = lz_utf8_next(p, &cp); p += adv;
        if (cp == 0x0E33) {                 /* NFKC-lite: SARA AM -> NIKHAHIT + SARA AA */
            lz_utf8_put(dst, &di, cap, 0x0E4D);
            lz_utf8_put(dst, &di, cap, 0x0E32);
            continue;
        }
        const char *rep = lz_ascii_map(cp);
        if (rep) { for (const char *r = rep; *r && di < cap - 1; r++) dst[di++] = *r; continue; }
        /* math alphanumeric series: fold by structure */
        if (cp >= 0x1D400 && cp <= 0x1D7FF) { if (lz_math_series(cp, dst, cap, &di)) continue; }
        /* fullwidth forms FF01..FF5E: NFKC shifts to ASCII (exact) */
        if (cp >= 0xFF01 && cp <= 0xFF5E) { lz_utf8_put(dst, &di, cap, cp - 0xFEE0); continue; }
        /* halfwidth katakana FF61..FF9F: NFKC expands to fullwidth katakana
         * (in the corpus build), we approximate by a hole — measured 500 raw */
        if (cp >= 0xFF61 && cp <= 0xFF9F) { if (di < cap - 1) dst[di++] = ' '; continue; }
        /* ideographic space */
        if (cp == 0x3000) { if (di < cap - 1) dst[di++] = ' '; continue; }
        /* vulgar fractions + Roman numerals 2150..217F: NFKC expands — map
         * each to its ASCII form (measured all 500 raw) */
        if (cp >= 0x2150 && cp <= 0x217F) {
            static const char *vu[] = {
                "1/7","1/9","1/10","1/3","2/3","1/5","2/5","3/5","4/5","1/6","5/6",
                "1/8","3/8","5/8","7/8","1/",
                "I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","L","C","D","M",
                "i","ii","iii","iv","v","vi","vii","viii","ix","x","xi","xii","l","c","d","m",
            };
            const char *r2 = vu[cp - 0x2150];
            for (const char *r = r2; *r && di < cap - 1; r++) dst[di++] = *r;
            continue;
        }
        if (lz_is_thai_hole(cp)) continue;  /* drop: unrepresentable in this tokenizer */
        /* control/whitespace the tokenizer also 500s on (measured): \n \t \r and
         * other C0 controls -> a single space (keeps word separation, no hole). */
        if (cp < 0x20 || cp == 0x7F) { if (di < cap - 1) dst[di++] = ' '; continue; }
        lz_utf8_put(dst, &di, cap, cp);
    }
    dst[di] = '\0';
    return di;
}
/* wall ms of the most recent embed POST (kept current by BOTH lz_embed and
 * lz_embed_batch; the /v1/state/search-batch handler reads it for telemetry). */
static double g_last_embed_ms = 0.0;
/* fetch embedding for text; returns dim or -1. Sidecar: llama-server
 * /embedding with {"content": "..."} (multilingual-e5-small, 384-dim). */
static int lz_embed(const char *text, float *vec, int maxdim) {
    const char *eh = getenv("LZ_EMBED_HOST");
    const char *ep = getenv("LZ_EMBED_PORT");
    const char *host = (eh && eh[0]) ? eh : "127.0.0.1";
    int port = (ep && atoi(ep)) ? atoi(ep) : 18095;
    /* normalize the text to the tokenizer's representable space, exactly as the
     * index build does — otherwise Thai queries 500 the sidecar. */
    static char sane[4096];
    lz_sanitize(sane, (int)sizeof(sane), text);
    char body[8192];
    char esc[8000];
    lz_json_escape(esc, sizeof(esc), sane);
    snprintf(body, sizeof(body), "{\"content\":\"%s\"}", esc);
    static char resp[65536];
    int nr = lz_http_post(host, port, "/embedding", body, resp, sizeof(resp));
    if (nr <= 0) {
        fprintf(stderr, "[ann_search] embed transport fail (nr=%d) sidecar=%s:%d\n", nr, host, port);
        return -1;
    }
    if (!strstr(resp, "\"embedding\"")) {
        /* last resort (measured: Unicode-unassigned astral like U+2BFFB also
         * 500s and carries no foldable meaning): retry once with every code
         * point above the BMP dropped from the RAW text.  Losing one dead cp
         * beats returning 503 to the whole query. */
        static char noastr[4096];
        int di = 0;
        for (const char *p2 = text; *p2 && di < (int)sizeof(noastr) - 8; ) {
            unsigned cp2; int a2 = lz_utf8_next(p2, &cp2); p2 += a2;
            if (cp2 > 0xFFFF) continue;
            lz_utf8_put(noastr, &di, (int)sizeof(noastr), cp2);
        }
        noastr[di] = '\0';
        lz_sanitize(sane, (int)sizeof(sane), noastr);
        lz_json_escape(esc, sizeof(esc), sane);
        snprintf(body, sizeof(body), "{\"content\":\"%s\"}", esc);
        nr = lz_http_post(host, port, "/embedding", body, resp, sizeof(resp));
        if (nr > 0 && strstr(resp, "\"embedding\"")) {
            fprintf(stderr, "[ann_search] embed retried after dropping astral (raw cp lost ok)\n");
        }
    }
    if (!strstr(resp, "\"embedding\"")) {
        /* the sidecar answered but not with an embedding — usually a 500 from
         * the tokenizer. Log the status line + a slice so the cause is visible
         * instead of a bare 503 "embed_unavailable". */
        char line[256]; int li = 0;
        for (const char *q = resp; *q && *q != '\r' && *q != '\n' && li < 255; q++) line[li++] = *q;
        line[li] = '\0';
        fprintf(stderr, "[ann_search] embed rejected by sidecar: %.200s\n", line);
        return -1;
    }
    /* anchor on the "embedding" key, NOT the first '[': the sidecar's batch
     * shape is [{"index":0,"embedding":[[...]]}] — a first-'[' parse lands on
     * the OUTER array and counts the '{' as dim 0 (measured: qd=385, route_ok
     * 384!=385 -> brute, plus a one-slot shift in the query vector). */
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

/* ── batch embed (B<=32, additive): ONE POST to :18095 /v1/embeddings ──
 * std OpenAI shape {"input":[...]}; response {"data":[{"embedding":[..]},..]}.
 * Fails CLOSED: *ok=0 -> caller falls back to per-string lz_embed. */
static float *lz_embed_batch(const char * const *qs, int nq, float *out,
                             int *out_dims, int maxdim, int *ok) {
    *ok = 0;
    if (!qs || nq <= 0 || nq > 32 || !out || !out_dims) return NULL;
    const char *eh = getenv("LZ_EMBED_HOST");
    const char *ep = getenv("LZ_EMBED_PORT");
    const char *host = (eh && eh[0]) ? eh : "127.0.0.1";
    int port = (ep && atoi(ep)) ? atoi(ep) : 18095;
    static char ebody[60000];
    int ebo = snprintf(ebody, sizeof(ebody), "{\"input\":[");
    static char sane[2048];
    static char esc[2048];
    for (int qi = 0; qi < nq; qi++) {
        lz_sanitize(sane, (int)sizeof(sane), qs[qi]);
        lz_json_escape(esc, sizeof(esc), sane);
        ebo += snprintf(ebody + ebo, sizeof(ebody) - ebo,
            "%s\"%s\"", qi ? "," : "", esc);
        if (ebo < 0 || ebo >= (int)sizeof(ebody) - 8) return NULL;
    }
    ebo += snprintf(ebody + ebo, sizeof(ebody) - ebo, "]}");
    LARGE_INTEGER qf, t0, t1;
    QueryPerformanceFrequency(&qf);
    QueryPerformanceCounter(&t0);
    static char resp[65536];
    int nr = lz_http_post(host, port, "/v1/embeddings", ebody, resp, sizeof(resp));
    QueryPerformanceCounter(&t1);
    g_last_embed_ms = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)qf.QuadPart;
    const char *dp = (nr > 0) ? strstr(resp, "\"data\"") : NULL;
    if (nr <= 0 || !dp) return NULL;
    int got = 0;
    dp = strchr(dp, '[');
    for (int qi = 0; qi < nq; qi++) {
        dp = (dp && *dp) ? strchr(dp, '[') : NULL;
        if (!dp) break;
        dp++;
        int nd = 0;
        while (*dp && *dp != ']' && nd < maxdim) {
            while (*dp == ' ' || *dp == ',') dp++;
            if (*dp == ']' || !*dp) break;
            out[(size_t)qi * maxdim + nd] = (float)atof(dp);
            nd++;
            while (*dp && *dp != ',' && *dp != ']') dp++;
        }
        if (dp && *dp == ']') dp++;
        out_dims[qi] = nd;
        if (nd > 0) got++;
    }
    if (got != nq) return NULL;
    *ok = 1;
    return out;
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
#define LZ_SEARCH_BUDGET_DEF 24000
/* Recall knee is corpus-size-relative (measured 2026-10-11, 300q x2 corpora
 * vs a numpy oracle: memcore knee ~0.40*N, sample ~0.69*N), so a FIXED default
 * under-serves a growing corpus. budget = min(N, max(FLOOR, frac*N)) — the
 * floor keeps the default identical for N <= FLOOR/frac (= 60k), above which
 * it scales. Explicit LZ_SEARCH_BUDGET still overrides both. */
#define LZ_SEARCH_BUDGET_FRAC_DEF 0.40
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
static int g_graft_buckets = 0;            /* ANN_GRAFT_BUCKETS: add graph-endorsed buckets to the anchor route */

static unsigned _fnv(const char *s);
static const char *regrow_state_path(void);
static void lz_diag(const char *fmt, ...);
static void lz_tags_ensure(LZEntry *ents, int nent);
static void lz_tags_free(void);
static void lz_tags_load(LZEntry *ents, int nent);
static int  lz_stamp_overlay(LZEntry *a, int n);
static int  lz_stamp_prefix_ok(const LZEntry *a, int n);
static void lz_index_free(LZEntry *a, int n);
static CRITICAL_SECTION g_lock;
/* The request lock is RECURSIVE (a Windows critical section), so the depth
 * has to be tracked per thread: the regrow releases it around its heavy
 * work and takes it back afterwards. */
static __thread int g_lock_depth = 0;
static void lz_lock_get(void) { EnterCriticalSection(&g_lock); g_lock_depth++; }
static void lz_lock_put(void) { if (g_lock_depth > 0) g_lock_depth--; LeaveCriticalSection(&g_lock); }

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
/* rows carrying a live routing stamp (set by lz_index_load; 0 = none).
 * Declared before lz_index_load (which writes it) and before handle_health
 * (which reads it). */
static int g_stamp_live = 0;
/* the stamp sidecar's identity (the background loader watches it: the
 * incremental regrow writes ONLY the sidecar, so the index file can stay
 * untouched while the routing table changes) */
static long g_stamp_mtime = 0, g_stamp_size = 0;
/* a regrow is running: the background loader waits instead of competing for
 * the same two cores (measured 2026-10-22: the regrow + the loader together
 * starved the sidecar and the health probe) */
static volatile LONG g_regrow_busy = 0;
static long g_stamp_state_mtime = 0;   /* the regrow state file's identity */
/* the background loader: the index is reloaded OFF the request path so a
 * rebuild never blocks a query (measured 2026-10-21: the inline reload of
 * 258 MB / 62,634 rows held the lock for >60 s and /health timed out) */
static volatile LONG g_loader_stop = 0;
static int g_loader_running = 0;
static volatile LONG g_loader_parsing = 0;  /* 1 = the loader is mid-parse */
static HANDLE g_loader_thread = NULL;
static double g_loaded_at_ms = 0;

/* DEAD ROUTE (2026-10-20): the anchor table and the order file are valid
 * (so the route layer says "routed") but the ROWS carry almost no routing
 * stamps. The pruned walk then scores only the handful of stamped rows and
 * serves them as the top-k — measured on live :8096: items_scored 4 of
 * 62,204 while /health said routed=1. The stamps die when the index is
 * rebuilt by scripts/build_index.py (rows written without the anchor
 * fields) and the incremental regrow copies that dead state into the
 * sidecar.
 * The threshold is a FRACTION, not "== 0": the live incident left 23 live
 * rows of 62,227, so an exact-zero test would have missed it. Half the
 * corpus is the fail-loud line; a healthy index has (almost) every row
 * stamped. Rows can legitimately be unstamped (a dim mismatch), so the
 * bar is deliberately low. */
#define STAMP_LIVE_MIN_FRAC 0.5
static inline int stamps_route_dead(int nent) {
    if (nent <= 0) return 0;
    if (g_stamp_live <= 0) return 1;
    return ((double)g_stamp_live < (double)nent * STAMP_LIVE_MIN_FRAC);
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
/* STAMP SIDECAR OVERLAY (2026-10-20): rows' CURRENT anchor/anchor2 live in
 * <PATH_INDEX>.stamp (8 bytes/row: anchor,anchor2 int32 pair). The sidecar
 * is the TREE's channel and it is authoritative for EVERY ROW IT COVERS —
 * not only when its size happens to equal n*8. Ignoring a short sidecar
 * throws away the whole routing table (measured 2026-10-20: the append
 * window between ann-sync's append and its regrow left stamps_live 0 of
 * 62,323 — the tree's labels were on disk but unread).
 * Contract:
 *   - the sidecar covers rows [0, sidecar_rows)
 *   - rows beyond that are NOT PLACED yet -> they keep the fruit's label (-1)
 *   - a sidecar LONGER than the index is truncated to n rows
 * Returns the number of rows that carry a live stamp. */
static int lz_stamp_overlay(LZEntry *a, int n) {
    char sp[600];
    snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
    struct stat ss;
    g_stamp_mtime = 0; g_stamp_size = 0;
    if (stat(sp, &ss) == 0) {
        g_stamp_mtime = (long)ss.st_mtime;
        g_stamp_size = (long)ss.st_size;
        /* THE GATE (2026-10-22): the sidecar is POSITIONAL, so its labels are
         * only valid for a buffer whose prefix is PROVEN to be the same rows.
         * The proof is the regrow's own append contract: the state file
         * records (n_prev, last_sid) = the count and the last sid of the
         * index the sidecar was computed for, so a[st.n_prev-1].sid ==
         * st.last_sid proves the prefix is intact. Without the gate, a
         * sidecar from another index version would be laid over the wrong
         * rows and the route would silently degrade (the S9 lesson). */
        if (!lz_stamp_prefix_ok(a, n)) {
            int live = 0;
            for (int i = 0; i < n; i++) if (a[i].anchor >= 0) live++;
            return live;
        }
        if (ss.st_size >= 8) {
            long side_rows = (long)ss.st_size / 8;
            if (side_rows > (long)n) side_rows = (long)n;
            FILE *sf = fopen(sp, "rb");
            if (sf) {
                for (int i = 0; i < (int)side_rows; i++) {
                    int32_t rec[2];
                    if (fread(rec, sizeof(rec), 1, sf) != 1) break;
                    a[i].anchor = (int)rec[0];
                    a[i].anchor2 = (int)rec[1];
                }
                fclose(sf);
            }
        }
    }
    int live = 0;
    for (int i = 0; i < n; i++)
        if (a[i].anchor >= 0) live++;
    return live;
}

static int lz_index_load(LZEntry **out, int *n_out, int *stamp_live_out) {
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
    /* STAMP SIDECAR OVERLAY (2026-10-20): rows' CURRENT anchor/anchor2 live
     * in <PATH_INDEX>.stamp (8 bytes/row: n,anchor2 int32 pair) when it
     * exists — the jsonl stamps may be stale after a perm-sidecar regrow
     * (which no longer rewrites the jsonl). Overlay the sidecar when it
     * covers exactly the loaded row count (records[i] i<n). A stale or
     * partial sidecar is IGNORED silently (jsonl stamps stay authoritative),
     * which is the same contract as before the sidecar existed. */
    /* the stamp overlay is applied by lz_stamp_overlay (it can also run on
     * its own when only the sidecar changed) */
    if (stamp_live_out) *stamp_live_out = lz_stamp_overlay(a, n);
    *out = a; *n_out = n;
    return 0;
}
/* ── FLOAT-KERNEL parallel arrays (ANN_FLOAT_KERNEL=1) ─────────────── */
static float *g_inv = NULL;
static int g_inv_n = 0;
static int g_float_kernel = 0;
static void lazy_inv_build(LZEntry *ents, int nent) {
    if (g_inv && g_inv_n == nent) return;
    free(g_inv); g_inv = NULL; g_inv_n = 0;
    g_inv = (float *)malloc((size_t)(nent > 0 ? nent : 1) * sizeof(float));
    if (!g_inv) return;
    for (int i = 0; i < nent; i++)
        g_inv[i] = (ents[i].norm > 0) ? (float)(1.0 / ents[i].norm) : 0.0f;
    g_inv_n = nent;
}
static void lz_index_free(LZEntry *a, int n) {
    if (!a) return;
    for (int i = 0; i < n; i++) free(a[i].vec);
    free(a);
    free(g_inv);
    g_inv = NULL;
    g_inv_n = 0;
    /* the TAG state is NOT freed here: it belongs to the CURRENT buffer, and
     * the swap reloads it explicitly (lz_tags_load). Freeing it here would
     * drop the labels of the buffer that is already live. */
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
            /* the tag sidecar can be re-baked without the index changing, so
             * it is checked on EVERY use, not only on an index reload */
            lz_tags_ensure(g_idx_cache, g_idx_n);
            *out = g_idx_cache; *n_out = g_idx_n; return 1;
        }
        /* the file moved but the background loader has not swapped yet: serve
         * the LOADED buffer (a stale index beats a 60 s stall) */
        if (g_loader_running && g_idx_cache) {
            *out = g_idx_cache; *n_out = g_idx_n; return 1;
        }
        lz_diag("cached_fallback ts=%lld loader=%d cache=%p\n",
                (long long)time(NULL), g_loader_running, (void *)g_idx_cache);
        lz_index_cache_invalidate();
        int sl = 0;
        if (lz_index_load(&g_idx_cache, &g_idx_n, &sl) == 0 && g_idx_cache) {
            g_idx_mtime = mt; g_idx_size = sz;
            g_stamp_live = sl;
            lz_tags_load(g_idx_cache, g_idx_n);
            *out = g_idx_cache; *n_out = g_idx_n; return 1;
        }
    }
    *out = NULL; *n_out = 0;
    return 0;
}

/* ── background index loader (2026-10-21) ───────────────────────────────
 * The index is 258 MB of JSONL: parsing it takes ~60 s on this box. Doing
 * that inside a request (the old inline reload) held the request lock the
 * whole time, so EVERY request — /health included — queued behind it
 * (measured live 2026-10-21: /health did not answer within 60 s while
 * ann_sync rebuilt the index).
 *
 * The loader owns the swap: it parses the new file into its OWN buffer
 * (no lock held), then swaps the pointer under the lock and frees the old
 * buffer. A query therefore always sees a complete index, and a rebuild
 * costs the service nothing.
 *
 * It also watches the STAMP SIDECAR on its own: the incremental regrow
 * writes only the sidecar (the fruit is never rewritten), so the routing
 * table can change while the index file does not. Before this, such a
 * change was invisible until the next index rebuild. */
/* a diagnostic log that cannot be lost (the stderr of a detached server is
 * not always captured by the supervisor) */
static void lz_diag(const char *fmt, ...) {
    char path[600];
    snprintf(path, sizeof(path), "%s.loader.log", PATH_INDEX);
    FILE *f = fopen(path, "a");
    if (!f) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fclose(f);
}

static double lz_now_ms(void) {
    LARGE_INTEGER qf, qt;
    QueryPerformanceFrequency(&qf);
    QueryPerformanceCounter(&qt);
    return (double)qt.QuadPart * 1000.0 / (double)qf.QuadPart;
}

/* ── regrow-side cache borrow (2026-10-22: regrow ไม่ parse เอง) ───────────
 * The incremental/full regrow used to call lz_index_load() itself — ~50 s on
 * a 258 MB jsonl, a pure duplicate of the parse the background loader had
 * already done into g_idx_cache. This borrows that buffer when it is still
 * in sync with the file on disk.
 *
 * Ownership: the buffer stays owned by the loader. The regrow READS it only
 * (lz_anchor_refresh never writes a[i].anchor/.anchor2 — verified 2026-10-22;
 * since 9c532e8 the fresh stamps go to the sidecar, not into the rows) and
 * must NOT free it.
 *
 * What holds the pointer steady for the whole regrow:
 *   - the caller runs with g_regrow_busy set, and the loader thread refuses
 *     to parse/swap while it is set, so it cannot free the buffer underneath
 *     us, and
 *   - the request lock is released for the duration of the regrow, so this
 *     short critical section is the only g_lock taker on the regrow side.
 *
 * Returns 1 when borrowed (*out / *n_out set); 0 when the caller must parse
 * on its own (no cache yet, or the file's mtime/size moved under it).
 */
static int lz_index_borrow(LZEntry **out, int *n_out) {
    *out = NULL; *n_out = 0;
    struct stat sti;
    if (stat(PATH_INDEX, &sti) != 0) {
        lz_diag("idx_borrow miss stat-fail ts=%lld\n", (long long)time(NULL));
        return 0;
    }
    long mt = (long)sti.st_mtime, sz = (long)sti.st_size;
    int ok = 0;
    EnterCriticalSection(&g_lock);
    if (g_idx_cache && g_idx_n > 0 && mt == g_idx_mtime && sz == g_idx_size) {
        *out = g_idx_cache;
        *n_out = g_idx_n;
        ok = 1;
    }
    LeaveCriticalSection(&g_lock);
    /* WAIT for the loader's in-flight parse (2026-10-22). If the loader is
     * already loading this exact file version, parsing it again here is a
     * wasted duplicate (measured live: idx_borrow miss -> loader_swap_lock,
     * both then parsed the same 62,752-row version — 52 s + 52 s back to
     * back). So when the borrow misses AND the loader is mid-parse, poll
     * for the matching buffer to appear; the loader's next swap ends the
     * wait regardless of who loaded it. Bound: a 258 MB parse takes ~50 s
     * on this box, 300 s covers slower disks; if the loader's parse ends
     * without a match (a stat race) the caller still self-parses. */
    for (int wi = 0; !ok && wi < 600 && g_loader_parsing; wi++) {
        Sleep(500);
        struct stat st2;
        if (stat(PATH_INDEX, &st2) == 0) { mt = (long)st2.st_mtime; sz = (long)st2.st_size; }
        EnterCriticalSection(&g_lock);
        if (g_idx_cache && g_idx_n > 0 && mt == g_idx_mtime && sz == g_idx_size) {
            *out = g_idx_cache;
            *n_out = g_idx_n;
            ok = 1;
        }
        LeaveCriticalSection(&g_lock);
    }
    lz_diag("idx_borrow %s ts=%lld n=%d\n", ok ? "hit" : "miss",
            (long long)time(NULL), *n_out);
    return ok;
}

static DWORD WINAPI index_loader_thread(LPVOID arg) {
    (void)arg;
    /* LOW PRIORITY (2026-10-22): the box has 2 cores and the loader's parse
     * uses one of them for ~50 s. At NORMAL priority the request threads
     * were starved (measured: /health waited 70 s for a lock it did not
     * even hold — the wait was CPU starvation, not contention) while the
     * regrow's kmeans and the embed sidecar competed for the rest. Below
     * normal, the service keeps answering and the reload still finishes. */
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
    lz_diag("loader_thread tid=%lu\n", (unsigned long)GetCurrentThreadId());
    for (;;) {
        if (g_loader_stop) break;
        Sleep(1500);
        struct stat st;
        if (stat(PATH_INDEX, &st) == 0) {
            long mt = (long)st.st_mtime, sz = (long)st.st_size;
            if ((!g_idx_cache || mt != g_idx_mtime || sz != g_idx_size) &&
                !g_regrow_busy) {
                LZEntry *na = NULL;
                int nn = 0, nsl = 0;
                lz_diag("loader_parse_start ts=%lld why=%s mt=%ld/%ld sz=%ld/%ld\n",
                        (long long)time(NULL),
                        !g_idx_cache ? "cache_null" :
                        (mt != g_idx_mtime) ? "mtime_diff" : "size_diff",
                        mt, g_idx_mtime, sz, g_idx_size);
                InterlockedExchange(&g_loader_parsing, 1);
                if (lz_index_load(&na, &nn, &nsl) == 0 && na) {
                    lz_diag("loader_swap_lock ts=%lld\n", (long long)time(NULL));
                    EnterCriticalSection(&g_lock);
                    LZEntry *old = g_idx_cache;
                    int oldn = g_idx_n;
                    g_idx_cache = na; g_idx_n = nn;
                    g_idx_mtime = mt; g_idx_size = sz;
                    g_stamp_live = nsl;
                    lz_tags_load(na, nn);      /* the labels follow the rows */
                    g_loaded_at_ms = lz_now_ms();
                    if (old) lz_index_free(old, oldn);  /* still under the lock */
                    LeaveCriticalSection(&g_lock);
                    lz_diag("reload ts=%lld entries=%d mtime=%ld size=%ld\n",
                            (long long)time(NULL), nn, mt, sz);
                    fprintf(stderr,
                            "[ann_search] index reloaded in the background: %d entries\n",
                            nn);
                }
                InterlockedExchange(&g_loader_parsing, 0);
            }
        }
        /* the stamp sidecar can change WITHOUT the index changing (the
         * incremental regrow writes only the sidecar), and the regrow state
         * file decides whether those labels apply to the loaded buffer */
        char sp[600];
        snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
        struct stat ss;
        long smt = 0, ssz = 0;
        if (stat(sp, &ss) == 0) { smt = (long)ss.st_mtime; ssz = (long)ss.st_size; }
        long stm = 0;
        { struct stat ts; if (stat(regrow_state_path(), &ts) == 0) stm = (long)ts.st_mtime; }
        if ((smt != g_stamp_mtime || ssz != g_stamp_size) || stm != g_stamp_state_mtime) {
            EnterCriticalSection(&g_lock);
            if (g_idx_cache) g_stamp_live = lz_stamp_overlay(g_idx_cache, g_idx_n);
            g_stamp_state_mtime = stm;
            LeaveCriticalSection(&g_lock);
        }
    }
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
    /* H2: the pair is bound — order must carry this table's K/ntr/checksum
     * (headerless, re-anchored, or torn files refuse here, not in the walk). */
    if (ord_load(PATH_ORDER, ord, nent, K, anch_cksum(C, K, adim)) != 0) {
        free(C); free(ord); return 0;
    }
    g_anch_cache = C; g_anch_k = K; g_anch_dim = adim; g_anch_ntr = antr;
    g_ord_cache = ord; g_ord_n = nent;
    for (int i = 0; i < 4; i++) g_anch_key[i] = key[i];
    return 1;
}

/* ── dot product (H -> G: 4 independent accumulator chains) ──────────
 * A scalar `dot += a[j]*b[j]` is one serial dependency chain — 4-cycle add
 * latency on this G4400, so 384 dims cost ~1500 cycles before any throughput.
 * Four chains let the adds overlap. Still double, so same precision as before
 * and no unit-norm precondition; but reassociating the sum is not
 * bit-identical (max_rel_diff 2.5e-12). Speedup and the alternatives not
 * taken: RESULTS-ROUTE-COST-2026-10-19.md (variants H and G). */
/* ── float kernel (env ANN_FLOAT_KERNEL=1, default OFF = old double path) ─
 * Same 4-chain shape as lz_dot, but with float mul/adds — for use with
 * reciprocal normalizers so no div shows up in the hot loop. NOT
 * bit-identical to the double path: float accumulation + reassociation
 * change rounding (see microbench; measured max_rel_diff < 1e-5). */
static inline float lz_dotf(const float *a, const float *b, int d) {
    float s0 = 0.0f, s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    int j = 0;
    for (; j + 4 <= d; j += 4) {
        s0 += a[j]     * b[j];
        s1 += a[j + 1] * b[j + 1];
        s2 += a[j + 2] * b[j + 2];
        s3 += a[j + 3] * b[j + 3];
    }
    float s = (s0 + s1) + (s2 + s3);
    for (; j < d; j++) s += a[j] * b[j];
    return s;
}
static inline double lz_dot(const float *a, const float *b, int d) {
    double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    int j = 0;
    for (; j + 4 <= d; j += 4) {
        s0 += (double)a[j]     * (double)b[j];
        s1 += (double)a[j + 1] * (double)b[j + 1];
        s2 += (double)a[j + 2] * (double)b[j + 2];
        s3 += (double)a[j + 3] * (double)b[j + 3];
    }
    double s = (s0 + s1) + (s2 + s3);
    for (; j < d; j++) s += (double)a[j] * (double)b[j];
    return s;
}

/* ── tag scope ──────────────────────────────────────────────────────────
 * A row's KIND is its file tag up to the first '/' ("exchange/xyz" ->
 * "exchange"). The scope is the request's "scope" field: either a kind
 * ("exchange") or a file ("chatlog/20261021_110617_2d9008"). */
static int lz_is_kind(const char *file, const char *kind) {
    size_t kl = strlen(kind);
    if (strncmp(file, kind, kl) != 0) return 0;
    return file[kl] == '/' || file[kl] == '\0';
}

/* ── TAG SIDECAR (2026-10-21) ────────────────────────────────────────────
 * scripts/tag_lut.py bakes a FROZEN tag->zone LUT over the index and writes
 * a SID-KEYED sidecar "<sid>\t<tag>\t<zone>". Sid-keyed, never positional:
 * a positional sidecar cannot be verified against the index it labels and
 * drifts silently (the S9 lesson from 2026-10-20).
 *
 * The tag is the semantic zone (memcore's `pattern`: chatpool, hermes,
 * architecture, project_rules, constraints, ...) which SPANS sessions — the
 * `file` tag (kind/term_hint) cannot express it, because one session writes
 * into many files and one file collects many sessions.
 *
 * A scoped query can therefore ask for a ZONE:
 *   scope="tag:architecture"  -> the rows whose tag is architecture
 *   scope="zone:3"            -> the rows of zone 3 (the frozen coordinate)
 * Both bypass the route layer exactly like the file scope does: the group is
 * derived from the rows themselves, so it cannot drift from the index. */
#define LZ_MAX_TAGS 64
static char g_tag_name[LZ_MAX_TAGS][64];
static int  g_tag_n = 0;
static int *g_ent_tag = NULL;      /* [nent] tag id per row, -1 = unlabeled */
static int *g_ent_zone = NULL;     /* [nent] zone id per row, -1 = none */
static int  g_ent_tag_n = 0;
static int  g_tag_unlabeled = 0;
static int *g_tag_sid_hash = NULL; /* sid -> row index (open addressing) */
static int  g_tag_sid_cap = 0;
static LZEntry *g_tag_ents_ref = NULL;
static char g_tag_path[600] = "";
static long g_tag_mtime = 0;
static long g_tag_size = 0;

static int lz_tag_intern(const char *name) {
    for (int i = 0; i < g_tag_n; i++)
        if (strcmp(g_tag_name[i], name) == 0) return i;
    if (g_tag_n >= LZ_MAX_TAGS) return -1;
    snprintf(g_tag_name[g_tag_n], sizeof(g_tag_name[0]), "%s", name);
    return g_tag_n++;
}

static void lz_tag_build_sid_hash(LZEntry *ents, int nent) {
    free(g_tag_sid_hash); g_tag_sid_hash = NULL; g_tag_sid_cap = 0;
    g_tag_ents_ref = ents;
    int cap = 1; while (cap < (nent ? nent : 1) * 2) cap <<= 1;
    g_tag_sid_cap = cap;
    g_tag_sid_hash = (int *)malloc((size_t)cap * sizeof(int));
    for (int i = 0; i < cap; i++) g_tag_sid_hash[i] = -1;
    for (int e = 0; e < nent; e++) {
        unsigned m = (unsigned)cap - 1, i = _fnv(ents[e].sid) & m;
        for (unsigned k = 0; k < (unsigned)cap; k++) {
            unsigned s = (i + k) & m;
            if (g_tag_sid_hash[s] < 0) { g_tag_sid_hash[s] = e; break; }
        }
    }
}

static int lz_tag_sid_pos(const char *sid) {
    if (!g_tag_sid_hash || !g_tag_ents_ref) return -1;
    unsigned m = (unsigned)g_tag_sid_cap - 1, i = _fnv(sid) & m;
    for (unsigned k = 0; k < (unsigned)g_tag_sid_cap; k++) {
        unsigned s = (i + k) & m;
        if (g_tag_sid_hash[s] < 0) return -1;
        if (strcmp(g_tag_ents_ref[g_tag_sid_hash[s]].sid, sid) == 0)
            return g_tag_sid_hash[s];
    }
    return -1;
}

static void lz_tags_free(void) {
    free(g_ent_tag); g_ent_tag = NULL;
    free(g_ent_zone); g_ent_zone = NULL;
    g_ent_tag_n = 0;
    free(g_tag_sid_hash); g_tag_sid_hash = NULL; g_tag_sid_cap = 0;
    g_tag_ents_ref = NULL;
    g_tag_unlabeled = 0;
}

/* the sidecar path: env ANN_TAGS_PATH, else next to the index it labels */
static void lz_tag_path(char *out, size_t cap) {
    const char *tp = getenv("ANN_TAGS_PATH");
    if (tp && tp[0]) { snprintf(out, cap, "%s", tp); return; }
    const char *slash = strrchr(PATH_INDEX, '/');
    const char *bslash = strrchr(PATH_INDEX, '\\');
    const char *cut = (slash && (!bslash || slash > bslash)) ? slash : bslash;
    if (cut) snprintf(out, cap, "%.*sann_tags.tsv", (int)(cut - PATH_INDEX + 1), PATH_INDEX);
    else snprintf(out, cap, "ann_tags.tsv");
}

static void lz_tags_load(LZEntry *ents, int nent) {
    lz_tags_free();
    g_ent_tag_n = nent;
    g_ent_tag = (int *)malloc((size_t)(nent ? nent : 1) * sizeof(int));
    g_ent_zone = (int *)malloc((size_t)(nent ? nent : 1) * sizeof(int));
    for (int i = 0; i < nent; i++) { g_ent_tag[i] = -1; g_ent_zone[i] = -1; }
    char path[600];
    lz_tag_path(path, sizeof(path));
    snprintf(g_tag_path, sizeof(g_tag_path), "%s", path);
    struct stat ss;
    g_tag_mtime = (stat(path, &ss) == 0) ? (long)ss.st_mtime : 0;
    g_tag_size = (stat(path, &ss) == 0) ? (long)ss.st_size : 0;
    FILE *f = fopen(path, "r");
    if (!f) { g_tag_unlabeled = nent; return; }
    lz_tag_build_sid_hash(ents, nent);
    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        *tab = '\0';
        char *tag = tab + 1;
        char *tab2 = strchr(tag, '\t');
        int zone = -1;
        if (tab2) { *tab2 = '\0'; zone = atoi(tab2 + 1); }
        size_t tl = strlen(tag);
        while (tl && (tag[tl - 1] == '\n' || tag[tl - 1] == '\r')) tag[--tl] = '\0';
        if (!tag[0]) continue;
        int pos = lz_tag_sid_pos(line);
        if (pos < 0) continue;
        int tid = lz_tag_intern(tag);
        if (tid < 0) continue;
        g_ent_tag[pos] = tid;
        g_ent_zone[pos] = zone;
    }
    fclose(f);
    g_tag_unlabeled = 0;
    for (int i = 0; i < nent; i++) if (g_ent_tag[i] < 0) g_tag_unlabeled++;
}

/* reload only when the sidecar or the index actually moved */
static void lz_tags_ensure(LZEntry *ents, int nent) {
    if (g_ent_tag && g_ent_tag_n == nent && g_tag_ents_ref == ents) {
        char path[600];
        lz_tag_path(path, sizeof(path));
        struct stat ss;
        if (stat(path, &ss) == 0 &&
            (long)ss.st_mtime == g_tag_mtime && (long)ss.st_size == g_tag_size &&
            strcmp(path, g_tag_path) == 0)
            return;
    }
    lz_tags_load(ents, nent);
}

/* Resolve a scope against the loaded rows into the pattern the walk will
 * match: 1 = exact file, 2 = kind. Falls from file to kind when the file
 * group cannot hold topk rows (the chain measured in bench_tag_filter.py).
 * Returns 0 when the scope matches nothing (handler answers 400
 * scope_not_found) and -1 when it matches fewer than topk rows in either
 * grouping (400 scope_too_narrow) — a narrow scope is never silently
 * widened past kind. */
static int lz_scope_resolve(const LZEntry *ents, int nent, const char *scope,
                            int topk, char *pat, size_t pcap,
                            int *fine_out, int *kind_out,
                            int *tag_out, int *zone_out) {
    if (tag_out) *tag_out = -1;
    if (zone_out) *zone_out = -1;
    /* kbuf/spat must hold the WHOLE tag in BYTES: a Thai file tag of 57
     * chars is ~127 bytes, and a 64-byte pattern truncated mid-codepoint
     * matched no row at all — the walk then got an empty array with the
     * index length as its bound (measured: mode "anchor", items_scored 1,
     * budget 0, one out-of-scope hit). Scope strings are <= 255 bytes. */
    char kbuf[256];
    int fine = 0, nk = 0;
    /* ZONE scope: "tag:<name>" (the semantic tag) or "zone:<id>" (the frozen
     * coordinate from the same bake). A zone is NEVER widened: it is a
     * closed group, so a zone that cannot hold topk rows is a 400, not a
     * silently broader answer. */
    if (strncmp(scope, "tag:", 4) == 0) {
        const char *nm = scope + 4;
        int tid = -1;
        for (int i = 0; i < g_tag_n; i++)
            if (strcmp(g_tag_name[i], nm) == 0) { tid = i; break; }
        int cnt = 0;
        for (int i = 0; i < nent; i++) if (g_ent_tag[i] == tid) cnt++;
        if (fine_out) *fine_out = cnt;
        if (kind_out) *kind_out = 0;
        if (tid < 0 || cnt == 0) { if (pcap) pat[0] = '\0'; return 0; }
        if (cnt < topk) return -1;
        if (tag_out) *tag_out = tid;
        if (zone_out) *zone_out = -1;
        if (pcap) snprintf(pat, pcap, "%s", nm);
        return 3;
    }
    if (strncmp(scope, "zone:", 5) == 0 && scope[5] >= '0' && scope[5] <= '9') {
        int z = atoi(scope + 5);
        int cnt = 0;
        for (int i = 0; i < nent; i++) if (g_ent_zone[i] == z) cnt++;
        if (fine_out) *fine_out = cnt;
        if (kind_out) *kind_out = 0;
        if (cnt == 0) { if (pcap) pat[0] = '\0'; return 0; }
        if (cnt < topk) return -1;
        if (tag_out) *tag_out = -1;
        if (zone_out) *zone_out = z;
        if (pcap) snprintf(pat, pcap, "%d", z);
        return 4;
    }
    const char *slash = strchr(scope, '/');
    int kl = slash ? (int)(slash - scope) : (int)strlen(scope);
    if (kl < 0) kl = 0;
    if (kl > (int)sizeof(kbuf) - 1) kl = (int)sizeof(kbuf) - 1;
    memcpy(kbuf, scope, (size_t)kl);
    kbuf[kl] = '\0';
    for (int i = 0; i < nent; i++) {
        if (strcmp(ents[i].file, scope) == 0) fine++;
        if (lz_is_kind(ents[i].file, kbuf)) nk++;
    }
    if (fine_out) *fine_out = fine;
    if (kind_out) *kind_out = nk;
    if (fine == 0 && nk == 0) { if (pcap) pat[0] = '\0'; return 0; }
    if (!slash) {                       /* bare scope = a kind request */
        snprintf(pat, pcap, "%s", kbuf);
        return nk >= topk ? 2 : (fine >= topk ? 1 : -1);
    }
    if (fine >= topk) { snprintf(pat, pcap, "%s", scope); return 1; }
    if (nk >= topk)   { snprintf(pat, pcap, "%s", kbuf); return 2; }
    return -1;
}

/* ── lz-mem-search core (from L435-586, topk bound -> ANN_MAXK) ──
 * Returns hit count 0..topk, or -1 when the query is unusable.
 * `scope` (may be NULL) hard-restricts the walk to the rows sharing one tag
 * — see lz_scope_resolve; an empty walk is served as mode "scope-miss". */
static int lz_mem_search(const float *qv, int qd,
                         const char *sid_filter, const char *scope, int topk,
                         char top_sid[][64], char top_file[][256],
                         int top_np[], double top_sc[],
                         char route_mode[16], int route_bk[], int *route_nb_out,
                         int *block_reads_out, int *items_scored_out, int *nent_out,
                         int *budget_out, int *truncated_out, double *us_per_item_out,
                         LZFog *fog_out, int *fallback_out) {
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
    /* FLOAT-KERNEL per-query reciprocal (one division per QUERY instead of
     * one per entry). Local stack — thread-safe without a lock. */
    float qinv_local = (qn > 0.0) ? (float)(1.0 / qn) : 0.0f;
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
    int fallback = 0;
    int scope_n = 0;      /* rows in the scoped walk; 0 = no scope (param "scope") */
    int scope_bad = 0;    /* scope given but unresolvable now -> scope-miss */
    int budget = LZ_SEARCH_BUDGET_DEF;
    int cap = 0;
    /* Budget resolution, in order:
     *   1. env LZ_SEARCH_BUDGET (>0) wins outright.
     *   2. else min(N, max(FLOOR, frac*N)): the floor keeps the historical
     *      24000 for N <= 60000 (today's corpora are 2881 / 59756, so nothing
     *      changes there), and the corpus scales past that so a growing index
     *      is not silently under-served by a fixed number. frac via
     *      LZ_SEARCH_BUDGET_FRAC (default 0.40 = the measured memcore knee).
     *   3. NO silent widen-to-full-scan: that trap (removed 2026-10-11) made a
     *      default larger than N unreachable and reported truncated=0 forever.
     * Recall is steep in budget (measured 2026-10-11, 300 real-vec queries x2
     * corpora vs numpy oracle, pruned top-5): memcore R@10 0.51 @8k, 0.88 @16k,
     * 0.96 @24k (0.40*N), 0.998 @full; sample R@10 0.61 @500, 0.94 @1000,
     * 0.995 @2000 (0.69*N). */
    { const char *be = getenv("LZ_SEARCH_BUDGET");
      if (be && atoi(be) > 0) {
          budget = atoi(be);
      } else {
          double frac = LZ_SEARCH_BUDGET_FRAC_DEF;
          { const char *fe = getenv("LZ_SEARCH_BUDGET_FRAC");
            if (fe && atof(fe) > 0.0 && atof(fe) <= 1.0) frac = atof(fe); }
          long scaled = (long)(frac * (double)nent + 0.5);
          if (scaled < LZ_SEARCH_BUDGET_DEF) scaled = LZ_SEARCH_BUDGET_DEF;
          if (nent > 0 && scaled > nent) scaled = nent;
          budget = (int)scaled;
      } }
    /* ── bucket-restricted walk (env ANN_WALK_PRUNE=1) ──────────────────
     * The default walk STEPS over all nent entries in placement order and
     * only counts scoring toward the budget, so the table's bucket choice
     * cannot change the scored set (measured: 4 seed tables, buckets differ
     * 30/30, hits identical 30/30). With PRUNE=1 the walk visits ONLY bucket
     * members (one O(nent) gather of cheap integer compares, then cosine on
     * members alone) — different tables now score different sets.
     * ANN_WALK_FLOOR=f adds a confidence floor: if the pruned walk's top
     * score is below f, the query falls back to a full scan (mode
     * "anchor+full", fallback=1), so recall is never worse than today.
     * Both default OFF/0.0 = byte-identical behaviour to before. */
    /* FLOAT-KERNEL env check (per call; restart not required between runs). */
    { const char *fk = getenv("ANN_FLOAT_KERNEL");
      g_float_kernel = (fk && atoi(fk)) ? 1 : 0; }
    if (g_float_kernel && ents && nent > 0) lazy_inv_build(ents, nent);
    int walk_prune = 0; double walk_floor = 0.0;
    { const char *wp = getenv("ANN_WALK_PRUNE");
      if (wp && atoi(wp) > 0) walk_prune = 1; }
    { const char *wf = getenv("ANN_WALK_FLOOR");
      if (wf) walk_floor = atof(wf); }
    /* ANN_DOOR_EXIT (2026-10-20): the door's optional EARLY EXIT. The
     * door walks every row in door order (recall == the fallback it
     * replaces). With this on, the walk STOPS as soon as the top score
     * clears the EXIT threshold — the door found a good enough answer in
     * the shut bucket, so the rest of the corpus is not needed.
     * ANN_DOOR_EXIT_FLOOR is the exit threshold and is SEPARATE from the
     * trigger floor (ANN_WALK_FLOOR): the trigger says "this query is bad
     * enough to need the door", the exit says "this answer is good enough
     * to stop". Using the same number for both makes the exit nearly
     * dead — measured 2026-10-20: with exit floor == trigger floor the
     * EXIT arm scored exactly as many rows as the plain door (33,574 =
     * 33,574), because the corpus clears the trigger floor only at the end
     * of the walk. Default: the exit threshold equals the trigger floor
     * (so the exit stays OFF in effect unless it is given its own value). */
    int door_early_exit = 0;
    { const char *de = getenv("ANN_DOOR_EXIT");
      if (de && atoi(de) > 0) door_early_exit = 1; }
    double door_exit_floor = walk_floor;
    { const char *ef = getenv("ANN_DOOR_EXIT_FLOOR");
      if (ef && atof(ef) > 0.0) door_exit_floor = atof(ef); }
    /* set only while the DOOR walk is the active walk — the early exit
     * must not silently change the plain fallback path (that path is the
     * exact-answer baseline the door is measured against). */
    int door_walk_active = 0;
    uint32_t *pmem = NULL;          /* member-only walk sequence (owned) */
    unsigned char *pseen = NULL;    /* dedupe across buckets (owned) */
    uint32_t *pmem2 = NULL;         /* door walk sequence (owned; ANN_DOOR) */
    unsigned char *pseen2 = NULL;   /* door dedupe (owned; ANN_DOOR) */
    const uint32_t *walk = NULL; int nwalk = 0;
    double us_per_item = 0;
    if (ents && qn > 0) {
        const uint32_t *ord = NULL;
        uint32_t *ord_own = NULL;
        const float *C = NULL;
        int routed = 0, K = 0;
        int dead_route = 0;
        int route_ok = (lz_route_cached(nent) && g_anch_dim == qd);
        /* ── TAG SCOPE (2026-10-21, request field "scope") ────────────────
         * The walk becomes the rows of one tag instead of a routed subset.
         * Measured (bench_tag_filter.py, 300 self-excluded queries vs a
         * numpy oracle): scoped candidates p50 254 rows against the 24000
         * budget — 94x less work at the median — while the in-group best
         * trails the global best by 0.0110 mean / 0.0000 median cosine. So
         * the scope answers a NARROWER question on purpose (recall@5 vs the
         * global oracle is 0.44 fine / 0.70 kind against the route's 0.91):
         * this is a scope selector, never a substitute for global search.
         * It never touches the route: no buckets, no tombstone latch, no
         * order sidecar — the group is derived from the rows themselves, so
         * it cannot drift from the index (the positional-sidecar hazard the
         * 2026-10-20 fixes had to chase). A scope that resolves to nothing
         * fails LOUD as "scope-miss" (empty hits) instead of serving an
         * unscoped answer under a scoped request. */
        if (scope && scope[0] && ents && nent > 0) {
            char spat[256];
            int scope_tag = -1, scope_zone = -1;
            int r = lz_scope_resolve(ents, nent, scope, topk, spat, sizeof(spat),
                                     NULL, NULL, &scope_tag, &scope_zone);
            if (r > 0) {
                int cnt = 0;
                for (int i = 0; i < nent; i++)
                    if ((r == 1 && strcmp(ents[i].file, spat) == 0) ||
                        (r == 2 && lz_is_kind(ents[i].file, spat)) ||
                        (r == 3 && g_ent_tag[i] == scope_tag) ||
                        (r == 4 && g_ent_zone[i] == scope_zone)) cnt++;
                uint32_t *arr = (uint32_t *)malloc((size_t)(cnt ? cnt : 1) * sizeof(uint32_t));
                if (arr) {
                    int w = 0;
                    for (int i = 0; i < nent && w < cnt; i++)
                        if ((r == 1 && strcmp(ents[i].file, spat) == 0) ||
                            (r == 2 && lz_is_kind(ents[i].file, spat)) ||
                            (r == 3 && g_ent_tag[i] == scope_tag) ||
                            (r == 4 && g_ent_zone[i] == scope_zone))
                            arr[w++] = (uint32_t)i;
                    if (w > 0) {            /* never hand out an empty walk */
                        ord_own = arr; ord = arr; scope_n = w;
                        budget = w;
                        /* a file scope that fell through to its kind is
                         * labelled so; a bare kind request was scoped on
                         * purpose; a zone keeps its own label */
                        int widened = (r == 2 && strchr(scope, '/') != NULL);
                        snprintf(rm, sizeof(rm), "%s",
                                 r == 3 ? "scope+tag" :
                                 (r == 4 ? "scope+zone" :
                                  (r == 1 ? "scope" : (widened ? "scope+kind" : "scope"))));
                    } else {
                        free(arr);
                        scope_bad = 1;
                    }
                } else {
                    scope_bad = 1;
                }
            } else {
                scope_bad = 1;
            }
        }
        /* DEAD ROUTE (2026-10-20): the routing table exists (anchors+order
         * are valid, so lz_route_cached says routed) but the rows carry no
         * stamps — the index was rebuilt by scripts/build_index.py and the
         * incremental regrow wrote that dead state into the sidecar. The
         * pruned walk would then score only the handful of rows that happen
         * to be stamped and serve them as the top-k (measured 2026-10-20 on
         * live :8096: items_scored 4 of 62,204, health still green).
         * Fail LOUD instead: refuse the pruned route and walk every row
         * (the old, exact behaviour) until a regrow restores the stamps.
         * A FRESH index (no anchors yet, so no route) is NOT dead — it
         * simply has not been regrowed. */
        if (route_ok) dead_route = stamps_route_dead(nent);
        if (dead_route && !scope_n && !scope_bad) {
            rm[0] = 'd'; rm[1] = 'e'; rm[2] = 'a'; rm[3] = 'd';
            rm[4] = '-'; rm[5] = 'r'; rm[6] = 'o'; rm[7] = 'u';
            rm[8] = 't'; rm[9] = 'e'; rm[10] = '\0';
        }
        if (scope_n > 0 || scope_bad) {
            /* the scoped walk (or the fail-loud empty walk) is final: the
             * route layer is skipped entirely, so no bucket is routed, no
             * tombstone latch is shut and the order sidecar is not read. */
        } else if (route_ok && !dead_route) {
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

            /* graph + anchor: the trained route picks buckets by vector
             * geometry; the file graph knows the query's STRUCTURAL cluster.
             * When ANN_GRAFT_BUCKETS=1 we ADD the buckets the graph endorses
             * (the buckets its proposed entries land in) to the walked set, so
             * the walk covers both the geometric and the structural route.
             * Only ADDS buckets — never removes — so recall cannot drop. */
            if (g_graft_buckets && g_graft_on && g_qn > 0 && rnb < ANN_MAXK) {
                uint32_t gc[64];
                int gn = graft_candidates(gc, 64);
                if (gn > 0) {
                    int bvote[ANCHR_MAXK];
                    for (int b = 0; b < ANCHR_MAXK; b++) bvote[b] = 0;
                    for (int i = 0; i < gn; i++) {
                        const LZEntry *e = &ents[gc[i]];
                        if (e->anchor >= 0 && e->anchor < ANCHR_MAXK) bvote[e->anchor]++;
                        if (e->anchor2 >= 0 && e->anchor2 < ANCHR_MAXK) bvote[e->anchor2]++;
                    }
                    int added = 0;
                    for (int a = 0; a < K && rnb < ANN_MAXK; a++) {
                        int best = -1, bi = -1;
                        for (int b = 0; b < ANCHR_MAXK; b++) if (bvote[b] > best) { best = bvote[b]; bi = b; }
                        if (bi < 0 || best <= 0) break;
                        bvote[bi] = -1;
                        int dup = 0;
                        for (int r = 0; r < rnb; r++) if (rb[r] == bi) { dup = 1; break; }
                        if (!dup) { rb[rnb++] = bi; added++; }
                    }
                    if (added) snprintf(rm, sizeof(rm), "%s", "anchor+g");
                }
            }

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
                    /* Take the warm path ONLY when it actually skips work:
                     * cov < budget. Two reasons.
                     *  1. The old `budget = cov` RAISED the cap: cov counts the
                     *     entries covered by the SHUT buckets, and a single
                     *     bucket covers 24-30% of the index (B1), so a repeat
                     *     query pushed the budget ABOVE LZ_SEARCH_BUDGET —
                     *     measured 2026-10-14: repeat scored 47027 vs the 24000
                     *     cold cap. The warm path must never do MORE work than
                     *     the cold one. (Latent since D5 made 24000 a hard cap
                     *     on 2026-10-11; WARM-PATH.md predates that.)
                     *  2. If cov >= budget there is nothing to skip, and
                     *     reordering warm-first would only change WHICH entries
                     *     are scored (a recall risk) for no speed win — so
                     *     leave the cold walk untouched. */
                    if (cov > 0 && cov < budget) {
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

            /* the route WALKED shuts the latch (monotone), then persist it.
             * Only the OPEN->SHUT transition is a new fingerprint; a repeat
             * walk is a no-op, not a log record — otherwise the P3 log grows
             * without bound on repeat queries. */
            for (int rbi = 0; rbi < rnb; rbi++) {
                uint32_t fid = lz_fog_id(rb[rbi]);
                if (wl_is_open(&g_fog, fid))
                    wl_traverse_log(&g_fog, fid,
                                    g_fog_log_open ? &g_fog_log : NULL);
            }
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
                /* a dead route stays labelled dead-route: the graph is a
                 * soft proposal, but the STAMP wipe is the loud fact */
                if (!dead_route) snprintf(rm, sizeof(rm), "%s", "graph");
            } else {
                if (gcand) free(gcand);
                ord_own = (uint32_t *)malloc((nent ? (size_t)nent : 1) * sizeof(uint32_t));
                if (ord_own) { for (int i = 0; i < nent; i++) ord_own[i] = (uint32_t)i; ord = ord_own; }
            }
        }
        /* prune: gather bucket members BUCKET-MAJOR (rb[0]'s members first,
         * then rb[1], ...) so a budget cut keeps the NEAREST bucket's
         * entries — this is what makes the table steer the scored set.
         * (Ord order inside a bucket keeps block locality.) */
        walk = ord; nwalk = nent;
        if (scope_n > 0) nwalk = scope_n;   /* the walk is the group, not the index */
        if (walk_prune && routed && rnb > 0 && ord && nent > 0) {
            pmem = (uint32_t *)malloc((size_t)nent * sizeof(uint32_t));
            pseen = (unsigned char *)calloc((size_t)(nent ? nent : 1), 1);
            if (pmem && pseen) {
                int w = 0;
                for (int r = 0; r < rnb && w < nent; r++)
                    for (int i = 0; i < nent; i++) {
                        int ei = (int)ord[i];
                        if (ei < 0 || ei >= nent) continue;
                        if (pseen[ei]) continue;
                        if (ents[ei].anchor == rb[r] || ents[ei].anchor2 == rb[r]) {
                            pseen[ei] = 1;
                            pmem[w++] = (uint32_t)ei;
                        }
                    }
                if (w > 0) {
                    walk = pmem; nwalk = w;
                    size_t rl = strlen(rm);
                    if (rl + 2 < sizeof(rm)) { rm[rl] = '+'; rm[rl+1] = 'p'; rm[rl+2] = '\0'; }
                }
                else { free(pmem); pmem = NULL; free(pseen); pseen = NULL; }   /* empty: normal walk */
            }
            else { if (pmem) free(pmem); if (pseen) free(pseen); pmem = NULL; pseen = NULL; }
        }
        LARGE_INTEGER qf, qt0, qt1;
        QueryPerformanceFrequency(&qf);
        QueryPerformanceCounter(&qt0);
        int in_run = 0;
        breads = 0;
        int full2 = 0;      /* fallback pass: unrestricted full walk */
        cap = budget;
        /* scope walks every row of the group by construction: one past the
         * count so reaching the end is "done", not "cut by budget", and the
         * reported budget stays the group size (not cap). */
        if (scope_n > 0) cap = budget + 1;
        if (scope_bad) { budget = 0; cap = 0; }
        for (;;) {
        for (int oi = 0; walk && oi < nwalk; oi++) {
            int ei = (int)walk[oi];
            if (ei < 0 || ei >= nent) continue;
            int sel = 1;
            if (sid_filter[0] && strcmp(ents[ei].sid, sid_filter) != 0) sel = 0;
            if (sel && routed && rnb > 0 && !full2) {
                sel = (ents[ei].anchor < 0);
                for (int rbi = 0; rbi < rnb && !sel; rbi++)
                    if (ents[ei].anchor == rb[rbi] || ents[ei].anchor2 == rb[rbi]) sel = 1;
            }
            if (!sel) { in_run = 0; continue; }
            if (!in_run) { breads++; in_run = 1; }
            if (ents[ei].dim != qd) continue;
            double sc;
            if (g_float_kernel && g_inv && g_inv_n == nent &&
                ents[ei].norm > 0 && ents[ei].dim == qd) {
                /* FLOAT-KERNEL path (env ANN_FLOAT_KERNEL=1) */
                sc = (double)(lz_dotf(qv, ents[ei].vec, qd) *
                              qinv_local * g_inv[ei]);
            } else {
                double dot = lz_dot(qv, ents[ei].vec, qd);
                sc = (ents[ei].norm > 0) ? dot / (qn * ents[ei].norm) : -1;
            }
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
            if (scored >= cap) { truncated = 1; break; }
            /* DOOR EARLY EXIT (env ANN_DOOR_EXIT=1, default OFF): the door
             * walk is exhaustive by default (so recall matches the fallback
             * it replaces). With the exit on, the walk stops as soon as the
             * top score clears the floor again — the shut bucket already
             * held a good enough answer. This is the door's latency win,
             * and it is a HEURISTIC: the floor decides when to stop, so
             * recall can drop below the exhaustive path. */
            if (door_early_exit && door_walk_active && door_exit_floor > 0.0 &&
                top_sc[0] >= door_exit_floor) {
                truncated = 1;
                break;
            }
        }
        /* confidence fallback: pruned top below floor -> full scan once */
        if (walk_prune && !full2 && !scope_n && !scope_bad &&
            walk_floor > 0.0 && top_sc[0] < walk_floor) {
            /* ── TOMBSTONE DOOR (env ANN_DOOR=1, 2026-10-20) ─────────────
             * The user's architecture: a tombstone that is SHUT is a pinned
             * door to its bucket's members ("apple fruit mini db"). When the
             * walk scores below the floor AND the query's top bucket is a
             * KNOWN shut tombstone, jump straight to that bucket's members
             * first (walk order = bucket-major built from ord) and only
             * spill to the full scan if the door scores below the floor
             * too. GRAFT stays the fail-loud final fallback (the existing
             * full2 pass below — unchanged). OFF = byte-identical default.
             * Built from ord (NOT pmem) so the warm-path branch (which
             * replaces ord and never allocates pmem) can take the door. */
            int door = 0;
            { const char *de = getenv("ANN_DOOR");
              door = (de && atoi(de)) ? 1 : 0; }
            if (getenv("ANN_DOOR_TRACE")) {
                static int tf_n = 0;
                if (tf_n++ < 8)
                    fprintf(stderr, "[fallback-trace] floor=%.3f top0=%.4f pmem=%d routed=%d rnb=%d ord=%d door=%d\n",
                            walk_floor, top_sc[0], pmem != NULL, routed, rnb, ord != NULL, door);
            }
            if (door && routed && rnb > 0 && ord) {
                free(pmem); pmem = NULL;
                free(pseen); pseen = NULL;
                /* pick the FIRST routed bucket whose tombstone is actually
                 * SHUT — bucket 0 maps to wl id 0 = WL_RESERVED_LO, which
                 * wl_traverse refuses, so bucket 0 can never shut (probed
                 * 2026-10-20 via ANN_DOOR_TRACE). Skipping reserved-id
                 * buckets is the same rule the walk itself obeys. */
                int dbk = -1;
                for (int ri = 0; ri < rnb; ri++) {
                    uint32_t fi = lz_fog_id(rb[ri]);
                    if (!wl_is_open(&g_fog, fi)) { dbk = rb[ri]; break; }
                }
                if (dbk >= 0) {                          /* a shut door found */
                    pmem2 = (uint32_t *)malloc((size_t)nent * sizeof(uint32_t));
                    pseen2 = (unsigned char *)calloc((size_t)(nent ? nent : 1), 1);
                    if (pmem2 && pseen2) {
                        int w = 0;
                        /* door members first ... */
                        for (int i = 0; i < nent; i++) {
                            int ei = (int)ord[i];
                            if (ei < 0 || ei >= nent || pseen2[ei]) continue;
                            if (ents[ei].anchor == dbk || ents[ei].anchor2 == dbk) {
                                pseen2[ei] = 1; pmem2[w++] = (uint32_t)ei;
                            }
                        }
                        /* ... then the REST of the routed buckets' members ... */
                        for (int r = 1; r < rnb; r++) {
                            for (int i = 0; i < nent; i++) {
                                int ei = (int)ord[i];
                                if (ei < 0 || ei >= nent || pseen2[ei]) continue;
                                if (ents[ei].anchor == rb[r] || ents[ei].anchor2 == rb[r]) {
                                    pseen2[ei] = 1; pmem2[w++] = (uint32_t)ei;
                                }
                            }
                        }
                        /* ... then everything else (kept: the door only
                         * REORDERS the walk). */
                        for (int i = 0; i < nent; i++) {
                            int ei = (int)ord[i];
                            if (ei < 0 || ei >= nent || pseen2[ei]) continue;
                            pseen2[ei] = 1; pmem2[w++] = (uint32_t)ei;
                        }
                        if (w > 0) {
                            /* free the pruned arrays (replaced by door walk) */
                            if (pmem) { free(pmem); pmem = NULL; }
                            if (pseen) { free(pseen); pseen = NULL; }
                            pmem = pmem2; pseen = pseen2;
                            pmem2 = NULL; pseen2 = NULL;   /* ownership moved */
                            walk = pmem; nwalk = w;
                            truncated = 0; scored = 0; breads = 0;
                            for (int ti = 0; ti < ANN_MAXK; ti++) {
                                top_sc[ti] = -2;
                                if (top_sid) top_sid[ti][0] = '\0';
                                if (top_file) top_file[ti][0] = '\0';
                                if (top_np) top_np[ti] = 0;
                            }
                            in_run = 0;
                            if (pseen) for (int ti = 0; ti < nent; ti++) pseen[ti] = 0;
                            snprintf(rm, sizeof(rm), "%s", "door");
                            full2 = 1;      /* one-shot: the door runs ONCE —
                                             * a repeat below floor must fall
                                             * through to the FULL2 scan, not
                                             * re-enter the door forever. */
                            /* RECALL CONTRACT (2026-10-20): the door
                             * REPLACES the below-floor fallback, and the
                             * fallback scans EVERY row (cap = nent). If the
                             * door kept the pruned cap (budget), it would
                             * score fewer rows than the path it replaces —
                             * measured live on 300 queries against a numpy
                             * oracle: recall@5 0.9307 (door) vs 1.0000
                             * (fallback), 56/300 queries worse. The handoff
                             * claimed "recall cannot drop"; it could, and
                             * did. So the door now scans EVERY row in
                             * DOOR ORDER: same recall, different order.
                             * The door's WIN is the ORDER (the shut bucket's
                             * members are scored first), and the optional
                             * early exit below (ANN_DOOR_EXIT, default
                             * OFF) is where a latency win can be taken
                             * without giving up recall. */
                            cap = nent;
                            /* the env flag SURVIVES here: the door walk is
                             * the one that may exit early. (It used to be
                             * cleared here, which silently disabled
                             * ANN_DOOR_EXIT — measured 2026-10-20: the EXIT
                             * arm scored all 62,266 rows, exactly like the
                             * plain door.) */
                            door_walk_active = 1;
                            continue;                      /* rewalk with door order */
                        }
                    }
                    if (pmem2) { free(pmem2); pmem2 = NULL; }
                    if (pseen2) { free(pseen2); pseen2 = NULL; }
                }
            }
            /* ── GRAFT AS HARD FALLBACK (env ANN_GRAFT_FALLBACK=1) ─────────
             * The open item from the 2026-10-20 handoff: "graft-as-hard-
             * fallback (graft proposes the walk list when the route layer
             * rejects) is NOT built — today graft kept its soft-bias
             * contract. The door branch is the attachment point."
             *
             * Here it is, as an OPT-IN branch (default OFF = byte-identical):
             * when the below-floor fallback fires and the graph can propose
             * candidates, the walk order becomes the graph's proposal
             * instead of the raw index order. The graph proposes a SMALLER
             * set (structurally related entries), so the scan is cheaper;
             * because the walk still covers everything the graph did not
             * propose (the remainder follows), recall cannot drop below the
             * full scan.
             * The graph proposes from STRUCTURE (file keys), which is
             * independent of the vector geometry — so this is a different
             * axis, not a second copy of the route.
             * OFF by default because a structural proposal narrows what a
             * query can reach: it is only correct if the query really is
             * about those files. Measure it before enabling. */
            int graft_fb = 0;
            { const char *gf = getenv("ANN_GRAFT_FALLBACK");
              if (gf && atoi(gf) > 0) graft_fb = 1; }
            if (graft_fb && g_graft_on && g_qn > 0 && nent > 0) {
                int gcap = nent;
                { const char *gc = getenv("ANN_GRAFT_CAND");
                  if (gc && atoi(gc) > 0) gcap = atoi(gc); }
                uint32_t *gcand = (uint32_t *)malloc((size_t)gcap * sizeof(uint32_t));
                int gn = gcand ? graft_candidates(gcand, gcap) : 0;
                if (gn > 0 && gcand) {
                    /* the graph's proposal FIRST, then everything else */
                    uint32_t *gf2 = (uint32_t *)malloc((size_t)nent * sizeof(uint32_t));
                    unsigned char *gs = (unsigned char *)calloc((size_t)nent, 1);
                    if (gf2 && gs) {
                        int w = 0;
                        for (int i = 0; i < gn; i++) {
                            int ei = (int)gcand[i];
                            if (ei < 0 || ei >= nent || gs[ei]) continue;
                            gs[ei] = 1; gf2[w++] = (uint32_t)ei;
                        }
                        for (int i = 0; i < nent && w < nent; i++) {
                            if (gs[i]) continue;
                            gs[i] = 1; gf2[w++] = (uint32_t)i;
                        }
                        if (w > 0) {
                            if (pmem) { free(pmem); pmem = NULL; }
                            if (pseen) { free(pseen); pseen = NULL; }
                            pmem = gf2; pseen = gs;
                            gf2 = NULL; gs = NULL;
                            walk = pmem; nwalk = w;
                            /* THE SAME RECALL CONTRACT AS THE DOOR (2026-10-21):
                             * this branch REPLACES the below-floor fallback,
                             * and that fallback scans EVERY row (cap = nent).
                             * Keeping the pruned cap here scored fewer rows
                             * than the path it replaced — measured against
                             * a numpy oracle: recall@5 0.2833 (graft, cap
                             * 24k) vs 0.4167 (plain fallback, 62k), 5 of 12
                             * queries worse. So the graft walk covers EVERY
                             * row too, in graft-first ORDER: the proposal
                             * is scored first (the graph's structural
                             * preference), and the remainder follows, so
                             * recall cannot drop below the fallback.
                             * The order is then the only difference — and
                             * the order matters because the graft bias
                             * (which rewards entries carrying the query's
                             * file keys) now applies to the entries that
                             * are scored FIRST. */
                            cap = nent;
                            scored = 0; breads = 0; truncated = 0;
                            for (int ti = 0; ti < ANN_MAXK; ti++) {
                                top_sc[ti] = -2;
                                if (top_sid) top_sid[ti][0] = '\0';
                                if (top_file) top_file[ti][0] = '\0';
                                if (top_np) top_np[ti] = 0;
                            }
                            in_run = 0;
                            snprintf(rm, sizeof(rm), "%s", "graft+full");
                            full2 = 1; fallback = 1;
                            free(gcand);
                            continue;
                        }
                    }
                    free(gf2); free(gs);
                }
                if (gcand) free(gcand);
            }
            full2 = 1; fallback = 1;
            walk = ord; nwalk = nent; cap = nent; truncated = 0;
            scored = 0; breads = 0;
            for (int ti = 0; ti < ANN_MAXK; ti++) {
                top_sc[ti] = -2;
                if (top_sid) top_sid[ti][0] = '\0';
                if (top_file) top_file[ti][0] = '\0';
                if (top_np) top_np[ti] = 0;
            }
            snprintf(rm, sizeof(rm), "%s", "anchor+full");
            continue;
        }
        break;
        }
        QueryPerformanceCounter(&qt1);
        if (scored > 0 && qf.QuadPart > 0)
            us_per_item = (double)(qt1.QuadPart - qt0.QuadPart) * 1e6 / (double)qf.QuadPart / scored;
        free(ord_own);
        if (pmem) free(pmem);
        if (pseen) free(pseen);
        if (pmem2) free(pmem2);
        if (pseen2) free(pseen2);
        if (!routed) breads = nent ? 1 : 0;
    }
    if (!use_cache) lz_index_free(ents, nent);
    if (route_mode) snprintf(route_mode, 16, "%s", rm);
    if (route_bk) for (int ri = 0; ri < ANN_MAXK; ri++) route_bk[ri] = rb[ri];
    if (route_nb_out) *route_nb_out = rnb;
    if (block_reads_out) *block_reads_out = breads;
    if (items_scored_out) *items_scored_out = scored;
    if (nent_out) *nent_out = nent;
    if (budget_out) *budget_out = (scope_n > 0) ? budget : cap;
    if (truncated_out) *truncated_out = truncated;
    if (fallback_out) *fallback_out = fallback;
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
    { const char *gb = getenv("ANN_GRAFT_BUCKETS"); g_graft_buckets = (gb && atoi(gb)) ? 1 : 0; }
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
 * This is what makes a fresh base index (sid/file/npast/dim/vec) routable.
 *
 * ── REGROW MODES (2026-10-20) ──
 *   unchanged   : index mtime+size+rowcount match the state file AND the
 *                 anchors load with their checksum -> return WITHOUT reading
 *                 or writing anything beyond stat + a small anchors read
 *                 (measured <0.5s). Telemetry: mode:"unchanged", skipped:1.
 *   incremental : the index GREW by appending only — the line at
 *                 state.n_prev-1 still carries state.last_sid, n >= n_prev.
 *                 Reuses the EXISTING trained anchors (no kmeans); routes ONLY
 *                 the new rows; recomputes the perm splice; rewrites the index
 *                 file (unavoidable one-pass rewrite); saves a fresh state.
 *   full        : state missing/corrupt/contract failed, or body has
 *                 "force":1 -> the EXISTING complete pipeline (kmeans train +
 *                 route-all + perm + rewrite), unchanged byte-for-byte, and a
 *                 fresh state file is written.
 * The state file is a NEW sidecar (<PATH_ANCH minus .bin>-state.json): the
 * on-disk index/anchors/order FORMAT is untouched. */
/* ── regrow state sidecar (state file, separate from index/anchors/order) ──
 * Stored next to the anchors: data/ann_anchors-state.json. Format (JSON):
 *   {"n_prev": N, "last_sid": "...", "index_mtime": T, "index_size": S,
 *    "K": K, "anchors_cksum": C, "full_ms": M}
 * n_prev/last_sid are the append-verification contract; mtime/size/K/cksum
 * are the unchanged fast-path contract; full_ms is telemetry only. */
static char g_regrow_state_path[600] = "";
static const char *regrow_state_path(void) {
    if (g_regrow_state_path[0]) return g_regrow_state_path;
    const char *a = PATH_ANCH;
    size_t l = strlen(a);
    if (l > 4 && strcmp(a + l - 4, ".bin") == 0)
        snprintf(g_regrow_state_path, sizeof(g_regrow_state_path), "%.*s-state.json", (int)(l - 4), a);
    else
        snprintf(g_regrow_state_path, sizeof(g_regrow_state_path), "%s-state.json", a);
    return g_regrow_state_path;
}
typedef struct {
    long n_prev;        /* -1 = absent */
    char last_sid[64];
    long index_mtime, index_size;
    int K;
    unsigned anchors_cksum;
} RegrowState;
static void regrow_state_load(RegrowState *st) {
    st->n_prev = -1;
    st->last_sid[0] = 0;
    st->index_mtime = -1; st->index_size = -1; st->K = -1; st->anchors_cksum = 0;
    FILE *f = fopen(regrow_state_path(), "r");
    if (!f) return;
    char sb[2048];
    size_t got = fread(sb, 1, sizeof(sb) - 1, f);
    sb[got] = 0;
    fclose(f);
    const char *p = strstr(sb, "\"n_prev\":");
    if (p) st->n_prev = atol(p + 9);
    p = strstr(sb, "\"last_sid\"");
    if (p) {
        p = strchr(p, ':');
        if (p) { p++; while (*p == ' ') p++; if (*p == '"') p++; }
        int ii = 0;
        while (*p && *p != '"' && ii < 63) st->last_sid[ii++] = *p++;
        st->last_sid[ii] = 0; }
    p = strstr(sb, "\"index_mtime\":");
    if (p) st->index_mtime = atol(p + 14);
    p = strstr(sb, "\"index_size\":");
    if (p) st->index_size = atol(p + 13);
    p = strstr(sb, "\"K\":");
    if (p) st->K = atoi(p + 4);
    p = strstr(sb, "\"anchors_cksum\":");
    if (p) st->anchors_cksum = (unsigned)strtoul(p + 16, NULL, 10);
}
/* THE GATE: the sidecar's labels are valid for this buffer only if the
 * buffer's prefix is the SAME rows the sidecar was computed for. The proof
 * is the regrow's own append contract (the state file's n_prev + last_sid).
 * A buffer that fails it gets NO sidecar labels: the route then reads as
 * dead and the walk fails loud (full scan) instead of routing by stamps
 * that belong to another index version. */
static int lz_stamp_prefix_ok(const LZEntry *a, int n) {
    if (!a || n <= 0) return 0;
    RegrowState st;
    regrow_state_load(&st);
    if (st.n_prev <= 0 || st.n_prev > n) return 0;
    if (!st.last_sid[0]) return 0;
    const char *sid = a[st.n_prev - 1].sid;
    if (!sid[0]) return 0;
    return strcmp(sid, st.last_sid) == 0;
}

static void regrow_state_save(const RegrowState *st, double full_ms) {
    char tmp[600];
    snprintf(tmp, sizeof(tmp), "%s.tmp", regrow_state_path());
    FILE *f = fopen(tmp, "w");
    if (!f) return;
    char esid[160];
    lz_json_escape(esid, sizeof(esid), st->last_sid);
    fprintf(f, "{\"n_prev\":%ld,\"last_sid\":\"%s\",\"index_mtime\":%ld,"
               "\"index_size\":%ld,\"K\":%d,\"anchors_cksum\":%u,"
               "\"full_ms\":%.0f}\n",
            st->n_prev, esid, st->index_mtime, st->index_size, st->K,
            st->anchors_cksum, full_ms);
    fclose(f);
    remove(regrow_state_path());
    if (rename(tmp, regrow_state_path()) != 0) remove(tmp);
}
/* append-contract check: n > n_prev and the prefix marker row's sid matches.
 * Cheap: entries are already parsed in memory; it's a strcmp. */
static int regrow_check_append(const RegrowState *st, const LZEntry *a, int n) {
    if (st->n_prev <= 0 || st->n_prev >= n) return 0;
    if (!st->last_sid[0]) return 0;
    const char *sid = a[st->n_prev - 1].sid;
    if (!sid[0]) return 0;
    return strcmp(sid, st->last_sid) == 0;
}

/* ── RESTAMP REPAIR (2026-10-20) ─────────────────────────────────────────
 * A rebuild of the index (scripts/build_index.py) writes rows WITHOUT the
 * anchor fields, so the routing table of every row is gone while the anchor
 * table and the order file stay valid — the server then serves a handful of
 * stamped rows as if they were the top-k (measured on live :8096:
 * items_scored 4 of 62,204).
 *
 * The FULL regrow path would repair it by rewriting the 233 MB jsonl — but
 * that path re-serializes every vector with "%.6g", which is a LOSSY
 * re-encoding of the values build_index.py wrote (round(x, 6) keeps 6
 * decimals; %.6g keeps 6 SIGNIFICANT digits). The vectors must not change.
 *
 * RESTAMP repairs the routing table WITHOUT touching the vectors: re-route
 * every row against the EXISTING centroids, write the fresh stamps into the
 * sidecar (<index>.stamp — the perm-sidecar's own channel), and rewrite the
 * order file so the bucket-major permutation matches the new stamps. The
 * jsonl is not read for writing and not rewritten: the apples stay.
 * Cost: one index parse + n*K distance computations, no 233 MB write.
 * Contract: the anchor table must exist and be valid (K/dim/checksum); if
 * it does not, there is nothing to re-route against -> the caller falls
 * back to the full path. 0 = ok, -1 = no usable table, -2 = bad args. */
static int lz_restamp(int *n_out, int *K_out) {
    LZEntry *a = NULL;
    int n = 0;
    *n_out = 0; if (K_out) *K_out = 0;
    lz_index_load(&a, &n, NULL);
    if (n < LZ_ANCH_MIN || !a || a[0].dim <= 0 || a[0].dim > ANCHR_MAXD) {
        lz_index_free(a, n);
        return -1;
    }
    int dim = a[0].dim;
    float *C = (float *)malloc((size_t)ANCHR_MAXK * ANCHR_MAXD * sizeof(float));
    if (!C) { lz_index_free(a, n); return -2; }
    int adim = 0, antr = 0;
    int K = anch_load(PATH_ANCH, C, ANCHR_MAXK, ANCHR_MAXD, &adim, &antr);
    if (K <= 0 || adim != dim) {
        free(C); lz_index_free(a, n);
        return -1;                      /* no usable table: full path instead */
    }
    int *rows  = (int *)malloc((size_t)n * sizeof(int));
    int *rows2 = (int *)malloc((size_t)n * sizeof(int));
    uint32_t *perm = (uint32_t *)malloc((size_t)n * sizeof(uint32_t));
    if (!rows || !rows2 || !perm) {
        free(C); free(rows); free(rows2); free(perm);
        lz_index_free(a, n);
        return -2;
    }
    for (int i = 0; i < n; i++) {
        if (a[i].dim != dim) { rows[i] = -1; rows2[i] = -1; continue; }
        int t2[LZ_ANCH_MAXK];
        int nb = anch_route(a[i].vec, C, K, dim, LZ_OVERLAP_TOPB, t2);
        rows[i]  = nb > 0 ? t2[0] : -1;
        rows2[i] = nb > 1 ? t2[1] : -1;
    }
    int rc = -1;
    if (anch_perm(rows, n, K, perm) == 0) {
        /* the sidecar carries the fresh stamps (the jsonl keeps its own
         * stamps; the overlay prefers the sidecar when it covers n rows) */
        char sp[600];
        snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
        FILE *sf = fopen(sp, "wb");
        if (sf) {
            int good = 1;
            for (int i = 0; i < n; i++) {
                int32_t rec[2] = { (int32_t)rows[i], (int32_t)rows2[i] };
                if (fwrite(rec, sizeof(rec), 1, sf) != 1) { good = 0; break; }
            }
            fclose(sf);
            /* the order file must carry the SAME permutation (bucket-major
             * over the fresh stamps) and stay bound to the table */
            if (good && ord_save(PATH_ORDER, perm, n, K, n,
                                 anch_cksum(C, K, dim)) == 0) {
                /* the anchors' ntr must equal the row count, else the route
                 * layer refuses the pair (antr != nent -> routed:0) */
                if (anch_save_atomic(PATH_ANCH, C, K, dim, n) == 0) {
                    RegrowState ns;
                    regrow_state_load(&ns);
                    ns.n_prev = n;
                    strncpy(ns.last_sid, a[n - 1].sid, sizeof(ns.last_sid) - 1);
                    ns.last_sid[sizeof(ns.last_sid) - 1] = 0;
                    struct stat sns;
                    if (stat(PATH_INDEX, &sns) == 0) {
                        ns.index_mtime = (long)sns.st_mtime;
                        ns.index_size  = (long)sns.st_size;
                    }
                    ns.K = K;
                    ns.anchors_cksum = anch_cksum(C, K, dim);
                    regrow_state_save(&ns, 0.0);
                    rc = 0;
                    *n_out = n;
                    if (K_out) *K_out = K;
                }
            }
        }
    }
    free(C); free(rows); free(rows2); free(perm);
    lz_index_free(a, n);
    return rc;
}

/* ── TREE-SIDE LABEL READ (2026-10-20) ────────────────────────────────────
 * The routing labels live in the TREE's channel: <index>.stamp (8 bytes per
 * row: anchor, anchor2). Reading them is a TREE operation — it never touches
 * a vector. This is the separation the owner's architecture requires
 * (HANDOFF-2026-10-10-session.md §5): the fruit (the vector rows) carries
 * no tree state, the tree carries the labels, and a regrow moves the tree
 * without rewriting the fruit.
 *
 * Returns the number of label pairs loaded (0 if the sidecar is missing or
 * shorter than n_prefix); out_a/out_a2 are the caller's buffers.
 */
static int lz_stamp_prefix_load(int n_prefix, int **out_a, int **out_a2) {
    *out_a = NULL; *out_a2 = NULL;
    if (n_prefix <= 0) return 0;
    char sp[600];
    snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
    struct stat ss;
    if (stat(sp, &ss) != 0) return 0;
    if ((long)ss.st_size < (long)n_prefix * 8) return 0;
    FILE *sf = fopen(sp, "rb");
    if (!sf) return 0;
    int *pa = (int *)calloc((size_t)n_prefix, sizeof(int));
    int *pa2 = (int *)calloc((size_t)n_prefix, sizeof(int));
    if (!pa || !pa2) { free(pa); free(pa2); fclose(sf); return 0; }
    int got = 0;
    for (int i = 0; i < n_prefix; i++) {
        int32_t rec[2];
        if (fread(rec, sizeof(rec), 1, sf) != 1) break;
        pa[i] = (int)rec[0];
        pa2[i] = (int)rec[1];
        got++;
    }
    fclose(sf);
    if (got < n_prefix) { free(pa); free(pa2); return 0; }
    *out_a = pa; *out_a2 = pa2;
    return got;
}

typedef struct {
    long n_prev;        /* entries present BEFORE this regrow (state-file contract) */
    long n_new;         /* n - n_prev */
    int skipped;        /* 1 = untouched fast path */
    char regrow_mode[16]; /* "unchanged"|"incremental"|"full"|"brute"|"error" */
    char load_src[16];    /* "borrowed" = reused the loader's buffer;
                           * "parse"    = the regrow parsed the jsonl itself;
                           * "none"     = the unchanged fast path never loaded */
    double ms_load, ms_train, ms_route, ms_rewrite, ms_save;
} RegrowTelemetry;

static int lz_anchor_refresh(int force, int *Kout, int *nout,
                             RegrowTelemetry *tel) {
    if (Kout) *Kout = 0;
    /**** unchanged fast path FIRST: zero index parse, stat-only. ****/
    /**** Skipped-path()==0 means the file's stat-based state matches the
     **** recorded state and the anchors load with a matching checksum. No
     **** row parsing happens at all — the whole regrow is a stat + a
     **** checksum read. ****/
    if (!force) {
        LARGE_INTEGER qf0;
        QueryPerformanceFrequency(&qf0);
        LARGE_INTEGER tld0, tld1;
        QueryPerformanceCounter(&tld0);
        struct stat sti;
        RegrowState st0;
        regrow_state_load(&st0);
        if (stat(PATH_INDEX, &sti) == 0 &&
            st0.n_prev >= 0 && st0.K > 0 &&
            st0.index_mtime == (long)sti.st_mtime &&
            st0.index_size == (long)sti.st_size) {
            /* row count: the state holds n_prev; we need n — count lines cheaply?
             * NO: do it RIGHT. stat alone can't count lines. The ORIGINAL
             * contract requires row count matching — but row count matching at
             * this point requires a row check. What the DESIGN says: stat(file)
             * + anchors load + checksum. Row count: n == st0.n_prev is implied
             * by mtime+size matching AND the state was written after the last
             * successful regrow, so the on-disk size matching the saved size
             * means no rows were added/removed. A row check would cost an
             * O(N) read. The contract's INTENT is: skip if the file hasn't
             * changed since the last regrow (mtime, size), which is exactly
             * what stat detects. That is O(1). Use it. */
            int unchanged = 0;
            if (st0.last_sid[0] && st0.anchors_cksum != 0) {
                /* We still can't parse the anchors' dim without reading the
                 * header: anch_load wants a dim. Read 2 u32 from the header
                 * directly (K, dim) — NOT the full parse; O(1). */
                FILE *af = fopen(PATH_ANCH, "rb");
                if (af) {
                    uint32_t ahdr[5];
                    if (fread(ahdr, sizeof(ahdr), 1, af) == 1 &&
                        ahdr[0] == ANCHR_MAGIC) {
                        /* header K/dim match state's K? */
                        if ((int)ahdr[1] == st0.K) {
                            /* full checksum verify */
                            fseek(af, 0, SEEK_END);
                            long afz = ftell(af);
                            fseek(af, 20, SEEK_SET);   /* 5 u32 header */
                            size_t nb = (size_t)(afz - 20);
                            float *C = (float *)malloc(nb);
                            if (C && fread(C, 1, nb, af) == nb)
                                unchanged = (anch_cksum(C, ahdr[1], (int)ahdr[2]) == ahdr[4] &&
                                             anch_cksum(C, ahdr[1], (int)ahdr[2]) == st0.anchors_cksum);
                            free(C);
                        }
                    }
                    fclose(af);
                }
            }
            if (unchanged) {
                /* THE TREE'S CHANNEL MUST EXIST TOO (2026-10-20): the
                 * "unchanged" fast path proves the FRUIT is unchanged (stat)
                 * and the TABLE is unchanged (checksum). It must also prove
                 * the TREE'S LABELS are there — the routing labels live in
                 * the sidecar, and a rebuild that lost it leaves the fruit
                 * and the table intact while the routing table is GONE.
                 * Without this check the server reports "unchanged" (and
                 * /health says routed=1) while every query scores a handful
                 * of rows (measured 2026-10-20, live :8096). */
                int labels_ok = 0;
                { char sp[600];
                  snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
                  struct stat ss;
                  if (stat(sp, &ss) == 0 &&
                      (long)ss.st_size >= (long)st0.n_prev * 8) {
                      /* the sidecar must carry LIVE labels, not a field of
                       * -1 (the degenerate state the bug produced) */
                      FILE *sf = fopen(sp, "rb");
                      if (sf) {
                          labels_ok = 1;
                          int32_t rec[2];
                          int checked = 0;
                          for (int i = 0; i < st0.n_prev; i++) {
                              if (fread(rec, sizeof(rec), 1, sf) != 1) { labels_ok = 0; break; }
                              if (rec[0] < 0) { labels_ok = 0; break; }
                              checked++;
                          }
                          if (checked != st0.n_prev) labels_ok = 0;
                          fclose(sf);
                      }
                  } }
                if (labels_ok) {
                    QueryPerformanceCounter(&tld1);
                    tel->ms_load = (double)(tld1.QuadPart - tld0.QuadPart) * 1000.0 / (double)qf0.QuadPart;
                    tel->ms_train = 0; tel->ms_route = 0; tel->ms_rewrite = 0; tel->ms_save = 0;
                    tel->skipped = 1;
                    tel->n_prev = st0.n_prev; tel->n_new = 0;
                    snprintf(tel->regrow_mode, sizeof(tel->regrow_mode), "unchanged");
                    snprintf(tel->load_src, sizeof(tel->load_src), "none");
                    if (nout) *nout = st0.n_prev;
                    if (Kout) *Kout = st0.K;
                    return 2;
                }
                /* the fruit and the table are unchanged but the tree's
                 * labels are gone -> fall THROUGH to the repair path
                 * (no_tree / full), never report "unchanged" over a dead
                 * routing table. */
            }
        }
    }
    /* NOT unchanged: get the rows and proceed (incremental or full). */
    LZEntry *a = NULL;
    int n = 0;
    int borrowed = 0;   /* 1 = a/n point at the loader's buffer (do NOT free) */
    int published = 0;  /* 1 = this regrow's own parse became the served buffer */
    LARGE_INTEGER qf;
    LARGE_INTEGER t_load0, t_load_end, t_train0, t_train1;
    QueryPerformanceFrequency(&qf);
    QueryPerformanceCounter(&t_load0);
    /* Prefer the loader's already-parsed buffer: re-parsing the same 258 MB
     * jsonl here cost ~50 s and produced the same rows. The borrow only
     * succeeds when the loader's snapshot matches the file on disk. */
    borrowed = lz_index_borrow(&a, &n);
    if (!borrowed) lz_index_load(&a, &n, NULL);
    QueryPerformanceCounter(&t_load_end);
    double load_ms = (double)(t_load_end.QuadPart - t_load0.QuadPart) * 1000.0 / (double)qf.QuadPart;
    if (nout) *nout = n;
    tel->ms_load = load_ms;
    snprintf(tel->load_src, sizeof(tel->load_src), borrowed ? "borrowed" : "parse");
    lz_diag("regrow_load src=%s n=%d ms=%.1f ts=%lld\n",
            borrowed ? "borrowed" : "parse", n, load_ms, (long long)time(NULL));
    tel->ms_train = 0; tel->ms_route = 0; tel->ms_rewrite = 0; tel->ms_save = 0;
    tel->skipped = 0; tel->n_prev = 0; tel->n_new = 0;
    snprintf(tel->regrow_mode, sizeof(tel->regrow_mode), "brute");
    int rc = 1;
    struct stat sti_load0;
    long load0_mtime = 0, load0_size = 0;
    if (stat(PATH_INDEX, &sti_load0) == 0) {
        load0_mtime = (long)sti_load0.st_mtime;
        load0_size  = (long)sti_load0.st_size;
    }
    if (n >= LZ_ANCH_MIN && a[0].dim > 0 && a[0].dim <= ANCHR_MAXD) {
        int dim = a[0].dim, K = n >= 64 ? 8 : n >= 32 ? 4 : 2;
        if (K > LZ_ANCH_MAXK) K = LZ_ANCH_MAXK;
        int m = 0;
        for (int i = 0; i < n; i++) if (a[i].dim == dim) m++;
        RegrowState st;
        regrow_state_load(&st);
        int incremental = (!force) && regrow_check_append(&st, a, n);
        /* Fail-loud race guard: ann-sync may append while we parsed the
         * snapshot. If the file changed under us (mtime/size differ from the
         * load0 snapshot), the rows we hold are NOT the real file — writing
         * them out would drop the concurrent rows. Abort incremental and
         * re-run the FULL path (which re-loads internally? no: it reuses
         * `a` — so instead flag and let the caller retry). We surface this
         * as a "race" error so the operator retriggers regrow. */
        if (incremental) {
            struct stat sti_now;
            if (stat(PATH_INDEX, &sti_now) == 0 &&
                ((long)sti_now.st_mtime != load0_mtime ||
                 (long)sti_now.st_size  != load0_size)) {
                tel->n_prev = st.n_prev;
                tel->n_new = -1;
                snprintf(tel->regrow_mode, sizeof(tel->regrow_mode), "race");
                rc = -2;
                goto regrow_done;
            }
        }
        if (m >= K) {
            if (incremental) {
                /* INCREMENTAL: load the existing anchors; no kmeans.
                 * Fall back to the full pipeline below if the load fails
                 * (missing/corrupt anchors -> contract fail). */
                float *C = (float *)malloc((size_t)K * dim * sizeof(float));
                int C_ok = 0;
                if (C) {
                    int vd = 0, vn = 0;
                    if (anch_load(PATH_ANCH, C, K, dim, &vd, &vn) == K &&
                        vd == dim) C_ok = 1;
                }
                if (!C_ok) { free(C); incremental = 0; goto full_pipeline; }
                /* CRITICAL CONTRACT: anchors' ntr must equal the NEW nent,
                 * otherwise lz_route_cached() rejects the pair (antr != nent)
                 * and the server falls to a full brute scan forever. The
                 * reused table's CENTROIDS are unchanged, so the checksum
                 * stays the same — only the header ntr moves to n. Re-save
                 * atomically even though the vectors are identical. This was
                 * the 2026-10-20 incident: after an append the anchors kept
                 * ntr=n_prev while order/index carried n → routed:0 + brute. */
                if (anch_save_atomic(PATH_ANCH, C, K, dim, n) != 0) {
                    free(C); incremental = 0; goto full_pipeline;
                }
                /* 1) re-route ONLY the new rows [n_prev .. n) into the
                 * existing C. rows[i<n_prev] restore the STAMPED anchors
                 * already stored in the index file's anchor/anchor2 fields.
                 * (The full-path kmeans load, which is 0 for this mode, and
                 * the route-only sweep eat the train/ route contracts: the
                 * ms_train slot is held 0 so the telemetry is honest — there
                 * was no training.) */
                LARGE_INTEGER t_route0, t_route1;
                QueryPerformanceCounter(&t_route0);
                int *rows = (int *)malloc((size_t)n * sizeof(int));
                int *rows2 = (int *)malloc((size_t)n * sizeof(int));
                uint32_t *perm = (uint32_t *)malloc((size_t)n * sizeof(uint32_t));
                /* TREE-SIDE LABELS (2026-10-20) ─────────────────────────────
                 * The routing labels (which branch a row hangs on) belong to
                 * the TREE, not to the FRUIT. The fruit is the vector row;
                 * the tree's label for it lives in the sidecar
                 * (<index>.stamp) and in the order perm. build_index.py
                 * rebuilds the fruit WITHOUT the anchor fields — that is
                 * CORRECT (the fruit carries no tree state).
                 *
                 * The bug this fixes: the incremental regrow read those
                 * (absent) fields and wrote them back as dead labels,
                 * overwriting the tree's own labels for 62k rows — measured
                 * 2026-10-20 on live :8096: items_scored 4 of 62,204 while
                 * /health still said routed:1. Under the owner's
                 * architecture (HANDOFF-2026-10-10-session.md §5) the fruit
                 * is a CLOSED cluster: old rows keep their label and are
                 * searched under it, and a broken branch fails box-by-box —
                 * never all at once.
                 *
                 * So the prefix labels are READ, never re-derived:
                 *   1. the sidecar (the tree's own channel) — authoritative
                 *   2. the fruit's embedded fields — a copy, no vector read
                 *   3. otherwise the tree is gone -> FAIL LOUD (no_tree) and
                 *      the caller repairs with POST /v1/state/restamp, which
                 *      re-places the fruit on the tree WITHOUT rewriting it
                 *      (the jsonl is not touched at all).
                 * Only genuinely NEW rows are routed onto the tree — that
                 * is the one place a vector is read for placement. */
                int *pa = NULL, *pa2 = NULL;
                int pl = lz_stamp_prefix_load(st.n_prev, &pa, &pa2);
                int prefix_from_fruit = 0;
                int tree_intact = 0;
                if (pl == st.n_prev) {
                    int live = 0;
                    for (int i = 0; i < pl; i++) if (pa[i] >= 0) live++;
                    tree_intact = (live == pl);
                }
                if (!tree_intact) {
                    /* the tree's sidecar is missing or degenerate: fall back
                     * to the fruit's own labels — a COPY, no vector is read
                     * (the labels are already there). */
                    int live = 0;
                    for (int i = 0; i < st.n_prev && i < n; i++)
                        if (a[i].anchor >= 0) live++;
                    if (live == st.n_prev && st.n_prev <= n) {
                        free(pa); free(pa2);
                        pa = (int *)calloc((size_t)st.n_prev, sizeof(int));
                        pa2 = (int *)calloc((size_t)st.n_prev, sizeof(int));
                        if (pa && pa2) {
                            for (int i = 0; i < st.n_prev; i++) {
                                pa[i] = a[i].anchor;
                                pa2[i] = a[i].anchor2;
                            }
                            tree_intact = 1;
                            prefix_from_fruit = 1;
                        }
                    }
                }
                if (!tree_intact) {
                    /* FAIL LOUD: the tree's labels are gone and the fruit
                     * carries none either. Re-deriving them here would mean
                     * reading 62k vectors inside the regrow — the coupling
                     * the owner's architecture forbids. The repair is
                     * explicit: POST /v1/state/restamp (re-place the fruit
                     * on the tree; the jsonl stays byte-identical). */
                    free(pa); free(pa2); pa = pa2 = NULL;
                    free(C); free(rows); free(rows2); free(perm);
                    rows = rows2 = NULL; perm = NULL; C = NULL;
                    tel->n_prev = st.n_prev;
                    tel->n_new = -1;
                    snprintf(tel->regrow_mode, sizeof(tel->regrow_mode), "no_tree");
                    rc = -3;
                    goto regrow_done;
                }
                if (rows && rows2 && perm) {
                    for (int i = 0; i < n; i++) {
                        if (i < st.n_prev) {
                            rows[i] = pa[i];
                            rows2[i] = pa2[i];
                        } else {
                            int t2[LZ_ANCH_MAXK];
                            int nb = (a[i].dim == dim)
                                ? anch_route(a[i].vec, C, K, dim, LZ_OVERLAP_TOPB, t2) : 0;
                            rows[i] = nb > 0 ? t2[0] : -1;
                            rows2[i] = nb > 1 ? t2[1] : -1;
                        }
                    }
                }
                free(pa); free(pa2); pa = pa2 = NULL;
                QueryPerformanceCounter(&t_route1);
                tel->ms_route = (double)(t_route1.QuadPart - t_route0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                if (rows && rows2 && perm && anch_perm(rows, n, K, perm) == 0) {
                    /* ISOLATED STAMP SIDECAR (2026-10-20, perm-sidecar
                     * architecture): the 222 MB jsonl is NOT rewritten —
                     * "apple stays in place, the branch moves". Only the
                     * NEW rows' anchor/anchor2 stamps go to a delta sidecar
                     * (<PATH_INDEX>.stamp): one 8-byte record per row
                     * [n_prev..n), appended atomically. Read path applies
                     * the sidecar over the jsonl stamps after load
                     * (lz_index_load). Sidecar contract with regrow_state:
                     * the state's n_prev tells which jsonl stamps are
                     * valid; anything >= n_prev comes from the sidecar. */
                    int wrote = 0;
                    LARGE_INTEGER t_rw0, t_rw1, t_sv0, t_sv1;
                    QueryPerformanceCounter(&t_rw0);
                    {
                        char sp[600];
                        snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
                        FILE *sf;
                        /* If we have no prior stamp file, or the sidecar
                         * doesn't cover [0..n_prev) yet, bootstrap it by
                         * appending ALL rows' stamps (n x 8 bytes, ~0.5 MB
                         * at 62k rows — one-time cost, never on the hot
                         * incremental path again). */
                        long base_ofs = -1;
                        {
                            struct stat ss;
                            if (stat(sp, &ss) == 0)
                                base_ofs = (ss.st_size - (long)st.n_prev * 8) / 8 >= 0
                                    ? st.n_prev * 8
                                    : -1;
                        }
                        /* TREE-SIDE LABELS (2026-10-20): the prefix labels
                         * are READ, never re-derived. So:
                         *  - if the prefix came from the tree's own sidecar,
                         *    the sidecar's prefix records are still the tree's
                         *    labels -> append only the new tail.
                         *  - if the prefix came from the fruit's copy (the
                         *    sidecar was missing/degenerate), the sidecar has
                         *    no valid prefix -> rewrite it whole so the tree
                         *    owns the labels again.
                         * Either way the jsonl is untouched: the fruit stays
                         * in place, only the tree's channel is written. */
                        if (prefix_from_fruit) base_ofs = -1;
                        if (base_ofs < 0) {
                            /* bootstrap full stamp set for all n rows */
                            FILE *b = fopen(sp, "wb");
                            if (b) {
                                for (int i = 0; i < n; i++) {
                                    int32_t rec[2] = { (int32_t)rows[i], (int32_t)rows2[i] };
                                    fwrite(rec, sizeof(rec), 1, b);
                                }
                                fclose(b);
                                wrote = 1;
                            }
                        } else {
                            sf = fopen(sp, "r+b");
                            if (sf) {
                                fseek(sf, st.n_prev * 8, SEEK_SET);
                                for (int i = st.n_prev; i < n; i++) {
                                    int32_t rec[2] = { (int32_t)rows[i], (int32_t)rows2[i] };
                                    fwrite(rec, sizeof(rec), 1, sf);
                                }
                                fclose(sf);
                                wrote = 1;
                            }
                        }
                    }
                    QueryPerformanceCounter(&t_rw1);
                    tel->ms_rewrite = (double)(t_rw1.QuadPart - t_rw0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                    if (wrote && ord_save(PATH_ORDER, perm, n, K, n,
                                          anch_cksum(C, K, dim)) == 0) {
                        RegrowState ns;
                        ns.n_prev = n;
                        strncpy(ns.last_sid, a[n - 1].sid, sizeof(ns.last_sid) - 1);
                        ns.last_sid[sizeof(ns.last_sid) - 1] = 0;
                        struct stat sns;
                        ns.index_mtime = 0; ns.index_size = 0;
                        if (stat(PATH_INDEX, &sns) == 0) {
                            ns.index_mtime = (long)sns.st_mtime;
                            ns.index_size = (long)sns.st_size;
                        }
                        ns.K = K;
                        ns.anchors_cksum = anch_cksum(C, K, dim);
                        QueryPerformanceCounter(&t_sv0);
                        regrow_state_save(&ns, 0.0);
                        QueryPerformanceCounter(&t_sv1);
                        tel->ms_save = (double)(t_sv1.QuadPart - t_sv0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                        rc = 0;
                        if (Kout) *Kout = K;
                    } else rc = -1;
                } else rc = -1;
                free(perm); free(rows); free(rows2);
                free(C);
                tel->n_prev = st.n_prev;
                tel->n_new = n - st.n_prev;
                snprintf(tel->regrow_mode, sizeof(tel->regrow_mode),
                         rc == 0 ? "incremental" : "error");
                goto regrow_done;
            }
full_pipeline:
            /* FULL: unchanged/append contract failed or a fresh base index
             * with no state. Original pipeline follows verbatim. */
            ;
            float *X = (float *)malloc((size_t)m * dim * sizeof(float));
            float *C = (float *)malloc((size_t)K * dim * sizeof(float));
            int *rows = (int *)malloc((size_t)n * sizeof(int));
            int *rows2 = (int *)malloc((size_t)n * sizeof(int));
            if (X && C && rows && rows2) {
                int w = 0;
                for (int i = 0; i < n; i++)
                    if (a[i].dim == dim) { memcpy(X + (size_t)w * dim, a[i].vec, (size_t)dim * sizeof(float)); w++; }
                QueryPerformanceCounter(&t_train0);
                if (anch_train(X, m, dim, K, C, NULL) == 0) {
                QueryPerformanceCounter(&t_train1);
                tel->ms_train = (double)(t_train1.QuadPart - t_train0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                if (anch_save_atomic(PATH_ANCH, C, K, dim, n) == 0) {
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
                        LARGE_INTEGER t_route0, t_route1;
                        QueryPerformanceCounter(&t_route0);
                        for (int i = 0; i < n; i++) {
                            if (a[i].dim != dim) { rows[i] = -1; rows2[i] = -1; continue; }
                            int nb = anch_route(a[i].vec, C, K, dim, LZ_OVERLAP_TOPB, t2);
                            rows[i] = nb > 0 ? t2[0] : -1;
                            rows2[i] = nb > 1 ? t2[1] : -1;
                        }
                        QueryPerformanceCounter(&t_route1);
                        tel->ms_route = (double)(t_route1.QuadPart - t_route0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                        uint32_t *perm = (uint32_t *)malloc((size_t)n * sizeof(uint32_t));
                        if (perm && anch_perm(rows, n, K, perm) == 0) {
                            /* H2: stamp the pair (table K/ntr/checksum bind) */
                            ord_save(PATH_ORDER, perm, n, K, n,
                                     anch_cksum(C, K, dim));
                            LARGE_INTEGER t_rw0, t_rw1, t_sv0, t_sv1;
                            QueryPerformanceCounter(&t_rw0);
                            /* TREE-SIDE LABELS, FRUIT UNTOUCHED (2026-10-20)
                             * The full regrow RETRAINS the tree: it reads the
                             * fruit's vectors to place every row (that is
                             * what training IS). But it must NOT REWRITE the
                             * fruit:
                             *   - the owner's architecture (HANDOFF-2026-10-10
                             *     -session.md §5): the fruit is a closed
                             *     cluster — "the tree may break, the fruit
                             *     stays the same, and can be put somewhere
                             *     else"; a regrow moves the tree only.
                             *   - the old jsonl rewrite re-serialized every
                             *     vector with "%.6g" (6 SIGNIFICANT digits)
                             *     while build_index.py wrote round(x, 6) (6
                             *     DECIMALS) — a LOSSY re-encoding of the
                             *     fruit. The bytes must not change.
                             * So the labels go to the TREE's channel
                             * (<index>.stamp, the same sidecar the
                             * incremental path uses) and the jsonl is left
                             * byte-identical. The read path overlays the
                             * sidecar when it covers n rows, so the served
                             * labels are the fresh ones either way. */
                            { char sp[600];
                              snprintf(sp, sizeof(sp), "%s.stamp", PATH_INDEX);
                              FILE *sf = fopen(sp, "wb");
                              int good = 0;
                              if (sf) {
                                  good = 1;
                                  for (int i = 0; i < n; i++) {
                                      int32_t rec[2] = { (int32_t)rows[i], (int32_t)rows2[i] };
                                      if (fwrite(rec, sizeof(rec), 1, sf) != 1) { good = 0; break; }
                                  }
                                  fclose(sf);
                              }
                              QueryPerformanceCounter(&t_rw1);
                              tel->ms_rewrite = (double)(t_rw1.QuadPart - t_rw0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                              if (!good) { free(perm); rc = -1; goto regrow_done; }
                            }
                            if (Kout) *Kout = K;
                            rc = 0;
                            /* full path proven; save fresh state.
                             * The index file itself was NOT rewritten (the
                             * fruit is untouched) — its stat is unchanged,
                             * so the state records the same mtime/size. */
                            struct stat sns;
                            QueryPerformanceCounter(&t_sv0);
                            if (stat(PATH_INDEX, &sns) == 0) {
                                RegrowState ns;
                                ns.n_prev = n;
                                strncpy(ns.last_sid, a[n - 1].sid, sizeof(ns.last_sid) - 1);
                                ns.last_sid[sizeof(ns.last_sid) - 1] = 0;
                                ns.index_mtime = (long)sns.st_mtime;
                                ns.index_size = (long)sns.st_size;
                                ns.K = K;
                                ns.anchors_cksum = anch_cksum(C, K, dim);
                                regrow_state_save(&ns, 0.0);
                            }
                            QueryPerformanceCounter(&t_sv1);
                            tel->ms_save = (double)(t_sv1.QuadPart - t_sv0.QuadPart) * 1000.0 / (double)qf.QuadPart;
                            free(perm);
                        }
                    } else rc = -1;
                } /* anch_save_atomic */
                } else rc = -1;
            }
            free(X); free(C); free(rows); free(rows2);
        }
        tel->n_prev = incremental ? st.n_prev : 0;
        tel->n_new = tel->n_prev ? (long)n - tel->n_prev : (long)n;
        snprintf(tel->regrow_mode, sizeof(tel->regrow_mode),
                 rc == 0 ? "full" : "error");
    } else if (n < LZ_ANCH_MIN) {
        remove(PATH_ANCH);
        remove(PATH_ORDER);
        remove(regrow_state_path());
    }
regrow_done:
    /* rc==0 covers full AND incremental; rc==2 is the unchanged fast path and
     * never invalidates (it wrote nothing).
     *
     * INVALIDATE ONLY WHAT ACTUALLY CHANGED (2026-10-22). A successful regrow
     * rewrites the anchors table and the order file but never the jsonl — the
     * fruit stays put — so the loader's index buffer is still valid: only the
     * route caches (which key on the anchors/order mtime) go. The fresh
     * stamps reach the buffer through the loader's own sidecar watcher.
     *
     * NEVER call lz_index_cache_invalidate() here. The loader's thread checks
     * mtime+size against the file on its own tick (loader_thread 853): a
     * stale or NULL cache is rebuilt without being told, and a self-parsed
     * run's rows are freed by this function, never by the cache path.
     * Invalidating was a THIRD parse of the same file: regrow_done fired it
     * right after regrow's own parse (both of the identical mtime/size), so
     * the loader re-parsed the exact bytes it already had. Live evidence:
     * ann_index.jsonl.loader.log regrow_done ts=...2623 followed by
     * loader_parse_start ts=...2624 with reload entries/mtime/size equal to
     * the previous reload's. */
    if (rc == 0) lz_route_cache_invalidate();
    /* PUBLISH THE FRESH PARSE (2026-10-22). When the borrow missed, the
     * regrow had to parse the jsonl ITSELF — that parse IS the current
     * index (same bytes the loader would parse next tick). Throwing it
     * away cost two things:
     *   1. a SECOND parse of the same file (the loader's tick re-parses
     *      the 258 MB index — ~50 s on this box) for bytes already parsed;
     *   2. a STALE SERVED BUFFER in between: the server kept answering
     *      with the old row count while the freshly trained route table
     *      carried the new one (antr != nent -> "routed=0" -> brute
     *      fallback, and the new rows were not served at all).
     * So the regrow's parse BECOMES the served buffer. The loader's tick
     * then sees the same mtime+size and does nothing — one parse, no
     * stale window. (The loader cannot swap concurrently: g_regrow_busy
     * is held for the whole regrow.) */
    if (rc == 0 && a && !borrowed) {
        struct stat sti;
        long mt = 0, sz = 0;
        if (stat(PATH_INDEX, &sti) == 0) { mt = (long)sti.st_mtime; sz = (long)sti.st_size; }
        EnterCriticalSection(&g_lock);
        LZEntry *old = g_idx_cache;
        int oldn = g_idx_n;
        g_idx_cache = a; g_idx_n = n;
        g_idx_mtime = mt; g_idx_size = sz;
        lz_tags_load(a, n);
        published = 1;
        if (old) lz_index_free(old, oldn);
        LeaveCriticalSection(&g_lock);
    }
    /* FRESH LABELS, FRESH COUNTER (2026-10-22). The regrow just wrote the
     * tree's labels (the sidecar) for all n rows, so the served labels and
     * the live-stamp counter must be true NOW — not on the loader's next
     * 1.5 s tick. Otherwise every query in that window reads a stale
     * (often empty) stamp set and the route is declared "dead-route"
     * (measured 2026-10-22 on a fresh full regrow: mode=dead-route while
     * the sidecar on disk carried all n live labels — the route fell back
     * to a full scan it did not need). The overlay is applied to the SAME
     * buffer the loader owns; the loader's own watcher would do the same
     * thing one tick later. */
    if (rc == 0) {
        EnterCriticalSection(&g_lock);
        if (g_idx_cache) g_stamp_live = lz_stamp_overlay(g_idx_cache, g_idx_n);
        LeaveCriticalSection(&g_lock);
    }
    if (a && !borrowed && !published) lz_index_free(a, n);
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
    /* the health probe must not wait for a starved sidecar: 2 s, then the
     * answer is honest ("embed":"down") instead of the whole probe hanging */
    int saved_to = g_http_timeout_ms;
    if (saved_to > 2000) g_http_timeout_ms = 2000;
    int embed_ok = (lz_embed("ping", (float[4]){0}, 1) >= 0) ? 1 : 0;
    g_http_timeout_ms = saved_to;
    /* TRUTHFUL ROUTE (2026-10-20): `routed` only means the anchor table and
     * the order file are valid and bound. It says NOTHING about whether the
     * ROWS carry routing stamps. A rebuilt index (scripts/build_index.py
     * writes rows without the anchor fields) leaves the table valid but the
     * routing table of every row empty — the server then scored 4 rows of
     * 62,204 and /health still said routed:1 (measured 2026-10-20, live
     * :8096). Report the stamp liveness separately and mark the service
     * degraded when the route is dead, so the watcher/operator can see it. */
    int stamps_live = g_stamp_live;
    int route_dead = (routed == 1) && stamps_route_dead(nent);
    /* the index path is a Windows path — escape it or the JSON is invalid
     * (\t, \a, \k are not legal escapes). */
    char esc[512];
    lz_json_escape(esc, sizeof(esc), PATH_INDEX);
    char b[1100];
    /* TAG SIDECAR truth (2026-10-21): a stale sidecar would silently narrow a
     * tag scope, so the health must say how many rows carry a tag and how
     * many are unlabeled (a sidecar older than the index). */
    char tescp[600];
    lz_json_escape(tescp, sizeof(tescp), g_tag_path[0] ? g_tag_path : "(none)");
    int tagged_rows = (nent > g_tag_unlabeled) ? (nent - g_tag_unlabeled) : 0;
    /* the loader's truth: "entries" is what is SERVED (the loaded buffer),
     * and "index_age_s" says how old that buffer is. A rebuild in flight
     * shows up as a growing age, never as a stalled request. */
    double age_s = (g_loaded_at_ms > 0.0) ? (lz_now_ms() - g_loaded_at_ms) / 1000.0 : -1.0;
    int bl = snprintf(b, sizeof(b),
        "{\"status\":\"%s\",\"service\":\"ann_search\",\"fork_of\":\"DWGLS 8c85f9f\","
        "\"index\":\"%s\",\"entries\":%d,\"routed\":%d,\"stamps_live\":%d,"
        "\"route_dead\":%d,\"maxk\":%d,\"embed\":\"%s\","
        "\"loader\":{\"mode\":\"%s\",\"index_age_s\":%.1f},"
        "\"tags\":{\"path\":\"%s\",\"kinds\":%d,\"tagged\":%d,\"unlabeled\":%d},"
        "\"fog\":{\"replayed\":%d,\"shut\":%d,\"free\":%u}}",
        (embed_ok && nent > 0) ? "ok" : "degraded",
        esc, nent, routed, stamps_live, route_dead, ANN_MAXK, embed_ok ? "ok" : "down",
        g_loader_running ? "bg" : "inline", age_s,
        tescp, g_tag_n, tagged_rows, g_tag_unlabeled,
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

/* ── batch-query forward decls (implementation below, additive) ── */
static int lv_search_json(const float *qv, int qd, int topk, char *out, int out_cap, int *out_len);
static int handle_search_batch(sock_t cli, const char *rq);
static int handle_search(sock_t cli, const char *rq, char *out, int out_cap, int *out_len) {
    char *bd3 = strstr(rq, "\r\n\r\n");
    if (!bd3) { sock_close(cli); return 0; }
    bd3 += 4;
    char sq[2048] = "";
    int stopk = 3;
    char sid_filter[64] = "";
    char scope_buf[256] = "";
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
    {
        const char *scp = lz_json_val(bd3, "scope");
        if (scp && *scp == '"') {
            /* unescape like the query text does: a file tag is free text and
             * arrives as \uXXXX (Python) or with escapes (any JSON writer).
             * Reading it raw silently matched NOTHING — the scope then fell
             * through to its kind (measured: 718-row group reported as
             * 29153 kind rows for a Thai tag). */
            scp++;
            lz_json_unescape(scope_buf, (int)sizeof(scope_buf), scp);
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
    /* a scope is validated BEFORE searching: unknown tag -> 400
     * scope_not_found, a tag too thin to hold topk -> 400 scope_too_narrow.
     * Never fall through to an unscoped answer under a scoped request. */
    if (scope_buf[0]) {
        LZEntry *sve = NULL;
        int svn = 0, fine_n = 0, kind_n = 0;
        char spat[256], esc[600];
        if (!lz_index_cached(&sve, &svn)) {
            const char *eb = "{\"status\":\"error\",\"error\":\"index_unavailable\","
                             "\"detail\":\"the index could not be loaded\"}";
            lz_http_send(cli, "503 Service Unavailable", "application/json", eb, (int)strlen(eb));
            sock_close(cli);
            return 0;
        }
        int sr = lz_scope_resolve(sve, svn, scope_buf, stopk, spat, sizeof(spat),
                                  &fine_n, &kind_n, NULL, NULL);
        if (sr <= 0) {
            char eb[512];
            lz_json_escape(esc, sizeof(esc), scope_buf);
            int el = snprintf(eb, sizeof(eb),
                "{\"status\":\"error\",\"error\":\"%s\","
                "\"scope\":\"%s\",\"fine\":%d,\"kind\":%d,\"topk\":%d}",
                sr == 0 ? "scope_not_found" : "scope_too_narrow",
                esc, fine_n, kind_n, stopk);
            lz_http_send(cli, "400 Bad Request", "application/json", eb, el);
            sock_close(cli);
            return 0;
        }
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
        int items_scored = 0, nent = 0, budget = 0, truncated = 0, fallback = 0;
        double us_per_item = 0;
        LZFog fog = { (int)WL_FREE, 0, 0, 0 };
        lz_mem_search(qv, qd, sid_filter, scope_buf, stopk,
            top_sid, top_file, top_np, top_sc,
            route_mode, route_bk, &route_nb,
            &block_reads, &items_scored, &nent,
            &budget, &truncated, &us_per_item, &fog, &fallback);
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
            "],\"block_reads\":%d,\"items_scored\":%d,\"items_total\":%d,\"us_per_item\":%.2f,\"budget\":%d,\"truncated\":%d,\"fallback\":%d,",
            block_reads, items_scored, nent, us_per_item, budget, truncated, fallback);
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
    /* stream the output if a caller buffer was supplied (search-batch uses
     * this to emit one query's JSON into a shared buffer without the per-
     * query 16 KB buffer; without a caller buffer nothing changes). */
    if (out_cap > 0 && out) {
        if (srlen < out_cap - 1) { memcpy(out, sresp, (size_t)srlen); out[srlen] = '\0'; }
        else { memcpy(out, sresp, (size_t)(out_cap - 1)); out[out_cap - 1] = '\0'; }
        if (out_len) *out_len = srlen;
        return 1;
    }
    lz_http_send(cli, "200 OK", "application/json", sresp, srlen);
    sock_close(cli);
    return 0;
}

/* per-connection critical section (pre-existing, shared). */
static CRITICAL_SECTION g_lock;

/* ── (additive) lv_search_json — one query, NO HTTP (batch holds g_lock) ──
 * Materialize the float vector into a minimal synthetic search request and
 * call handle_search with a caller buffer; cli=-1 is never touched on the
 * streaming path (errors return before any socket use). */
static int lv_search_json(const float *qv, int qd, int topk, char *out, int out_cap, int *out_len) {
    static char mini_rq[24576];
    int used = snprintf(mini_rq, sizeof(mini_rq),
        "POST /v1/state/search HTTP/1.1\r\nContent-Length: 0\r\n\r\n");
    int n2 = snprintf(mini_rq + used, sizeof(mini_rq) - used, "{\"vec\":[");
    used += n2;
    for (int j = 0; j < qd; j++) {
        n2 = snprintf(mini_rq + used, sizeof(mini_rq) - used, "%s%.9g", j ? "," : "", (double)qv[j]);
        used += n2;
        if (used >= (int)sizeof(mini_rq) - 4) break;
    }
    n2 = snprintf(mini_rq + used, sizeof(mini_rq) - used, "],\"topk\":%d}", topk);
    used += n2;
    int olen = 0;
    handle_search(-1, mini_rq, out, out_cap, &olen);
    if (out_len) *out_len = olen;
    return olen;
}

/* ── POST /v1/state/search-batch (additive) ──────────────────────────
 * BATCH QUERY endpoint: {"queries":["a","b",...],"k":3} (or "topk").
 * ONE POST to :18095 /v1/embeddings for the whole input array (std OpenAI
 * shape — the same sidecar the index build uses; measured B=8 4.0 ms/ea
 * vs 12.3 ms per single string), then ALL N queries are scanned under ONE
 * g_lock acquisition (the amortized scan). Shape of each result object ==
 * today's single-query /v1/state/search body; top level adds
 * {"telemetry":{"n","embed_ms","took_ms"}}. Cap N<=32 — malformed / empty /
 * oversized / bad-k -> 400 with an "error" field. */
static int handle_search_batch(sock_t cli, const char *rq) {
    char *bd3 = strstr(rq, "\r\n\r\n");
    if (!bd3) { sock_close(cli); return 0; }
    bd3 += 4;
    int bq_cap = 32;   /* hard cap; N>32 -> 400 */
    static char qs[32][2048];
    int nq = 0;
    const char *bq = strstr(bd3, "\"queries\"");
    int has_queries = 0;
    if (bq) {
        bq = strchr(bq + 8, ':');
        if (bq) {
            bq++;
            while (*bq && (*bq == ' ' || *bq == '\t')) bq++;
            if (*bq == '[') {
                bq++;
                has_queries = 1;
                while (nq < bq_cap) {
                    while (*bq == ' ' || *bq == '\t' || *bq == ',') bq++;
                    if (*bq != '"') break;
                    bq++;
                    char *dst = qs[nq];
                    lz_json_unescape(dst, 2048, bq);
                    nq++;
                    while (*bq && *bq != '"') { if (*bq == '\\' && bq[1]) bq++; bq++; }
                    if (*bq == '"') bq++;
                }
            }
        }
    }
    int overflow = 0;
    /* STRING-AWARE overflow count: walk the raw body from the '[' that
     * starts the queries array and count every real string member by
     * decoding escapes; quarantined commas INSIDE a string never count. */
    if (has_queries) {
        int extra = 0;
        const char *cap_arr = strchr(bd3, '[');
        if (cap_arr) {
            const char *w = cap_arr + 1;
            while (*w) {
                while (*w == ' ' || *w == '\t' || *w == ',') w++;
                if (*w == ']') break;
                if (*w != '"') break;
                /* count this member: exactly one string-token boundary */
                if (nq + extra >= bq_cap) { extra++; break; }   /* beyond cap: overflow */
                w++;
                while (*w && *w != '"') { if (*w == '\\' && w[1]) w++; w++; }
                if (*w == '"') w++;
                else break;                                /* unterminated string -> treat as non-member */
            }
        }
        if (extra > 0) overflow = 1;
    }
    if (!has_queries || overflow) {
        const char *eb = overflow
            ? "{\"status\":\"error\",\"error\":\"too_many_queries\",\"cap\":32}"
            : "{\"status\":\"error\",\"error\":\"missing_queries\",\"hint\":\"send {\\\"queries\\\":[..],\\\"k\\\":3} (cap 32)\"}";
        lz_http_send(cli, "400 Bad Request", "application/json", eb, (int)strlen(eb));
        sock_close(cli);
        return 0;
    }
    if (nq == 0) {
        const char *eb = "{\"status\":\"error\",\"error\":\"empty_queries\"}";
        lz_http_send(cli, "400 Bad Request", "application/json", eb, (int)strlen(eb));
        sock_close(cli);
        return 0;
    }
    int bk = lz_json_int(bd3, "topk", -1);
    if (bk < 0) bk = lz_json_int(bd3, "k", 3);
    if (bk < 1) bk = 1;
    if (bk > ANN_MAXK) {
        char eb[160];
        int el = snprintf(eb, sizeof(eb),
            "{\"status\":\"error\",\"error\":\"topk_out_of_range\",\"requested\":%d,\"max\":%d}", bk, ANN_MAXK);
        lz_http_send(cli, "400 Bad Request", "application/json", eb, el);
        sock_close(cli);
        return 0;
    }
    /* ONE embed round-trip for the whole batch. */
    static float qvs[32][1024];
    static int qdd[32];
    int embed_ok = 0;
    {
        static const char *sq[32];
        for (int qi = 0; qi < nq; qi++) sq[qi] = qs[qi];
        float *r = lz_embed_batch((const char * const *)sq, nq,
                                  (float *)qvs, qdd, 1024, &embed_ok);
        (void)r;
        if (!embed_ok) {
            /* fallback path: per-string via the existing single-string path */
            for (int qi = 0; qi < nq; qi++) {
                static float t[1024];
                int d = lz_embed(qs[qi], t, 1024);
                if (d > 0) { memcpy(qvs[qi], t, sizeof(float) * d); qdd[qi] = d; }
                else qdd[qi] = 0;
            }
        }
    }
    /* lock ONCE for the whole batch (the amortized scan). */
    static char out_buf[76800];
    int olen = 0;
    LARGE_INTEGER qf, qt0, qt1;
    QueryPerformanceFrequency(&qf);
    QueryPerformanceCounter(&qt0);
    double emb_ms = g_last_embed_ms;
    lz_lock_get();
    static char row_buf[16384];
    for (int qi = 0; qi < nq; qi++) {
        row_buf[0] = '\0';
        int chunk = 0;
        if (qdd[qi] > 0) {
            int rlen = 0;
            chunk = lv_search_json(qvs[qi], qdd[qi], bk,
                                   row_buf, (int)sizeof(row_buf) - 1, &rlen);
            if (chunk > 0) chunk = rlen > 0 ? rlen : chunk;
        }
        const char *row = row_buf;
        if (chunk <= 0) {   /* embed failing or buffer cap: emit an explicit error object */
            row = "{\"status\":\"error\",\"error\":\"embed_unavailable\"}";
            chunk = (int)strlen(row);
        }
        if (olen > 0 && olen < (int)sizeof(out_buf) - 1) { out_buf[olen++] = ','; }
        if (olen + chunk >= (int)sizeof(out_buf) - 1) break;   /* cap: emit what we have */
        memcpy(out_buf + olen, row, (size_t)chunk);
        olen += chunk;
        out_buf[olen] = '\0';
    }
    QueryPerformanceCounter(&qt1);
    double took_ms = (double)(qt1.QuadPart - qt0.QuadPart) * 1000.0 / (double)qf.QuadPart;
    lz_lock_put();
    static char bresp[81920];
    int brlen = snprintf(bresp, sizeof(bresp),
        "{\"status\":\"ok\",\"telemetry\":{\"n\":%d,\"embed_ms\":%.2f,\"took_ms\":%.2f},\"results\":[",
        nq, emb_ms, took_ms);
    brlen += snprintf(bresp + brlen, sizeof(bresp) - brlen, "%.*s", olen, out_buf);
    brlen += snprintf(bresp + brlen, sizeof(bresp) - brlen, "]}");
    lz_http_send(cli, "200 OK", "application/json", bresp, brlen);
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

static void dispatch(sock_t cli, char *rq) {
    char method[16] = "", path[256] = "";
    sscanf(rq, "%15s %255s", method, path);
    if (strcmp(method, "GET") == 0 && strcmp(path, "/health") == 0) {
        handle_health(cli);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/restamp") == 0) {
        /* RESTAMP (2026-10-20): repair the ROUTING TABLE without touching
         * the vectors. The full regrow path rewrites the 233 MB jsonl and
         * re-serializes every vector with "%.6g" — a lossy re-encoding of
         * the values build_index.py wrote (round(x, 6) = 6 DECIMALS vs
         * %.6g = 6 SIGNIFICANT digits). The vectors must not change, so the
         * repair re-routes the rows against the existing centroids and
         * writes the stamps into the sidecar only.
         * 0 = repaired, -1 = no usable anchor table (caller: full regrow),
         * -2 = allocation failure. */
        LARGE_INTEGER rqf2, rq02, rq12;
        QueryPerformanceFrequency(&rqf2);
        QueryPerformanceCounter(&rq02);
        int rN2 = 0, rK2 = 0;
        int rrc2 = lz_restamp(&rN2, &rK2);
        QueryPerformanceCounter(&rq12);
        double rms2 = (double)(rq12.QuadPart - rq02.QuadPart) * 1000.0 / (double)rqf2.QuadPart;
        if (rrc2 == 0) {
            lz_index_cache_invalidate();
            lz_route_cache_invalidate();
        }
        char rresp2[512];
        int rrlen2 = snprintf(rresp2, sizeof(rresp2),
            "{\"status\":\"%s\",\"mode\":\"restamp\",\"nent\":%d,\"K\":%d,"
            "\"ms\":%.1f,\"restamp\":{\"mode\":\"%s\",\"rc\":%d}}",
            (rrc2 == 0) ? "ok" : "error", rN2, rK2, rms2,
            (rrc2 == 0) ? "restamp" : (rrc2 == -1) ? "no_table" : "error",
            rrc2);
        lz_http_send(cli, (rrc2 == 0) ? "200 OK" : "409 Conflict",
                     "application/json", rresp2, rrlen2);
        sock_close(cli);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/regrow") == 0) {
        /* REGROW MODES: unchanged (<0.5s) / incremental (append) / full.
         * body "force":1 -> full path regardless of state. Telemetry keys are
         * ADDITIVE only — existing keys (status, mode, nent, K) keep their
         * legacy meanings: mode stays "anchor" on a trained regrow, "brute"
         * below-min, and the new regrow_mode carries the pressed mode. */
        char *rbody = strstr(rq, "\r\n\r\n");
        int force = 0;
        if (rbody && strstr(rbody, "\"force\"")) {
            if (lz_json_int(rbody, "force", 0) == 1) force = 1;
        }
        int rK = 0, rN = 0;
        RegrowTelemetry rt;
        memset(&rt, 0, sizeof(rt));   /* load_src is read in the response */
        LARGE_INTEGER rqf, rq0, rq1;
        QueryPerformanceFrequency(&rqf);
        QueryPerformanceCounter(&rq0);
        /* THE REGROW IS HEAVY (2026-10-22): it parses the 258 MB index,
         * trains the anchors and rewrites the tree's channel — ~90 s on this
         * box. Under the request lock that stalls EVERY request: measured
         * live, /health timed out (30 s) three times during a scheduled
         * regrow while the index rebuild had already been moved off the
         * request path.
         * The heavy work touches only files and its own buffers, so the
         * lock is RELEASED around it and taken again for the answer. The
         * route stays consistent: the anchor table is checksum-bound to the
         * row count, so a concurrent index change makes the pair refuse
         * (fail loud), never route by labels that belong elsewhere. */
        int lock_released = 0;
        lz_diag("regrow_release ts=%lld depth=%d\n", (long long)time(NULL), g_lock_depth);
        while (g_lock_depth > 0) { LeaveCriticalSection(&g_lock); g_lock_depth--; lock_released++; }
        g_regrow_busy = 1;
        /* the regrow is a background job on a 2-core box: run it below
         * normal so the service stays responsive while it trains */
        DWORD old_prio = GetThreadPriority(GetCurrentThread());
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
        int rrc = lz_anchor_refresh(force, &rK, &rN, &rt);
        SetThreadPriority(GetCurrentThread(), old_prio);
        g_regrow_busy = 0;
        while (lock_released-- > 0) { EnterCriticalSection(&g_lock); g_lock_depth++; }
        lz_diag("regrow_done ts=%lld depth=%d\n", (long long)time(NULL), g_lock_depth);
        QueryPerformanceCounter(&rq1);
        double rms = (double)(rq1.QuadPart - rq0.QuadPart) * 1000.0 / (double)rqf.QuadPart;
        /* rc 0=full/incremental regrow, 1=brute<n/null, 2=unchanged fast path,
         * -2=race (index changed mid-regrow; operator should re-trigger). */
        const char *rmode = (rrc == 0 || rrc == 2) ? "anchor"
                          : (rrc == 1) ? "brute"
                          : (rrc == -2) ? "race" : "error";
        char rresp[768];
        int rrlen = snprintf(rresp, sizeof(rresp),
            "{\"status\":\"%s\",\"mode\":\"%s\",\"nent\":%d,\"K\":%d,"
            "\"regrow\":{\"mode\":\"%s\",\"ms\":%.1f,\"skipped\":%d,"
            "\"n_prev\":%d,\"n_new\":%d,\"internal_mode\":\"%d\","
            "\"load_src\":\"%s\","
            "\"ms_per_phase\":{\"load\":%.1f,\"train\":%.1f,\"route\":%.1f,"
            "\"rewrite\":%.1f,\"save\":%.1f}}}",
            rrc < 0 ? "error" : "ok", rmode, rN, rK,
            rt.regrow_mode, rms, rt.skipped,
            (int)rt.n_prev, (int)rt.n_new, rrc,
            rt.load_src[0] ? rt.load_src : "none",
            rt.ms_load, rt.ms_train, rt.ms_route, rt.ms_rewrite, rt.ms_save);
        lz_http_send(cli, "200 OK", "application/json", rresp, rrlen);
        sock_close(cli);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/search") == 0) {
        handle_search(cli, rq, NULL, 0, NULL);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/v1/state/search-batch") == 0) {
        handle_search_batch(cli, rq);
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
        LARGE_INTEGER dqf, dq0, dq1;
        QueryPerformanceFrequency(&dqf);
        QueryPerformanceCounter(&dq0);
        lz_lock_get();
        QueryPerformanceCounter(&dq1);
        double wait_ms = (double)(dq1.QuadPart - dq0.QuadPart) * 1000.0 / (double)dqf.QuadPart;
        dispatch(cli, rq);
        lz_lock_put();
        if (wait_ms > 1000.0) {
            lz_diag("lock_wait ts=%lld ms=%.0f\n", (long long)time(NULL), wait_ms);
            fprintf(stderr, "[ann_search] lock wait %.0f ms (path held the lock)\n",
                    wait_ms);
        }
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
    { const char *te = getenv("LZ_HTTP_TIMEOUT_MS");
      if (te && atoi(te) > 0) g_http_timeout_ms = atoi(te); }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { fprintf(stderr, "wsa fail\n"); return 1; }
    InitializeCriticalSection(&g_lock);
    lz_fog_init();
    { LZEntry *e; int n; int c = lz_index_cached(&e, &n);
      g_loaded_at_ms = lz_now_ms();
      /* the background loader owns every later reload: a rebuild of the
       * 258 MB index must never block a request (measured 2026-10-21: the
       * inline reload held the lock >60 s and /health timed out) */
      g_loader_running = 1;
      g_loader_thread = CreateThread(NULL, 0, index_loader_thread, NULL, 0, NULL);
      fprintf(stderr, "[ann_search] index=%s entries=%d cache=%d maxk=%d loader=bg\n",
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

