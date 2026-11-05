# Mixed workload soak — queries + concurrent ingest (Colab T4)

Environment: Google Colab, Tesla T4, torch 2.11 +cu130.
Workload: 4 reader threads (continuous queries) + 1 writer thread (ingest
+1000 vecs every 2s, RCU-style atomic swap of ResidentReranker). 5 min.

## Result
- duration: 300 s
- queries served: 13,762,272 (45,822 q/s)
- ingests: 149
- query latency: p50=1.08 ms  p95=2.01 ms  p99=2.49 ms  max=19.97 ms
- ingest build: p50=11.99 ms  p95=18.68 ms  p99=21.45 ms  max=40.72 ms
- RSS drift: +0.0 MB (no leak)
- traced peak: 67.8 MB
- invariants: I1=0 I2=0 I3=0 I4=0 I5=0
- RESULT: PASS

## Interpretation
Real-world mixed workload (continuous queries + concurrent ingest) validates:
- RCU-style atomic swap keeps query p99 at ~2.5 ms even under write contention.
- Ingest build cost ~12-21 ms (ResidentReranker rebuild) is acceptable.
- No memory leak, no invariant violation across 13.7M queries + 149 ingests.
- This is the closest to production behavior tested so far.

## Note
- V size oscillates 50k-55k (cap: NCAND + 5*INGEST_BATCH), so memory is bounded.
- True production soak (full tesspack serve path) still pending per #6149.
