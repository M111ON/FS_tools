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

## Known limits (inherited, measured upstream)

- Anchor route does **not** prune: a query scores the whole index (`items_scored
  == items_total`). Cost is O(N) at exact recall — the hard-bucket wall.
- `LZ_ANCH_MAXK = 8` caps routing buckets while N grows.
- Wins on index **memory** and build time, not query latency (loses ~84× to HNSW).

Next levers live upstream and are ported on demand: tombstone warm path, maze
refine layer. Do not raise `ANCHR_MAXK` expecting pruning — measured negative.
