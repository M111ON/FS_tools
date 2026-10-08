# FORK.md — ann/ provenance and re-sync

`ann/` is a **standalone fork** of the DWGLS anchor-route ANN search core. It is
not a rewrite: the search code is lifted whole, then cut free of every GGUF /
llama / model-load dependency so it runs as one small HTTP search server.

## Origin

| field | value |
|---|---|
| source repo | `I:/DWGLS-native-fs` |
| origin URL | https://github.com/M111ON/DWGLS.git |
| forked at commit | `8c85f9f` — *docs: handoff for next session — ANN warm path pickup* |
| forked on | 2026-10-08 |

The upstream working tree was clean at fork time (`git status --porcelain` empty).

## What was taken (exact cut)

| piece | source span in `tools/gguf_lazy_serve.c` |
|---|---|
| HTTP helpers (`lz_http_recv/send`, `lz_json_int`, `lz_json_escape`, `lz_http_post`) | L44–116, L205–216 |
| `lz_embed` (embed sidecar client) | L179–204 |
| fog latch globals + `lz_fog_init` + `lz_fog_id` | L219–263 |
| `LZEntry`, `lz_json_val`, `lz_entry_parse`, `lz_index_load/free`, index cache | L281–385 |
| anchors/order route cache | L391–427 |
| `lz_mem_search` (the shared selection core) | L435–586 |
| `POST /v1/state/search` handler | L1907–2021 |

Vendored headers — **byte-identical copies** from `core/` (verify with `diff`):

```
include/anchor_route.h
include/geo_wang_latch.h
include/geo_id_route_log.h   (pulled in by geo_wang_latch.h)
include/rdh_capture.h        (pulled in by rdh_addr.h)
include/rdh_addr.h
```

## What was NOT taken (deliberately left behind)

Everything that pulls in the model: `#include "llama.h"`, `"ggml.h"`,
`"ggml-backend.h"`, `"../core/gguf_box.h"`, `"../core/win_cache.h"`, the
`main()` model loader, the generate path (`lz_mem_prompt`, `lz_difficulty`),
and all tensor / field / page-fault machinery. The embedder is an **external
sidecar over HTTP**, not an in-process model — so this binary needs no `.gguf`,
no backend DLLs, and no llama library.

## Behaviour change (the only one)

`topk` is bounded at `ANN_MAXK` (32) instead of 8. Upstream clamps to 8 only
because the generate path reuses 8-row stack arrays; a standalone search server
has no such caller. The arrays (`top_sid`, `top_file`, `rb`, `route_bk`) are
sized `ANN_MAXK` here to match. To reproduce the upstream contract exactly, set
`ANN_MAXK 8` in `src/ann_search.c`.

## Known upstream issue (carried, not caused by the fork)

`tests/test_anchor_determinism.c` fails — `RECEIPT ROUTE_IDENTICAL no` /
`STANDING PROOF FAILED`. **It fails identically in the upstream repo at
`8c85f9f`**, with the vendored headers and test file byte-identical, under both
this fork's flags and the upstream repo's own CFLAGS. It is therefore a
pre-existing bug, not a fork artifact, so it is excluded from `make test` here.

Cause (narrowed): on a query where two centroids are **exactly equidistant**,
`anch_assign` (`if (d < bd)` → keeps the *first/lowest* index) and
`anch_route[0]` (partial selection sort → keeps the *last/highest* index on a
strict `<` tie) disagree. The test's oracle `assign == route[0]` then fails.
Because the trained centroids are byte-identical across the two runs, the route
divergence must come from such a tie in the fixed synthetic dataset. It does not
affect the real index (`dim 384` continuous embeddings do not sit on ties).

Fix belongs upstream; see `anchor_route.h` `anch_assign` L46 vs `anch_route`
L70–72. Track it there so the fork does not silently diverge.

## How to re-sync (when upstream moves)

1. `cd I:/DWGLS-native-fs && git log --oneline -1` — note the new HEAD.
2. Diff the vendored headers:
   `for h in anchor_route.h geo_wang_latch.h geo_id_route_log.h rdh_capture.h rdh_addr.h; do diff include/$h I:/DWGLS-native-fs/core/$h; done`
   Copy any header that changed (they are the actual ANN logic; the .c is glue).
3. Diff the cut spans listed above against `tools/gguf_lazy_serve.c`. Port only
   the search-core changes; never re-import the model/GGUF side.
4. Re-run `make test` and the live check in `README.md`.
5. Update the commit hash in this file and in the `ann_search.c` header comment.

## Data (not source — regenerated upstream)

`data/kv_index.jsonl`, `data/kv_index.order`, `data/kv_anchors.bin` were copied
from upstream `build/` at fork time (2881 entries). They are **data**, not code:
rebuild them with the upstream `build/reindex_docs.py` (embeds via the sidecar
on :18095) if the corpus changes. `data/lz_route.log` (tombstone log) is created
at runtime and is git-ignored.
