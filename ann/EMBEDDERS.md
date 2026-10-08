# EMBEDDERS.md — which embedder is which, and why there are three

Measured 2026-10-08. The short version: **it is the same model name
(`multilingual-e5-small`) but three different packages**, and mixing two of them
costs ranking accuracy. Know which one a path uses before comparing results.

## The three packages (one model, three wrappers)

| # | package | file | runtime | who uses it | port |
|---|---|---|---|---|---|
| 1 | **ONNX int8** | `model_int8.onnx` (118 MB) | onnxruntime + tokenizers (Python) | memcore daemon, MCP, `v2.py` search, index build | in-process |
| 2 | **ONNX int8 (HTTP)** | same, wrapped | `embed_server.py` (OpenAI `/v1/embeddings`) | 9Router selfhosted-embedding | **:8080** (not always running) |
| 3 | **GGUF Q8_0** | `multilingual-e5-small-q8_0-fixed.gguf` (131 MB) | llama.cpp (`llama-server /embedding`) | ANN `:8096` **text** path only | **:18095** |

- ONNX model dir: `I:\cortexkit\magic-context\models\Xenova\multilingual-e5-small`
- GGUF file: `I:\model\multilingual-e5-small-q8_0-fixed.gguf`
- Both are 384-dim. Same model, different quantization (int8 vs Q8_0) → different
  vectors.

## Which path uses which (the map)

```
daemon (memory_daemon.py)  ── ONNX #1 ──▶  fact_store.v2.sqlite3 (embeddings)
                                              │
                              build_index.py  │  (reads stored ONNX vecs, no re-embed)
                                              ▼
                              kv_index.jsonl (index, built from ONNX vectors)
                                              │
memcore/MCP  ── ONNX #1 embed ──▶  POST :8096 {"vec":[...]}   ← ANN just ranks; NO GGUF
                                              │
curl text ───── text ─────────▶  POST :8096 {"query":"..."}    ← ANN embeds via GGUF #3
```

**Key fact:** the memcore/MCP path sends a **vector** (`vec`), so ANN never calls
the GGUF sidecar. **The GGUF sidecar (#3) is touched only when raw text is POSTed
to `:8096` directly** (e.g. `curl`). So the ONNX/GGUF difference does not affect
the normal memcore path at all.

## Do the two agree? (measured)

cosine between the ONNX and GGUF embedding of the SAME text:

| | mean | min | max |
|---|---|---|---|
| ONNX vs GGUF | **0.970** | 0.941 | 0.991 |

- **0.97, not 1.0** → they are not interchangeable. Same space, ~3% direction
  drift from quantization.
- Effect: index is built from ONNX vectors; a GGUF query retrieves the source
  entry at top-1 only **0.80–0.87** vs **0.80** for ONNX (measured over 30 real
  rows) — the drift is why `recall@1` is not 1.0 in cross-embedder checks.
- This is **normal** for any quantization (like JPEG vs WebP of one photo). Not a
  bug. Choosing one package removes it.

## Speed (measured 2026-10-08, interleaved 40 rounds, warm)

| | median | mean | min | max |
|---|---|---|---|---|
| ONNX in-process | **8.8 ms** | 9.5 | 5.4 | 31 |
| GGUF HTTP | **9.7 ms** | 33.2 | 6.9 | 293 |

- **Median ratio 1.10× — they are a tie.** GGUF is NOT meaningfully slower at
  steady state.
- The GGUF difference is **transport** (separate process, HTTP) + **tail
  jitter**, not compute. Compute is on par or faster.
- **Cold first call:** llama-server pays ~75–240 ms once (model warm-up) — that
  is the "slow" number people remember. Steady state is 7–9 ms after ~4 calls.
- A one-shot warmup at sidecar start (POST `/embedding` once) removes the cold
  penalty; it does NOT remove the occasional 20–24 ms socket jitter (~15% of
  calls on this 2-core box).

**Conclusion:** GGUF and ONNX are the same speed once warm. If you only ever call
the GGUF path, no change is needed; add a warmup if you want the first call fast.

## If you want ONE embedder (remove the drift)

Use **ONNX**, because the index is already built from ONNX vectors — query and
index then share a space exactly (recall@1 → ~1.0) and you drop the GGUF
sidecar. The ANN server is C and cannot load ONNX, so it would call
`embed_server.py` (:8080, OpenAI format) instead of llama-server.

⚠ Two things to fix first if you do this:
1. **API shape mismatch.** `embed_server.py` speaks OpenAI
   (`{"input":...}` → `{"data":[{"embedding":[...]}]}`); ANN's `lz_embed` speaks
   llama (`{"content":...}` → `{"embedding":[...]}`). One side must be adapted.
2. **The sanitizer may become unnecessary.** ANN's `lz_sanitize` exists to dodge
   GGUF tokenizer holes (Thai U+0E33, `\n`, math symbols). The HF `tokenizers`
   used by ONNX likely accepts those — verify before removing.

Do not mix: never build the index with one package and query with the other.

## e5 prefix rule

e5 wants `"passage: "` for indexed text and `"query: "` for queries.

- daemon embeds stored content with `"passage: "`.
- `v2.py search()` embeds the query with `"query: "`.
- The ANN **text** path (`lz_embed`) sends **no prefix** — a real (small)
  inconsistency for direct `:8096` text queries.

Measured effect of prefix on stored-vector match: `passage:` 0.967, none 0.966,
`query:` 0.952 — so the stored index is closest to `passage:`/none, and the
query-side `query:` prefix is the intended asymmetry, not drift.
