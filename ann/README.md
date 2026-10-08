# ann/ — standalone ANN semantic-search server

The DWGLS anchor-route ANN search, extracted into a small self-contained
server. One job, one endpoint:

```
POST /v1/state/search   {"query":"...", "topk":3, "sid":"optional"}
GET  /health
```

No GGUF, no llama, no model in-process. The embedder is an external sidecar
(llama-server `/embedding`, `multilingual-e5-small`, 384-dim) reached over HTTP.

## Layout

```
include/   vendored headers (byte-identical copies from DWGLS core/)
src/       ann_search.c — the whole server
tests/     anchor-route + wang-latch tests (copies; green upstream)
data/      kv_index.jsonl · kv_index.order · kv_anchors.bin  (regenerated upstream)
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
| `ANN_INDEX_PATH` | `data/kv_index.jsonl` | vector index |
| `ANN_ANCH_PATH` | `data/kv_anchors.bin` | trained centroids |
| `ANN_ORDER_PATH` | `data/kv_index.order` | bucket-major placement perm |
| `ANN_ROUTE_LOG` | `data/lz_route.log` | tombstone (P3) log — **must be a native Windows path** |
| `ANN_PORT` | `8096` | listen port (or `--serve N`) |
| `LZ_ROUTE_BUCKETS` | auto (`topk+4`) | buckets walked |
| `LZ_SEARCH_BUDGET` | index size | candidate budget |

## Response shape

```json
{"status":"ok",
 "hits":[{"sid":"...","file":"...","npast":400,"score":0.8793}],
 "route":{"mode":"anchor","buckets":[2,4,3,7,1,0,6],
          "block_reads":1,"items_scored":2881,"items_total":2881,
          "us_per_item":31.99,"budget":2881,"truncated":1,
          "fog":{"lit":7,"open":10359,"shut":7,"replayed":25,"free":10366}}}
```

`topk` is fail-loud: `> ANN_MAXK` (32) returns `400 topk_out_of_range`, never a
silent clamp. See `FORK.md` for the one behaviour change vs upstream and the
carried upstream test issue.

## Integrations

### memcore → ANN (built and measured 2026-10-08)

memcore (`I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3`) already stores
`multilingual-e5-small` vectors at dim 384 — the same model this server's
sidecar serves — so its 57,076 entries index **without re-embedding**:

```sh
# 1. base index from the store (no embedding; reads stored vec blobs)
python scripts/build_index.py --out data/memcore/kv_index.jsonl

# 2. train anchors + place + write the anchor fields (server-side)
ANN_INDEX_PATH=data/memcore/kv_index.jsonl \
ANN_ANCH_PATH=data/memcore/kv_anchors.bin \
ANN_ORDER_PATH=data/memcore/kv_index.order \
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
| `ANN_AMP_BOOST` | `0` | tombstone × graft amplifier: multiply the bias of warm-bucket entries (needs `ANN_GRAFT=1` + `ANN_TOMBSTONE_WARM=1`) |
| `ANN_AMP_K` | `2.0` | warm-bucket weight multiplier |

Model (relation/confidence weights, neighbor mode `direct|2hop|max|sum`, damp,
k) lives in `data/graft/bias.json` — change it there and re-run, no rebuild.

**Measured** (57k index, 40 queries that name a file, direct mode): cluster
share in top-10 rose 0.395 → 0.83 at λ=0.05, but the top-10 order also
changed ~44% — λ=0.02 is the conservative point (+22% cluster, 22% reorder).

**Tombstone is the amplifier — now wired and measured.** This bias is the
*structural* axis (which code a query is about — static, baked). The fog latch /
tombstone is the *hot/cold* axis (which routes were just walked — changes per
query). They now **compose**: with `ANN_AMP_BOOST=1`, an entry that sits in a
bucket the tombstone kept warm has its graft bias **multiplied by `ANN_AMP_K`**
(default 2.0). The boost multiplies the bias only — never the cosine score — so
with either axis off it is a strict no-op and recall is untouched.

Measured (57k memcore index, `ANN_GRAFT=1 ANN_TOMBSTONE_WARM=1 LZ_ROUTE_BUCKETS=5`,
warm pass): a warm route scores the same items, but the multiplier promotes the
warm cluster — `route.amp.applied` = 87 entries, and warm entries climb into the
top-5 on 3/5 path-bearing queries (e.g. `cf764fd707`, `404ad7b548`). Off by
default; keep the two switches independent.

## Tombstone warm path (hot/cold axis) — OPTIONAL, OFF by default

`ANN_TOMBSTONE_WARM=1` makes the walk read the fog latch before walking: buckets
already SHUT (walked by an earlier query) are scored first, then the scan stops
after them. An overlapping query scores fewer items with the **same top-k**; a
novel query is unchanged. Inert at `topk>=3` on the 57k index (the route already
covers everything — the B1 wall). Full numbers, limits, and reproduce steps in
[`WARM-PATH.md`](WARM-PATH.md).

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

Next levers live upstream and are ported on demand: tombstone warm path, maze
refine layer. Do not raise `ANCHR_MAXK` expecting pruning — measured negative.
