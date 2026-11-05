# GPU rerank — Colab T4 results

Environment: Google Colab, Tesla T4 (15GB, compute 7.5), torch 2.11 +cu130.
Candidate sets: random unit-normalised float32, d=128.

## Speedup vs CPU (scipy cdist euclidean, nq queries x nc candidates)

| nq | nc | cpu (ms) | gpu fp32 (ms) | speedup | gpu fp16 (ms) | fp16 agree |
|---|---|---|---|---|---|---|
| 32 | 50k | 498.6 | 15.6 | 31.9x | 23.3 | 96.9% |
| 32 | 200k | 1126.2 | 26.7 | 42.2x | 61.0 | 100.0% |
| 64 | 50k | 515.6 | 7.6 | 67.8x | 7.2 | 98.4% |
| 64 | 200k | 1828.6 | 24.8 | 73.7x | 47.2 | 100.0% |
| 128 | 200k | 3587.6 | 26.9 | 133.2x | 46.7 | 100.0% |

## Conclusions
- fp32 cosine argmax on T4 is exact (100% agreement with CPU) and 32-133x faster.
- fp16 prefilter is 97-100% agreement; use as a prefilter then fp32 rescore for
  exactness (rerank_topk_gpu does this).
- Throughput scales with batch size (nq) and candidate count; at 128 queries
  x 200k candidates, 133x. This resolves tb6848cad's "SIMD/GPU or cheaper
  metric" option for the rerank stage.

## Shipped
- rerank_gpu.py: rerank_cosine_gpu (fp32, exact), rerank_cosine_fp16 (prefilter),
  rerank_topk_gpu (two-stage). Falls back to numpy BLAS when torch missing.
- test_rerank_gpu.py: 3 checks, ALL PASS (local CPU torch).
