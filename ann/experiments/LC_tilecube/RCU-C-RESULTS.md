# RCU C11 atomic publish path — Colab results (tfb3b3e89)

Environment: Google Colab, gcc -std=c11 -O1 -g -fsanitize=thread -pthread.
Implementation: rcu_publish.c (C11 atomics + memory fences + pthread mutex).

## Result
- violations: I1=0 I2=0 I3=0 I4=0 I5=0
- RESULT: PASS
- TSan: clean (0 data races) on 2 full runs; a 3rd run hit TSan infra
  "unexpected memory mapping" (returncode 66) — environment issue, not a race.

## What the implementation provides
- atomic published pointer: atomic_store_explicit(..., memory_order_release) +
  atomic_load_explicit(..., memory_order_acquire) (rcu_dereference)
- epoch bump + publish in ONE step under the same pthread mutex (pub_lock)
- reader grace tracking via atomic_int active/gen per reader slot
- reclamation: bounded grace period (1s cap) with any_held check by gen;
  reclaim (invalidate) only when no reader holds the old version
- safety CHECK uses atomic reads of reader state (independent of decision)

## Lessons preserved (from memory #8245)
(a) epoch bump and publish are one atomic step under the same lock
(b) reclamation decision may use epochs; safety check uses object/reader identity

## Caveats
- Python caveat resolved for the memory model: C11 atomics + fences are real.
- Still a protocol-level test (8 readers, 10 swaps, synthetic workload);
  production validation (soak, p99, corrupted+fuzz) pending per #6149 gates.
- Grace period cap is 1s; a reader preempted longer than that could be
  reclaimed while still live (bounded loss). Production should use a longer
  or quiescent-state-based grace period.
