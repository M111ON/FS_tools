# Soak test — rerank path (1 hour, Colab T4)

Environment: Google Colab, Tesla T4, torch 2.11 +cu130.
Workload: ResidentReranker, 64 queries x 50k candidates (d=128), sustained 60 min.

## Result
- minutes: 60
- total queries: 2,619,370
- p99 first 5 min: 4.08 ms
- p99 last 5 min: 3.21 ms
- drift: 0.79x (stable, no degradation)
- RSS drift: +0.0 MB (no leak)
- traced peak: 2.4 MB
- total errors: 0
- RESULT: PASS

## Pass criteria
- no crash: yes
- p99 stable (drift < 2x): yes (0.79x)
- RSS drift < 100 MB: yes (0.0 MB)
- 0 errors: yes

## Interpretation
The ResidentReranker (V resident on GPU) holds p99 ~3-4.7 ms over 2.6M queries
with zero errors and zero memory drift. No degradation over 1 hour. This satisfies
the soak gate (#6149) for the rerank stage on T4-class hardware.

## Note
- RSS reported as 1961.5 MB throughout (torch CUDA context resident); drift 0.
- This is the rerank stage only, not the full tesspack serve path. Full
  tesspack soak (RSS under load, p99 end-to-end, corrupted+fuzz verify) still
  pending per #6149.
