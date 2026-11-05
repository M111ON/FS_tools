# Production readiness — ANN / geometry retrieval / RCU / GPU rerank

Date: 2026-11-05. Scope: I:/tools/ann/experiments/LC_tilecube (+ DWGLS-native-fs for RCU).
All receipts committed to github.com/M111ON/FS_tools (main).

---

## 1. What was tested (receipts)

### 1.1 Geometry retrieval (icosphere) — GEOMETRY-RETRIEVAL-REPORT.md
- Claim: geometric topology (icosphere) improves semantic candidate retrieval at
  equal scan budget, after controlling degree AND neighbour-size distribution.
- Replicated on SIFT1M (L2) and MemCore (cosine).
- Locked control (162 cells, 100 paired queries):
  - SIFT1M pool recall @5/10/20%: Ico .648/.804/.973 vs SizeRand .542/.603/.689
    (+.106/+.201/+.284, 95% CI excludes 0, wins 63/90/94 of 100)
  - MemCore: Ico .429/.664/.837 vs SizeRand .269/.328/.414 (+.160/+.336/+.423)
- Level gate: max_cell/N <= scan_budget (sufficient for zero truncation);
  L1->20%, L2->10%, L3->5%.
- Transition downtime: 1143 ms -> 3.3 ms; end-to-end 19.7x; remap 1.25% evals, 0 mismatches.
- RCU: 5 invariants zero, 3 conditions pass (2-thread local).
- Retracted (report §4): "geometry does not matter" (20-cell artifact),
  "mean cell size x%" rule, "ranking is bottleneck on text" (tie noise),
  "level does not affect query cost", "preNDCG~0 = no ordering info".

### 1.2 Precompute order asset — test_precompute_order.py (4/4 PASS)
- `ico_precompute_order.py` builds `order_L2_L3.npz` (0.815 MiB, 12.9 ms build).
- Self-contained (vf/vt included, version 1), idempotent.
- `ico_transition_precomputed.py`: exact match vs full argmin, 28x speedup at 100k.

### 1.3 Safety factor decision — ico_safety_factor.py
- Replayed adversarial insert stream; recommends safety factor 0.5
  (transition at half-budget). Colab validation on real tenant-per-cell corpus pending.

### 1.4 Fast rerank kernels — test_rerank_fast.py (4/4 PASS)
- `rerank_fast.py`: euclid via cdist 8.6x, dot via BLAS 72x, top-k two-stage.
- `rerank_gpu.py`: fp32 cosine 32-133x (synthetic), 109x (SIFT), 244x (SciFact);
  fp16 prefilter 97-100% agreement; `ResidentReranker` keeps V on GPU (9.6x extra).
- test_rerank_gpu.py: 4/4 PASS (local CPU torch).

### 1.5 MemCore dedup — test_memcore_dedup.py (2/2 PASS)
- `memcore_dedup.py`: 600 LC fruit items -> 296 canonical (50.67% dup).
- `memcore_ndcg_dedup.py`: absolute NDCG@10 = 0.6893, dup-tolerant = 0.720
  (+0.030) on 54,247 x 384 memcore corpus (Ico, 20% budget, 50 queries).
- Confirms "reranker loss" is partly near-duplicate tie noise.

### 1.6 RCU / epoch reclamation
- Local (2 CPUs): ico_rcu_stress.py, 10 swaps, 5 invariants zero (1 reader).
- Colab multi-reader: 4/8/16 readers, all invariants zero, max live 2, deferred 2.
  (time-sliced on 2 vCPU; true >2 parallelism needs a bigger machine.)
- Colab C11 atomic: rcu_publish.c compiles with TSan, runs PASS (I1-I5=0),
  TSan clean on 2 runs. (3rd run hit TSan infra "unexpected memory mapping".)

### 1.7 GPU bench (real data) — GPU-BENCH-REALDATA.md
- SIFT 200k x 128: CPU 2944 ms -> GPU 27 ms (109x) -> resident 2.9 ms (1004x).
- SciFact 5183 x 384: CPU 1059 ms -> GPU 4.3 ms (244x) -> resident 1.3 ms (790x).

---

## 2. What is NOT yet proven (gates from #6149, #7829, #949)

### 2.1 Product-level (tess-serve v1) — #6149
- [ ] RSS under actual load (not just mmap idle) — must measure with concurrent
      requests, not idle mmap.
- [ ] Soak test 1-4 hr on real workload.
- [ ] p99 latency under load (not just p50 of single query).
- [ ] Corrupted + fuzz verify overhead (verify path cost).
- [ ] MoE batch scaling: 1 / 8 / 32 concurrent (v2).
- [ ] Cache hit rate, NVMe bandwidth, tok/s baseline, $/M-tokens on real GPU cloud.
- [ ] Cost gate: cost >= 30% or latency <= 1.2x else not main product.

### 2.2 Temporal KV research gates — #7829
- [ ] VRAM reduction > 50% at context > 50k.
- [ ] Added latency <= 20% vs full KV.
- [ ] Accuracy parity with full KV.
- [ ] Deterministic across 100 runs.
- [ ] Multi-model coordinate universe (20-models vision) unproven.
- [ ] Negative port for window overflow; delta∝events on real inference (needs raw K/V hook).
- [ ] llama.cpp must not be modified before Phases 1-4 pass (#7826).

### 2.3 ANN / geometry layer
- [ ] Wire precompute asset (order_L*.npz) into the index build pipeline
      (currently standalone scripts; format ready, no consumer).
- [ ] Wire ResidentReranker into tesspack serve path / ANN rerank stage.
- [ ] True >2 parallel reader validation (needs machine with >= 4 cores;
      Colab free tier has 2 vCPU).
- [ ] Safety factor 0.5 validated on real tenant-per-cell corpus (not synthetic).
- [ ] Rerank SIMD/GPU in production: port BLAS path to C/Rust if latency
      gate requires it (Python BLAS may suffice for 200k; 1M+ needs C).
- [ ] Dedup applied to the 54k memcore corpus before embedding (ceiling ~0.706
      -> re-measure NDCG after dedup; currently only mapped, not re-embedded).
- [ ] NDCG re-run with larger query sample (currently 50 queries).
- [ ] Production atomicity: C11 RCU validated but soak/fuzz (TSan 60s per config)
      pending; grace-period cap is 1s (preempted reader risk).

### 2.4 Platform / ops
- [ ] Deploy ANN service correctly: ann_keepalive.py starts ann_search_batch.exe,
      not ann_search.exe; copy binary + restart (#7977).
- [ ] Embedding sidecar (e5-small on :18095) crashes on U+00A0 / U+2212;
      fold input to ASCII before embedding (#8199).
- [ ] Vault chat export scheduled (was stalled 25 days) (#7798).
- [ ] Magic-context compartment coverage: 77% of long sessions have no summary
      (#7995) — handoff/ledger must not assume block coverage.

### 2.5 Hardware / env
- [ ] llama-bench exits code 4 on 100k token runs on this hardware (#7833)
      — runtime limit; needs larger machine or shorter runs.
- [ ] 49.6 GB/s figure is Linux T4, not local (#949) — don't quote locally.
- [ ] GPU on WDDM box wins only resident multi-pass (#949) — local box loses
      3.6x on naive copies; T4 (Linux) is the real GPU target.

---

## 3. Recommended next steps (priority order)

1. **Wire residency into serve path** — biggest measured win (9.6x) with
   lowest risk; ResidentReranker is ready, just needs a consumer.
2. **Soak + p99 + RSS-under-load** on real tesspack (1-4 hr) — gates the
   product claim (#6149). Run on T4 Linux, not WDDM.
3. **Port RCU to production** with soak + fuzz (60s TSan per config) and a
   longer grace period (quiescent-state based, not 1s cap).
4. **True >2 parallel RCU** on a machine with >= 8 cores (Colab Pro / VM).
5. **Dedup + re-embed memcore** to lift the 0.706 ceiling; re-measure NDCG.
6. **Wire precompute asset** into index build; validate transition on real
   index (not just synthetic sift).

---

## 4. Files index

| file | purpose |
|---|---|
| GEOMETRY-RETRIEVAL-REPORT.md | full geometry retrieval report (9 sections) |
| READ_PATH_PLAN.md | read path plan/tasks |
| ico_precompute_order.py + test | precompute order asset |
| ico_transition_precomputed.py | transition using asset |
| ico_safety_factor.py | safety factor decision |
| rerank_fast.py + test | CPU fast rerank |
| rerank_gpu.py + test | GPU rerank (torch) |
| memcore_dedup.py + test + map | memcore dedup |
| memcore_ndcg_dedup.py | NDCG absolute vs dup-tolerant |
| ico_rcu_stress.py / _multi.py | RCU stress (local + colab) |
| rcu_publish.c | C11 atomic publish path |
| COLAB-PLAN.md | colab run plan |
| RCU-MULTI-READER-RESULTS.md | multi-reader receipts |
| RCU-C-RESULTS.md | C atomic receipts |
| GPU-RERANK-RESULTS.md | synthetic GPU bench |
| GPU-BENCH-REALDATA.md | real SIFT/SciFact bench |
| PRODUCTION-READINESS.md | this file |
