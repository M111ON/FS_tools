# ann/ — standalone ANN semantic-search server

The DWGLS anchor-route ANN search, extracted into a small self-contained
server. One job, one endpoint:

```
POST /v1/state/search   {"query":"...", "topk":3, "sid":"optional"}
GET  /health
```

No GGUF, no llama, no model in-process. The embedder is an external sidecar
(llama-server `/embedding`, `multilingual-e5-small`, 384-dim) reached over HTTP.

## Naming (2026-10-26)

The record that a door was walked is a **totem** — a marker planted at the door
meaning "this way was walked". It is NOT a deletion marker. The word
`tombstone` is retired (it read as "this data is deleted" and caused the same
misreading in several sessions). The store is the **totem latch**
(`g_totem`, `lz_totem_*`, `/health` → `totem`, env `ANN_TOTEM_WARM`); the latch
primitive keeps its own state words, **open / shut** (`wl_is_open`).

## Layout

```
include/   vendored headers (byte-identical copies from DWGLS core/)
src/       ann_search.c — the whole server
tests/     anchor-route + wang-latch tests (copies; green upstream)
data/      ann_index.jsonl · ann_index.order · ann_anchors.bin  (regenerated upstream)
FORK.md    provenance + re-sync instructions
```

## Build

```sh
make                 # -> build/ann_search.exe
make test            # anchor_route / anchor_routed / wang_latch
```

## Run

Start the embed sidecar first (it is NOT part of this repo):

```sh
cd I:/llama/llama-b10830-win-vulkan-x64
./llama-server.exe -m I:/model/multilingual-e5-small-q8_0-fixed.gguf \
    --embedding --port 18095 -c 512 &
```

Then the search server:

```sh
cd I:/tools/ann
LZ_EMBED_HOST=127.0.0.1 LZ_EMBED_PORT=18095 ./build/ann_search.exe --serve 8096
```

Query:

```sh
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"query":"tesspack format spec","topk":3}' \
  http://127.0.0.1:8096/v1/state/search
```

## Environment

| var | default | meaning |
|---|---|---|
| `LZ_EMBED_HOST` | `127.0.0.1` | embed sidecar host |
| `LZ_EMBED_PORT` | `18095` | embed sidecar port |
| `ANN_INDEX_PATH` | `data/ann_index.jsonl` | vector index |
| `ANN_ANCH_PATH` | `data/ann_anchors.bin` | trained centroids |
| `ANN_ORDER_PATH` | `data/ann_index.order` | bucket-major placement perm |
| `ANN_ROUTE_LOG` | `data/lz_route.log` | totem (P3) log — **must be a native Windows path** |
| `ANN_PORT` | `8096` | listen port (or `--serve N`) |
| `LZ_ROUTE_BUCKETS` | auto (`topk+4`) | buckets walked |
| `LZ_SEARCH_BUDGET` | index size | candidate budget |
| `ANN_WALK_PRUNE` | `0` | bucket-major pruned walk (scores members only) |
| `ANN_WALK_FLOOR` | `0.0` | confidence floor: pruned top below this falls back to full scan |

## Response shape

```json
{"status":"ok",
 "hits":[{"sid":"...","file":"...","npast":400,"score":0.8793}],
 "route":{"mode":"anchor","buckets":[2,4,3,7,1,0,6],
          "block_reads":1,"items_scored":2881,"items_total":2881,
          "us_per_item":31.99,"budget":2881,"truncated":1,"fallback":0,
          "totem":{"lit":7,"open":10359,"shut":7,"replayed":25,"free":10366}}}
```

`topk` is fail-loud: `> ANN_MAXK` (32) returns `400 topk_out_of_range`, never a
silent clamp. See `FORK.md` for the one behaviour change vs upstream and the
carried upstream test issue.

## Regrow modes: `POST /v1/state/regrow` (2026-10-20)

The regrow endpoint now takes THREE modes, chosen from a state sidecar file
(`<ANN_ANCH_PATH minus .bin`>-state.json, e.g.
`data/memcore/ann_anchors-state.json`):

| mode | trigger | what it does | measured |
|---|---|---|---|
| `unchanged` | index mtime+size match state AND anchors checksum matches | **nothing** (stat + a 4-u32 anchor header read + a checksum read) | **0.3–0.5 ms** on a 4k corpus (3 repeats 1–3 ms wall incl. HTTP) |
| `incremental` | index GREW by append only (line n_prev-1 still carries state.last_sid) | reuses EXISTING anchors (no kmeans), routes ONLY rows ≥ n_prev, recomputes perm, rewrites the index file once | 4k+200: train **0 ms**, route **0.9 ms** (new rows only), rewrite 1232 ms, load 2326 ms |
| `full` | state missing/corrupt/contract failed, or body `{"force":1}` | the ORIGINAL kmeans-train pipeline, unchanged byte-for-byte, writes fresh state | 4k: load 2045, train 716, route 19, rewrite 975 |

Semantic contract: **corpus unchanged = O(1); append = linear in new rows only
(+ the one unavoidable full index rewrite); force = full.** The state file is a
NEW separate sidecar — the on-disk index/anchors/order FORMAT is unchanged.
Telemetry keys are additive: `regrow:{mode,ms,skipped,n_prev,n_new,
ms_per_phase:{load,train,route,rewrite,save}}`; the legacy keys `status`,
`mode` (`anchor` = trained regrow, `brute` below-min, `error`),
`nent`, `K` keep their meanings.

Determinism (2026-10-20): regrow x2 on the SAME 4k corpus produces
**byte-identical** anchor files (sha256 `e099b9f4c8248d23…` both runs);
`full_ms` in the state is telemetry only. On the live-shaped 62k corpus the
full path measures 76.9 s total: ~17.6 s of it is the one-pass 232 MB
fprintf(%.6g) index rewrite, ~10.4 s the kmeans assignment, and the rest index
load/parse/anchor-route/perm write (see `bench*.py` microbenches).

## Integrations

### memcore → ANN (built and measured 2026-10-08)

memcore (`I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3`) already stores
`multilingual-e5-small` vectors at dim 384 — the same model this server's
sidecar serves — so its 57,076 entries index **without re-embedding**:

```sh
# 1. base index from the store (no embedding; reads stored vec blobs)
python scripts/build_index.py --out data/memcore/ann_index.jsonl

# 2. train anchors + place + write the anchor fields (server-side)
ANN_INDEX_PATH=data/memcore/ann_index.jsonl \
ANN_ANCH_PATH=data/memcore/ann_anchors.bin \
ANN_ORDER_PATH=data/memcore/ann_index.order \
  ./build/ann_search.exe --serve 8099
curl -s -X POST http://127.0.0.1:8099/v1/state/regrow    # ~60 s on 57k
```

Measured on 57,076 entries:

| | wall | note |
|---|---|---|
| memcore `v2.search` | **568 ms** (7.9 s cold) | rebuilds the 57k×384 matrix every call |
| this server | **~60 ms** | index cached; exact cosine over the whole set |

Recall: top-5 **identical to an independent numpy cosine oracle** on every
query tested (4/4). The anchor route does not prune (scores all 57k) — this is
exact recall, the win is caching + no matrix rebuild, not approximation.

`npast` in a memcore-built index is the content character length (the index
format has no other field for it). To map a hit back to memcore, the `sid` is
the memcore `entries.id`.

## Confident Guideline (graft cluster bias) — OPTIONAL, OFF by default

A soft ranking bias built from **graft's file-relationship graph**. Entries
that mention a file in the query's neighborhood get `score + λ·affinity`, so
ordering leans toward the structurally-related cluster. It is **never a
filter** — the bias only ADDS, so the scored candidate set is unchanged and
recall cannot drop.

```sh
# build the bias tables from graft's wiring graph (re-run after the graph moves)
python scripts/graft_bias.py
python scripts/graft_bias.py --mode 2hop --damp 0.5      # try other forms
python scripts/graft_bias.py --rel calls=1.0,imports=0.7,extends=0.9,contains=0.4

# turn it on (default OFF)
ANN_GRAFT=1 ANN_GRAFT_LAMBDA=0.02 ./build/ann_search.exe --serve 8096
```

| env | default | meaning |
|---|---|---|
| `ANN_GRAFT` | `0` | master switch |
| `ANN_GRAFT_LAMBDA` | `0.05` | bias strength (0 = off) |
| `ANN_GRAFT_MAXN` | `4` | max file keys per entry |
| `ANN_GRAFT_DIR` | `data/graft` | tables dir |
| `ANN_AMP_BOOST` | `0` | totem × graft amplifier: multiply the bias of warm-bucket entries (needs `ANN_GRAFT=1` + `ANN_TOTEM_WARM=1`) |
| `ANN_AMP_K` | `2.0` | warm-bucket weight multiplier |
| `ANN_GRAFT_CAND` | index size | cap on graph-proposed candidates in the train-free route |
| `ANN_GRAFT_BACKWARD` | `0` | expand graph candidates via in-edges (files that point at the query's file) |
| `ANN_GRAFT_BUCKETS` | `0` | with a trained route, ADD the graph-endorsed buckets to the walked set (monotone) |

The reciprocal ("magnetic") mix is **not** a server env var — it is baked into
`nb.tsv` by `scripts/graft_bias.py --magnet indirect --rho 0.5`. See
[`GRAPH-ROUTE.md`](GRAPH-ROUTE.md).

Model (relation/confidence weights, neighbor mode `direct|2hop|max|sum`, damp,
k) lives in `data/graft/bias.json` — change it there and re-run, no rebuild.

**Measured** (57k index, 40 queries that name a file, direct mode): cluster
share in top-10 rose 0.395 → 0.83 at λ=0.05, but the top-10 order also
changed ~44% — λ=0.02 is the conservative point (+22% cluster, 22% reorder).

**Totem is the amplifier — now wired and measured.** This bias is the
*structural* axis (which code a query is about — static, baked). The totem latch /
totem is the *hot/cold* axis (which routes were just walked — changes per
query). They now **compose**: with `ANN_AMP_BOOST=1`, an entry that sits in a
bucket the totem kept warm has its graft bias **multiplied by `ANN_AMP_K`**
(default 2.0). The boost multiplies the bias only — never the cosine score — so
with either axis off it is a strict no-op and recall is untouched.

Measured (57k memcore index, `ANN_GRAFT=1 ANN_TOTEM_WARM=1 LZ_ROUTE_BUCKETS=5`,
warm pass): a warm route scores the same items, but the multiplier promotes the
warm cluster — `route.amp.applied` = 87 entries, and warm entries climb into the
top-5 on 3/5 path-bearing queries (e.g. `cf764fd707`, `404ad7b548`). Off by
default; keep the two switches independent.

## Totem warm path (hot/cold axis) — OPTIONAL, OFF by default

`ANN_TOTEM_WARM=1` makes the walk read the totem latch before walking: buckets
already SHUT (walked by an earlier query) are scored first, then the scan stops
after them. An overlapping query scores fewer items with the **same top-k**; a
novel query is unchanged. Inert at `topk>=3` on the 57k index (the route already
covers everything — the B1 wall). Full numbers, limits, and reproduce steps in
[`WARM-PATH.md`](WARM-PATH.md).

## Graph route (train-free) — OPTIONAL, OFF by default

When there is **no trained anchor route** (a fresh index, or a train that cannot
run), the server would otherwise fall back to `mode:"brute"` — a full O(N) scan.
With `ANN_GRAFT=1` the file-relationship graph instead **proposes the candidates**:
entries carrying the query's files plus entries carrying a neighbour file
(`route.mode = "graph"`). A query the graph cannot map falls back to the full
scan, so novel recall is untouched. Knob `ANN_GRAFT_CAND` caps the subset.

Measured on the real 57k index with the anchor route disabled: the graph scores
**11–266 of 57,076** entries (a 200×–5000× smaller set) but recall@10 against
exact brute is only **0.34** (0.60 on files well-represented in the index, 0.00 on
sparse ones). It is a *rough* map / front tier, not an exact route. Full numbers:
[`GRAPH-ROUTE.md`](GRAPH-ROUTE.md).

## Pruned walk (bucket-major) — OPTIONAL, OFF by default

The default anchor walk STEPS over all `nent` entries in placement order and
only counts scoring toward the budget, so the table's bucket choice cannot
change the scored set (measured: 4 seed tables, buckets differ 30/30, hits
identical 30/30). With `ANN_WALK_PRUNE=1` the walk visits ONLY bucket members,
gathered bucket-major (`rb[0]`'s members first), so a budget cut keeps the
NEAREST bucket's entries and the table steers the set. `ANN_WALK_FLOOR=f`
adds a confidence floor: a pruned top below `f` falls back to a full scan
(`mode:"anchor+full"`, `fallback:1`), so recall is never worse than today.

Measured (59,752-entry memcore index, 30 queries, `LZ_ROUTE_BUCKETS=5`,
`LZ_SEARCH_BUDGET=8000`, 17% scan): pure prune top-1 **13/30**, overlap
**2.70/5** (trained) / **2.17/5** (training-free derived table — same tier,
no training); floor `0.5` never fires on this index (tops run ≥0.5); floor
`0.95` fires and matches full hits exactly. Both knobs default off =
byte-identical behaviour (verified: same top-1, `mode:"anchor"`).

## Run as a service (Windows)

The server is kept alive by a supervisor + a Task Scheduler entry, the same
pattern chat-pool uses (`I:/tools/chat-pool/pool_keepalive.py`):

```sh
python ann_keepalive.py --once     # one pass: if /health is down, start it
pythonw ann_keepalive.py           # foreground loop (rarely needed)
```

`ann_keepalive.py` also starts the **embed sidecar** if `:18095` is down (it
runs `llama-server` from its own dir, which its exe needs for sibling DLLs).
It only ever STARTS — never kills a healthy server — and backs off on a
crash-loop.

Task: `ann-keepalive` (logon + every 5 min, runs `--once`), registered from
`ann-keepalive.xml`:

```sh
schtasks /create /tn ann-keepalive /xml "I:\tools\ann\ann-keepalive.xml" /f
```

It binds **:8096** and serves the derived memcore index (`data/memcore/`) when
present, else the shipped sample. `chat-pool`'s `GET /semantic` defaults to
`http://127.0.0.1:8096`, so the two line up with no extra config.

## Known limits (inherited, measured upstream)

- Anchor route does **not** prune: a query scores the whole index (`items_scored
  == items_total`). Cost is O(N) at exact recall — the hard-bucket wall.
- `LZ_ANCH_MAXK = 8` caps routing buckets while N grows.
- Wins on index **memory** and build time, not query latency (loses ~84× to HNSW).

## Embed-input sanitizer (tokenizer vocab gap) — always on

See [`EMBEDDERS.md`](EMBEDDERS.md) for the full embedder map (ONNX vs GGUF, speed,
drift, the e5 prefix rule). Summary of the sanitizer below.

The `multilingual-e5-small` GGUF tokenizer has holes: some codepoints make the
sidecar's `/embedding` return **500** (`invalid unordered_map<K,T> key`), which the
server surfaced as `503 embed_unavailable`. The big one is **U+0E33 (Thai SARA AM,
the "ำ" in "ทำ")**; Thai-block gaps (`U+0E3B/0E3C/0E4E/0E5B`, `U+0E5C..U+0E7F`),
several math symbols, and even **`\n`/`\t`/`\r`** do the same.

`lz_sanitize()` in `ann_search.c` normalizes the text before it reaches the
sidecar: U+0E33 → U+0E4D U+0E32 (NFKC), an ASCII map for the symbols, drop for the
remaining Thai holes, control/whitespace → space. It is the C twin of
`scripts/build_index.py:sanitize`. Meaning-preserving; a no-op for English.

Measured: raw `ระบบนี้ทำงานยังไง` → sidecar **500**; sanitized → **200**. English
top-1 unchanged (`51b0fc3f…`), so no ranking regression.

Next levers live upstream and are ported on demand: totem warm path, maze
refine layer. Do not raise `ANCHR_MAXK` expecting pruning — measured negative.

## Batch endpoint: `POST /v1/state/search-batch` (2026-10-20)

One call, N≤32 queries. Body `{"queries":[...], "k":3}` (or `"topk"`).
Response: `{"status":"ok","telemetry":{"n","embed_ms","took_ms"},"results":[…]}` —
each `results[i]` has the SAME shape as a single `/v1/state/search` response
(`status`, `hits[]`, `route{}`).

Two wins it compounds:
1. **One embed call per batch** — `input:[...]` array to the :18095 sidecar
   instead of N calls (measured: B=1 12.3 ms, B=8 4.0 ms/ea, B=16 3.6 ms/ea).
2. **One `g_lock` acquisition per batch** — all N scans run inside a single
   critical section, so per-request lock overhead and cache re-warming are paid
   once. Measured on the 62k live index shape (port 8097 scratch server):
   batch B=8 ≈ 162–247 ms vs 8 serial singles ≈ 251 ms (kernel OFF)
   / ≈ 183 ms (kernel ON).

Float kernel (`ANN_FLOAT_KERNEL=1`, default OFF): hot loop uses `lz_dotf()`
(pure-float 4-acc chain) with per-entry reciprocal `1/norm` precomputed at
index load and per-query `1/qn`. Microbench (24k × 384, 9 reps): 353.9 ns/dot
→ 242.4 ns/dot = **1.46×**; NOT bit-identical (reassociation, LAW 3b) —
max_rel_diff 2.797e-07 over 50 pairs. End-to-end single-query p50 32 ms → 23 ms
and top-k sids OFF == ON per query on the 62k index (8/8 identical).
