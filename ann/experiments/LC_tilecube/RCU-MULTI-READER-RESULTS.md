# RCU multi-reader stress — Colab results (t7092dac9)

Environment: Google Colab (free CPU runtime, many cores). Synthetic sift_base.fvecs
(50k x 128 float32). Protocol identical to ico_rcu_stress.py (1 writer + N readers,
10 swaps, deliberate slow reader every 12 reads with 120 ms hold).

| readers | reads served | reclaimed/deferred | max live | crit p50 (ms) | crit p95 (ms) | slow reads | invariants I1-I5 |
|---|---|---|---|---|---|---|---|
| 4  | 559   | 9 / 2 | 2 | 5.90  | 127.84 | 46  | all 0 |
| 8  | 1705  | 9 / 2 | 2 | 24.44 | 145.65 | 141 | all 0 |
| 16 | 6372  | 9 / 2 | 2 | 58.07 | 178.32 | 529 | all 0 |

## Conditions (all PASS at every reader count)
1. pre-swap reader finishes on old index — True (reads per gen > 0 for gens 1..9)
2. post-swap reader sees the new index — True (gen 10 read)
3. reclaim only when unheld — True (max live stays 2 even at 16 readers; deferred=2)

## Notes
- max simultaneously live versions stayed at 2 across 4/8/16 readers — the grace
  period correctly defers reclamation while any reader holds the old version.
- invariants I1-I5 (snapshot integrity, object identity, no use-after-reclaim,
  no reclaim-while-held by identity, no stale generation) all zero at every scale.
- writer critical path (build + publish) scaled with reader contention: 10-swap
  total wall-clock ~3s (4 readers) to ~28s (16 readers) — contention, not
  correctness, is the scaling limit.
- Python/GIL atomicity caveat still applies (memory #8245): this validates
  protocol logic, not the memory model. Production claim requires the C/Rust
  implementation (tfb3b3e89).
