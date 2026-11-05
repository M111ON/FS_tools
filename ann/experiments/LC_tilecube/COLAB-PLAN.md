# Colab plan — RCU multi-reader + production atomic swap

Context: local box has 2 logical CPUs → only 1 reader + 1 writer exercised
(ico_rcu_stress.py, 10/10 swaps, 5 invariants zero). Tasks tfb3b3e89 and
t7092dac9 need a bigger machine. Run on Colab (free CPU runtime) in this order.

## Task A — RCU multi-reader stress (t7092dac9)

Files to upload:
  - ann/experiments/LC_tilecube/ico_rcu_stress_multi.py  (NEW: --readers via RCU_READERS env)
  - ann/experiments/LC_tilecube/ico_rcu_stress.py        (reference, single-reader)
  - ann/experiments/LC_tilecube/ico_lifecycle.py
  - ann/experiments/LC_tilecube/ico_cost_quality.py       (icosphere, vert_adj)
  - sift/sift_base.fvecs (or a smaller subset; N=50k needs ~25 MB)

Run matrix:
  RCU_READERS=4  python ico_rcu_stress_multi.py
  RCU_READERS=8  python ico_rcu_stress_multi.py
  RCU_READERS=16 python ico_rcu_stress_multi.py

Collect per run:
  - reads per generation (distribution)
  - reclaimed / deferred counts, max live versions
  - grace-period latency (hold read duration)
  - writer starvation (time between publishes)
  - reader critical-section p50/p95 under contention
  - invariants I1..I5 (must stay 0)

Pass criteria: all 5 invariants zero at 4/8/16 readers; max live versions
bounded (<= readers+1); deferred reclaims > 0 (deliberate slow reader fires).

## Task B — Production atomic swap in C/Rust (tfb3b3e89)

Goal: replace the Python reference assignment (GIL-atomic only) with a real
atomic pointer swap + release/acquire fences + epoch-based grace periods.

Deliverables:
  1. rcu_publish.h / .c (or .rs): 
       - atomic_store_explicit(&current, ver, memory_order_release)
       - epoch bump under the same lock as publish (one atomic step)
       - grace period: readers increment a per-CPU counter on enter, decrement
         on leave; reclaimer waits until all counters from the target epoch
         reach zero (bounded by RCU_GRACE_MS, default 1000)
  2. Reader API: rcu_dereference (acquire load) + rcu_read_lock/unlock
  3. Stress harness in C: N readers threads, 1 writer, asserts I1..I5 with
     object-identity check (not epoch arithmetic) for safety
  4. Fuzz: ThreadSanitizer build; run 60s per config

Validation:
  - TSan clean (no data races)
  - I1..I5 zero at 4/8/16 threads
  - bounded grace period (no reader blocks reclamation forever)
  - publish latency p99 < 1 ms

## Notes
  - Python version validated protocol LOGIC only (memory #8245): GIL atomicity
    != memory model. C/Rust run is mandatory before production claims.
  - Do not modify llama.cpp (memory #7826) — RCU is for the ANN index layer.
  - Keep the two lessons from the first harness failure:
      (a) epoch bump + publish must be ONE atomic step under the same lock
      (b) reclamation decision may use epochs; safety CHECK must use object identity
  - Colab has no persistent storage beyond session; push results (logs + .json)
    to the repo or save to Google Drive before session ends.
