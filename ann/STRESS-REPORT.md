# ANN semantic-search stack — stress-test report

> **FIX STATUS (2026-10-08, after this report):** the six highest-impact findings
> were fixed in `ann/src/ann_search.c` and deployed to :8096 —
> **#1** JSON `\uXXXX`/escape decoding in the query parser (was wrong Thai top-1),
> **#2** response JSON escaping, **#3** regrow writes escaped JSON (no corruption),
> **#4** per-connection thread + 5s socket timeout (an idle connection no longer
> freezes the service), **#5** `/health` now probes the embed sidecar
> (`status:ok|degraded`, `embed:ok|down`), **#6/#7** embed failure → `503`, missing
> query → `400`. Verified ad-hoc (10/10) and live (Thai via chat-pool `/semantic`
> now matches raw UTF-8). Still open, lower severity: **#9** stale cache on
> same-second rewrite, **#10** 2047-char truncation, **#11** topk coercion,
> **#12–15** telemetry/hygiene.
>
> **#8 — VERIFIED, AND THE FIRST FIX WAS WRONG (corrected 2026-10-08).** The
> first attempt set `SO_REUSEADDR` *then* `SO_EXCLUSIVEADDRUSE`. On Windows the
> two are **mutually exclusive**: the exclusive `setsockopt` fails
> `WSAEINVAL(10022)` and the port stays shareable, so two instances still bound
> `:8096` (reproduced against the shipped binary: both processes alive, split
> brain). The correct fix is **`SO_EXCLUSIVEADDRUSE` alone** (no `SO_REUSEADDR`
> on Windows); the second bind then fails `WSAEADDRINUSE(10048)` and the process
> exits 1. Receipts: `I:\tmp\verify8_sock.c` (three socket regimes) and
> `I:\tmp\ann_v8b\bind_test.py` (A/B against the real binary). Non-Windows keeps
> `SO_REUSEADDR` (fast restart, no split-brain there).

**Target:** `I:/tools/ann` (standalone ANN server, fork of DWGLS `8c85f9f`)
**Box:** Windows 10.0.19045 (MSYS_NT-10.0-19045), x86_64, gcc 8.1.0, Python 3.14.7, curl 8.18.0
**Live service under test:** `ann_search.exe` PID 9116 on `0.0.0.0:8096`; embed sidecar
`llama-server.exe` PID 2456 on `127.0.0.1:18095`; index `data/memcore/kv_index.jsonl`
(57,076 entries, dim 384). Binary `build/ann_search.exe` built 2026-10-08 10:43.

**Scope:** diagnosis only. No code, config, index, or service was modified. All mutating
experiments were run against throwaway copies of the sample index on scratch ports
(8097–8116) under `I:\tmp\ann_probe\`; the live `:8096` service and the repo data were
never written to. (Note: `ann/src/ann_search.c` shows as modified in git — a `vec`-input
feature — but that edit was made by another process during this session, **not** by this
test; the running binary predates it, and no experiment here wrote any repo file.)

**Method:** raw-socket HTTP fuzzing of the two endpoints; independent NumPy-free cosine
oracle over the full 57,076-vector index for recall; scratch-instance runs of
`ann_search.exe` with hand-built indexes for the parser/escaping/regrow cases.

---

## Severity summary

| # | Finding | Severity |
|---|---|---|
| 1 | JSON string escapes (`\uXXXX`, `\n`, `\t`, `\"`, `\\`) are **not decoded** → wrong query vector | **Critical (correctness)** |
| 2 | Response JSON is **not escaped** → server emits invalid JSON / response injection | **High** |
| 3 | `POST /v1/state/regrow` rewrites the index with raw `fprintf` → **permanent index corruption** | **High (data loss)** |
| 4 | Single-threaded blocking server, no socket timeout → **one idle connection freezes the service** | **High (DoS)** |
| 5 | `/health` reports `ok` with a **dead embed sidecar** → supervisor can't see a broken search path | **Medium** |
| 6 | All search failures return **HTTP 200** (`{"error":"embed unavailable"}`) | **Medium** |
| 7 | Missing/empty `query` → connection closed with **no HTTP response** | **Medium** |
| 8 | `SO_REUSEADDR` lets a second instance bind the **same port** on Windows | **Medium** |
| 9 | Index cache keyed on (mtime-seconds, size) → **stale reads** after a same-second same-size rewrite | **Medium** |
| 10 | Query **silently truncated at 2047 chars**; sidecar 500 at >512 tokens surfaces as generic error | **Medium** |
| 11 | `topk` parsing: silent coercion of `0`/negatives/garbage; first-substring-wins enables **topk override** | **Medium** |
| 12 | "Budget/truncated" telemetry is misleading (always `truncated:1` on a full scan) | **Low** |
| 13 | Fog latch is write-only; route log grows unbounded and is never compacted | **Low** |
| 14 | `test_anchor_determinism` fails (carried upstream bug, reproducible here) | **Low (known)** |
| 15 | HTTP hygiene: no HEAD, no keep-alive, no version negotiation, malformed JSON accepted | **Low** |

---

## 1. CRITICAL — JSON string escapes are not decoded (wrong results for standard clients)

The `query` value parser (`handle_search`, `ann_search.c` L587-603) copies the raw bytes
between the quotes and, on seeing `\`, **skips the backslash and keeps the next character
literally**. It does not decode `\uXXXX`, `\n`, `\t`, `\"`, `\\`, etc. A standard JSON
client (Python `json.dumps`, `curl` with a `\u`-escaped body, most HTTP libraries) sends
non-ASCII as `\uXXXX`, so the server embeds a **mangled** string.

### Reproduction (against live `:8096`)

```bash
# (a) standard client: non-ASCII sent as \uXXXX escapes
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"exchange hcom \u0e2a\u0e37\u0e48\u0e2d\u0e2a\u0e32\u0e23\u0e02\u0e49\u0e32\u0e21 session","topk":1}' \
  http://127.0.0.1:8096/v1/state/search
# observed:
# {"status":"ok","hits":[{"sid":"0bad043ffdd9d6338cce77460760a3d4",...,"score":0.8903}],...}

# (b) raw UTF-8 (what the server expects)
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"exchange hcom สื่อสารข้าม session","topk":1}' \
  http://127.0.0.1:8096/v1/state/search
# observed:
# {"status":"ok","hits":[{"sid":"d445d5f79249f41b7fdf1a454e3667e4",...,"score":0.9168}],...}
```

The independent cosine oracle (embed the *real* query, exact cosine over all 57,076 rows)
returns `d445d5f79249f41b7fdf1a454e3667e4` @ 0.9168 as the true nearest. Form (a) returns a
different, lower-scoring document — because the server actually embedded the mangled text
`exchange hcom u0e2au0e37u0e48u0e2du0e2au0e32u0e23u0e02u0e49u0e32u0e21 session`
(backslashes stripped, `u`/hex kept). Confirmed end-to-end: oracle over that mangled string
reproduces the server's exact pick and score (`0bad0...`, 0.8903).

### Impact

- **8/8** Thai queries tested (the memcore corpus is heavily Thai) returned the **wrong
  top-1** when sent as escaped JSON and the **correct** top-1 when sent as raw UTF-8:
  `match@1 = False` for every escaped query, `True` for every raw query.
- Any client that sends non-ASCII via `\uXXXX` (i.e. the default of most JSON libraries)
  silently gets degraded/incorrect semantic search.
- The same flaw corrupts ordinary escapes: `\n`, `\t`, `\"`, `\\` are not decoded, so
  "alpha\nbeta" (literal `\n` in the JSON text) embeds differently from a real newline.

### Exact repro for the escape class (scratch, no live impact)

`I:\tmp\ann_probe\escape_bug.py` / `escape_bug2.py`; results:

```
sent as \uXXXX  -> top1: [('0bad043ffdd9d6338cce77460760a3d4', 0.8903)]
sent as raw UTF8 -> top1: [('d445d5f79249f41b7fdf1a454e3667e4', 0.9168)]
oracle           -> top1: (0.9168314833064376, 'd445d5f79249f41b7fdf1a454e3667e4')
```

---

## 2. HIGH — Response JSON is not escaped (invalid JSON / response injection)

`handle_search` writes `top_sid` and `top_file` into the response with `snprintf`
(`ann_search.c` L649-651) using no escaping. If any indexed `sid`/`file` contains a `"` or
`\`, the response body is **invalid JSON**. `lz_json_escape` exists (used by `/health`) but
is not applied here.

### Reproduction (scratch instance, index with a quoted field)

`I:\tmp\ann_probe\escape_test2.py` builds a 1-entry-forced index whose top hit has
`file='evil"file\name.md'`, `sid='evil"sid'`, then queries it:

```
raw: {"status":"ok","hits":[{"sid":"evil\","file":"evil\","npast":400,"score":1.0000}],...}
>>> INVALID JSON: Expecting ',' delimiter: line 1 column 40 (char 39)
```

The server emits `"sid":"evil\",` — an escaped-looking but unterminated string. A strict
JSON client fails to parse the whole response.

Note: `scripts/build_index.py` sanitises `"`→`'` and `\`→`/` before writing
(`clean_field`), so the *shipped* memcore/sample corpora are safe. But the server itself
does no escaping, so any other index (or a field injected via regrow — see §3) breaks the
response contract.

---

## 3. HIGH — `regrow` rewrites the index with raw `fprintf` (permanent corruption)

`lz_anchor_refresh` (`ann_search.c` L524-529) rewrites `PATH_INDEX` with
`fprintf(ix, "{\"sid\":\"%s\",\"file\":\"%s\",...}", a[i].sid, a[i].file, ...)` — no JSON
escaping, and it writes the *parsed* field values straight back. A `"` or `\` in any field
is written unescaped and **corrupts the index file on disk**; the entry then silently
disappears on the next load.

### Reproduction (scratch instance)

`I:\tmp\ann_probe\regrow_test.py`: build a 40-entry base index containing
`file='path/with"quote.md'`, POST `/v1/state/regrow`, then validate the file:

```
regrow: {"status":"ok","mode":"anchor","nent":40,"K":4}
CORRUPT line 4: Expecting ',' delimiter: line 1 column 60 (char 59)
  {"sid":"doc_action_plan_2026_08_22_3","file":"path/with\",",...
rewritten index: 40 lines, 1 unparseable
health after regrow: {...,"entries":40,...}   # server still counts 40; the bad line is dropped on parse
```

The rewritten line is permanently malformed JSON. This is a data-loss path: regrow is the
documented way to make a fresh `build_index.py` output routable (README "memcore → ANN"),
and it silently corrupts any field containing a quote or backslash.

---

## 4. HIGH — Single-threaded blocking server; one idle connection freezes everything

`main` (`ann_search.c` L707-733) `accept()`s one connection and services it inline.
`lz_http_recv` (L67-88) loops on `recv` with **no timeout**; a client that opens a
connection and sends nothing (or promises a `Content-Length` and sends no body) parks the
accept loop indefinitely.

### Reproduction (against live `:8096`)

`I:\tmp\ann_probe/dos.py`:

```
===== BASELINE /health latency =====
  health 3.4 ms
  health 0.5 ms
===== DoS: half-open connection holds the single accept loop =====
  opened a connection that promises Content-Length: 1000 but sends no body; holding it...
  /health while slow-loris held: 6.00s -> TimeoutError('timed out')
  >>> SERVER BLOCKED by the half-open connection (single-threaded accept loop)
  /health after release: 0.00s -> 200 OK
```

Also confirmed with a fully silent connection (connect, send 0 bytes):

```
/health with an idle (0-byte) connection open: 5.00s -> TimeoutError('timed out')
>>> BLOCKED
/health after releasing: 0.00s -> 200 OK
```

One attacker connection (or one hung client, or one slow reader) takes the entire search
service — including `/health` — offline for as long as it is held. There is no
per-connection timeout, no thread/select pool, and no backlog protection.

Concurrency is therefore serialized: 8 parallel searches take ~0.57 s wall (≈ 8× one
search); a single held connection stalls all of them.

---

## 5. MEDIUM — `/health` is green with a dead embed sidecar (false health)

`handle_health` (L554-577) only reports index/route/fog state. It never probes the embed
sidecar, yet every search depends on it. With the sidecar down, `/health` still returns
`status:ok`, so the supervisor (`ann_keepalive.py`) sees a healthy service while search is
100 % broken.

### Reproduction (scratch instance, sidecar pointed at a dead port)

`I:\tmp\ann_probe/dead_embed.py` (starts `ann_search.exe` with `LZ_EMBED_PORT=18099`):

```
GET /health -> HTTP 200
  {"status":"ok",...,"entries":2881,...}      <-- green, but...
POST /v1/state/search -> HTTP 200
  {"error":"embed unavailable"}                <-- every query fails
```

`ann_keepalive.py` `_healthy()` only checks `{status: ok}` on `:8096`, so it will never
restart or flag a broken embedder — exactly the failure mode the supervisor was written to
prevent.

---

## 6. MEDIUM — All search failures return HTTP 200

`handle_search` returns `200 OK` for the embed-failure path (`L665-668`) and for the
success path. Clients cannot distinguish success from failure by status code.

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST -H "Content-Type: application/json" \
  -d '{"query":"<6000 chars>","topk":3}' http://127.0.0.1:8096/v1/state/search
# HTTP 200
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"<6000 chars>","topk":3}' http://127.0.0.1:8096/v1/state/search
# {"error":"embed unavailable"}
```

Contrast with the one genuinely fail-loud path: `topk>32` correctly returns
`400 topk_out_of_range`. Errors are inconsistent.

---

## 7. MEDIUM — Missing/empty `query` → no HTTP response at all

If the body has no `"query"` (or an empty one), `handle_search` does
`sock_close(cli); return 0;` (`L604`) **without sending any status line**. The client sees a
connection closed with zero bytes — not a `400`.

```bash
curl -s -i -X POST -H "Content-Type: application/json" -d '{"topk":3}' \
  http://127.0.0.1:8096/v1/state/search
# (no output — server closes the socket with no response)
```

Raw socket confirms `bytes=0` for both missing and empty `query`. `urllib` raises
`RemoteDisconnected`. A well-behaved client cannot tell this apart from a network failure.

---

## 8. MEDIUM — `SO_REUSEADDR` allows two instances on the same port

`main` sets `SO_REUSEADDR` (`L695`) with no `SO_EXCLUSIVEADDRUSE`. On Windows this lets a
second `ann_search.exe` **also bind the same port**, so a stale/duplicate instance can
silently steal traffic instead of failing to start.

### Reproduction (scratch)

`I:\tmp\ann_probe/infra2.py`:

```
=========== C) two instances, SAME port (SO_REUSEADDR) ===========
  inst1 health: {"status":"ok",...}
  inst2 alive (bound same port): True
  >>> SO_REUSEADDR let a second process bind :8104 (no bind-failure exit)
  inst2 stderr: (empty)
```

Two processes bind `:8104` with no error; which one answers a given connection is
undefined. The keepalive supervisor's "never kill a healthy server — we only ever START"
policy (README) makes duplicate starts plausible (e.g. two `--once` runs racing), so this
is a real split-brain risk.

### Resolution (verified 2026-10-08)

Fixed by binding with **`SO_EXCLUSIVEADDRUSE` only** on Windows. The first attempt
(`SO_REUSEADDR` then `SO_EXCLUSIVEADDRUSE`) was a silent no-op — the two options
are mutually exclusive, the exclusive `setsockopt` returns `WSAEINVAL(10022)`, and
the port stays shareable (measured: two shipped-binary instances both alive on one
port). With `SO_EXCLUSIVEADDRUSE` alone the second instance's `bind` fails
`WSAEADDRINUSE(10048)` and it exits 1. Receipts:
`I:\tmp\verify8_sock.c` (regimes A/B/C) and `I:\tmp\ann_v8b\bind_test.py`
(A/B against the real binary: committed = SPLIT-BRAIN, fixed = SAFE).

---

## 9. MEDIUM — Index cache keyed on (mtime **seconds**, size) → stale reads

`lz_index_cached` (`L284-299`) reloads only when `st_mtime` or `st_size` change.
`st_mtime` is truncated to whole seconds (`(long)st.st_mtime`). A rewrite within the same
second that keeps the same size is **not detected**; the server keeps serving the old
vectors.

### Reproduction (scratch)

`I:\tmp\ann_probe/cache_test2.py`: rewrite the index with a same-size, same-integer-second
mtime change:

```
orig mtime=1791434907.004  new mtime=1791434907.000  same-int-second=True  same-size=True
health entries after same-second same-size rewrite: 2881
NEW sid visible (cache reloaded)? False
```

The route cache (`lz_route_cached`) has the same seconds-granularity weakness for
`kv_anchors.bin`/`kv_index.order`. A regrow that finishes within the same second as the
prior file write can leave the server routing on stale anchors/order.

---

## 10. MEDIUM — Query silently truncated at 2047 chars; token-limit failure masked

`sq[2048]` bounds the query (`L584`, `L596-600`); longer queries are cut at 2047 bytes with
no error. Separately, the sidecar hard-fails (HTTP 500) above **512 tokens**; the server
surfaces that only as the generic `{"error":"embed unavailable"}`.

### Reproduction

Clean truncation proof (`I:\tmp\ann_probe/probe17.py`) — two queries identical for the
first 2047 chars, differing only in the tail, both individually embeddable:

```
pad len: 2047 sidecar(pad): 200
q1 len: 2071 sidecar(full q1): 200
q2 len: 2071 sidecar(full q2): 200
q1 hits: ['a38861e43bdf3eaa430bc6dc411b1b36', ...]
q2 hits: ['a38861e43bdf3eaa430bc6dc411b1b36', ...]   # identical
pad hits:['a38861e43bdf3eaa430bc6dc411b1b36', ...]
q1 == q2  (different tails, same result): True
q1 == pad (server truncated q1 to 2047): True
```

Sidecar token limit bisected (`I:\tmp\ann_probe/probe16.py`):

```
unit=' '       max chars = 509     (≈512 single-char tokens)
unit='word '   max chars = 2549
unit='ab'      max chars = 2039
```

So a query that is fine byte-wise but token-heavy (>512 tokens, e.g. long CJK) returns
`embed unavailable` with HTTP 200 and no hint about the cause.

---

## 11. MEDIUM — `topk` parsing: silent coercion and substring injection

`lz_json_int` (`L99-110`) does `strstr` for the first `"topk"` then `atoi` — it is not a
JSON parser. Consequences:

- **Silent coercion** (not fail-loud, despite the README claim of a fail-loud contract):

  ```bash
  curl -s -X POST -H "Content-Type: application/json" -d '{"query":"tesspack","topk":0}' \
    http://127.0.0.1:8096/v1/state/search | python -c "import sys,json;print(len(json.load(sys.stdin)['hits']))"
  # 1        <- topk=0 silently became 1
  ```
  `topk:-5` → 1 hit; `topk:"abc"` → 1 hit; `topk:3.9` → 3 hits; `topk:"1e3"` → 1 hit.
  Only `>32` is rejected (`400`); everything below/below-zero is coerced.

- **First-substring-wins injection**: any earlier occurrence of the byte sequence `topk"`
  in the raw body overrides the real field, even inside the query string:

  ```
  body: {"query":"tesspack"topk":1","topk":5}     -> returned hits = 1  (real topk=5)
  body: {"query":"tesspack","topk":1,"topk":5}    -> returned hits = 1  (JSON says last=5)
  body: {"query":"tesspack","topk":5,"topk":1}    -> returned hits = 5  (JSON says last=1)
  ```

  Duplicate keys resolve to the **first**, the opposite of typical JSON consumers. A
  mis-parsed `topk` also changes routing (`topb = topk + headroom`), so a "wrong" topk
  changes which buckets are walked, not just how many hits are printed.

---

## 12. LOW — Budget/`truncated` telemetry is misleading

On the 57k index, `topk ≥ 3` scores **every** item (`items_scored == items_total == 57076`)
yet the response still sets `"truncated":1`. The flag does not mean "results were cut"; it
means the budget was reached, and the default budget equals the index size.

```
topk | buckets | items_scored | items_total | truncated
   1 |       5 |        44898 |       57076 | 0
   2 |       6 |        51716 |       57076 | 0
   3 |       7 |        57076 |       57076 | 1     <- full scan, still "truncated"
  32 |       8 |        57076 |       57076 | 1
```

Scratch run with `LZ_SEARCH_BUDGET=100` confirms the flag only becomes meaningful when the
budget is below N (`items_scored=100, budget=100, truncated=1`). A client using
`truncated` to decide whether results are exact will be misled in both directions.

This also contradicts the README's "the anchor route does not prune" blanket claim: at
`topk=1`/`topk=2` it **does** prune (46,893–51,716 of 57,076 scored), i.e. the routing
behaviour — and thus the exactness guarantee — depends on `topk`.

---

## 13. LOW — Fog latch is write-only; route log grows unbounded

In `ann_search.c` the latch is only ever **written** (`wl_traverse_log`, L392). `wl_is_open`
is never called (grep: no reader). The "tombstone" state therefore never influences
routing — it is pure telemetry, yet it is persisted on every query.

```
ROUTE LOG GROWTH (unbounded append per query)
  log size before=12451 after 20 queries=16291 delta=3840 bytes (192.0 B/query)
  -> each successful query appends N*32-byte records (route walk); file never compacted
```

At the observed rate (`data/memcore/lz_route.log` grew to ~20 KB in normal use) a busy
client grows the file without bound; nothing ever appends a `CLEAR` or compacts it. Also,
`lz_fog_id(bucket)` = `wl_id(bucket%144, 0)` = `(bucket%144)*72`, so **bucket 0 maps to
reserved id 0** and can never be latched:

```
walking 8 buckets (topk=8) with 40 distinct queries:
  buckets ever walked: [0,1,2,3,4,5,6,7]
  max fog.shut observed: 7   (8 buckets walked; bucket 0 can never shut)
```

---

## 14. LOW (known) — `test_anchor_determinism` fails

Reproduced with the shipped binary:

```
$ ./build/test_anchor_determinism.exe
RECEIPT TRAIN_IDENTICAL yes
RECEIPT ROUTE_IDENTICAL no
RECEIPT TAMPER_REJECTED yes
STANDING PROOF FAILED        (exit=1)
```

This is documented in `FORK.md` as a pre-existing upstream tie-break bug (`anch_assign` vs
`anch_route[0]` on equidistant centroids) and is excluded from `make test`. The other three
test binaries pass (`test_anchor_route` 5/5, `test_anchor_routed` 13/13,
`test_wang_latch` ALL PASS). Noted for completeness — it is a real, reproducible red test,
but not caused by this fork.

---

## 15. LOW — HTTP hygiene gaps

- **HEAD not supported**: `HEAD /health` → `404` **with a body** (62 bytes), violating HEAD
  semantics.
- **No keep-alive**: always responds `Connection: close`; pipelining two requests on one
  connection returns only the first response.
- **No version negotiation**: `HTTP/1.0`, `HTTP/0.9`, and a request line with no version
  are all answered `HTTP/1.1`.
- **Malformed JSON accepted**: `{"query":"tesspack","topk":3` (unterminated) returns `200`
  with hits.
- **Oversized request** (> 1 MiB request buffer, `rq[1<<20]`) → connection reset with no
  `413`; the server survives (stays up) but the client gets a raw TCP reset.
- **Lying `Content-Length`** (says 100, sends 30) → server blocks until the client times
  out (feeds directly into §4).
- **Content-Type ignored**: a `text/plain` body is parsed as JSON.

---

## What works (for balance)

- **Recall is exact when the query vector is correct.** With a raw-UTF-8 (or the correctly
  embeddable) query, top-1/top-3/top-5 matched an independent cosine oracle on every
  English query tested (`recall@1 20/20`, `recall@3 20/20` on the lowk sweep; and all
  8/8 Thai queries matched once sent as raw UTF-8).
- **`topk>32` is fail-loud** (`400 topk_out_of_range`) — the one path that does the right
  thing.
- **The binary survives oversized requests and repeated abuse** without crashing
  (process stayed alive and kept serving after every malformed/oversized request).
- **The three shipped `make test` binaries pass.**

---

## Reproduction index (all under `I:\tmp\ann_probe\`, scratch only)

| file | finding |
|---|---|
| `dos.py` | §4 single-connection freeze |
| `dead_embed.py` | §5 false-green `/health` |
| `regrow_test.py` | §3 index corruption on regrow |
| `escape_test2.py` | §2 unescaped response JSON |
| `escape_bug.py`, `escape_bug2.py`, `diag5.py` | §1 escape decoding bug (root cause) |
| `recall_thai.py` | §1 recall: escaped vs raw UTF-8 (8/8) |
| `probe17.py`, `probe16.py` | §10 truncation + sidecar token limit |
| `inject.py`, `probe19.py` | §11 topk coercion / injection |
| `infra2.py` | §8 SO_REUSEADDR double-bind |
| `cache_test2.py` | §9 stale index cache |
| `topk_table.py`, `semantics.py` | §12 budget/truncated telemetry |
| `fog_test.py` | §13 latch bucket-0 dead + log growth |
| `repro.sh`, `final_repro.py` | quick live one-liners |

*End of report.*
