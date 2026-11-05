# GPU rerank bench — SIFT1M + BEIR SciFact (Colab T4)

## SIFT1M (200k x 128, 100 queries, real sift_base.fvecs)
| path | ms | speedup vs CPU |
|---|---|---|
| CPU scipy cdist | 2944.1 | 1.0x |
| GPU transfer-each-time | 27.0 | 109.1x |
| GPU V-resident (matmul only) | 2.9 | 1004.3x |

## BEIR SciFact (5183 docs x 384, 1109 queries, all-MiniLM-L6-v2)
| path | ms | speedup vs CPU |
|---|---|---|
| CPU scipy cdist | 1059.0 | 1.0x |
| GPU transfer-each-time | 4.3 | 244.3x |
| GPU V-resident (matmul only) | 1.3 | 790.4x |

## Random (synthetic, from GPU-RERANK-RESULTS.md)
128 x 200k: 3587.6 ms CPU -> 26.9 ms GPU (133.2x) -> 2.68 ms resident (1335x)

## Conclusions
- GPU rerank is exact (fp32 argmax matches CPU) and 100-1000x faster than CPU
  scipy cdist on real SIFT/SciFact data.
- Keeping V resident on GPU removes the PCIe transfer (88% of e2e) and gives
  the largest single win (9.6-10x on top of transfer-each-time).
- Maps to DWGLS residency layer (active region), NOT jet bridge (which per
  doctrine is for late data, not speedup).
- fp16 prefilter: 97-100% agreement; use as prefilter + fp32 rescore for exactness.

## Shipped
- rerank_gpu.py: ResidentReranker + rerank_cosine_gpu + topk; fallback numpy.
- GPU-RERANK-RESULTS.md (synthetic T4 table)
- This file: real-data bench.
