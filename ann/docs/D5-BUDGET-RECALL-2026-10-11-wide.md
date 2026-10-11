# D5 — budget vs recall, WIDE sample (2026-10-11 late)

**Question (handoff caveat #1):** the 24000 default came from **40 queries**.
Is it still the right knee at **300 queries across 2 corpora**?

**Verdict: YES — 24000 confirmed.** The 40-query knee reproduces at 7.5× the
sample on both corpora. No change to the default.

Method unchanged from `bench_budget_recall.py`: raw-`vec` queries (the row's own
embedding, no sidecar drift) vs a numpy brute-force cosine oracle, production env
(`ANN_WALK_PRUNE=1`, `LZ_ROUTE_BUCKETS=5`, `ANN_WALK_FLOOR=0.5`). Scratch servers
only; live :8096 untouched.

---

## memcore corpus — 59756 entries, 300 queries

| budget | scored | trunc | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| 8000 (old) | 8000 | 1 | 0.5533 | 0.5140 | 0.5080 |
| 16000 (knee) | 16000 | 1 | 0.9767 | 0.8993 | 0.8770 |
| **24000 (default)** | 24000 | 1 | **1.0000** | **0.9667** | **0.9603** |
| 32000 | 32000 | 1 | 1.0000 | 0.9860 | 0.9820 |
| full (44938 scored / 59756) | 44938 | 0 | 1.0000 | 0.9993 | 0.9980 |

## sample corpus — 2881 entries, 300 queries

| budget | scored | trunc | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| 500 | 500 | 1 | 0.6933 | 0.6320 | 0.5987 |
| 1000 | 1000 | 1 | 1.0000 | 0.9393 | 0.9263 |
| 2000 | 2000 | 1 | 1.0000 | 0.9960 | 0.9957 |
| full (2507 scored / 2881) | 2507 | 0 | 1.0000 | 0.9973 | 0.9967 |

---

## What the wide sample changed

1. **24000 reproduces.** memcore R@10 = 0.9603 at 300q vs 0.9600 at 40q —
   the knee is stable, not a small-sample artifact. The old 8000 still costs
   **0.4523 of R@10** (0.5080 → 0.9603): the fix is loading-bearing at both
   sample sizes.
2. **The knee is corpus-size-relative, not absolute.** On 2881 entries, the
   default 24000 > N so it is effectively a full scan; the useful knee is
   ~2000 (= 0.69·N) where R@10 = 0.9957. On 59756, 24000 = 0.40·N. **A fixed
   default is the wrong shape for a size-varying corpus** — the honest form is
   a fraction of N (memcore knee ≈ 0.40·N, sample ≈ 0.69·N). Not changed here
   (would alter live behaviour); recorded as the next lever.
3. **Both corpora saturate below a literal full scan.** memcore's pruned walk
   scores 44938 of 59756 (75%) at `trunc=0` — the covered-entry ceiling, same
   number the handoff reported. R@10 at that ceiling is 0.9980, so 24000
   (0.9603) gives up ~0.038 to buy ~2.6× fewer scored items.

## Tooling bug found and fixed (same session)

`bench_budget_recall.py --budgets 0` is documented "0 = unbounded", but after the
widen-trap removal `spawn()` only sets `LZ_SEARCH_BUDGET` when `budget > 0` — so
**`0` silently fell back to the 24000 default**. My first sweep's "unbounded"
row printed `scored=24000`, identical to the 24000 row. Fixed: `0` now maps to
`N` explicitly (verify run: sample `--budgets 0` → scored=1659, a real full
scan). Anyone who re-runs the old command got a wrong "unbounded" row.

## Caveats (honest)

- Query set = random index rows (same protocol as the 40-query run). Real
  queries are not uniform draws; the *shape* is right, the absolute number is
  a ceiling estimate, not a promise for live traffic.
- The 44938 / 2507 "full" ceilings are the pruned walk's covered set, not all N.
  A literal all-N scan is not what the serve path does at any budget.
- The sample corpus's shipped `ann_anchors.bin`/`ann_index.order` were **stale**
  (`routed:0` → brute → R@10 = scored/N, the exact uniform-sample shape the
  handoff warns about). I regrew them (`POST /v1/state/regrow`) before
  measuring. Worth a look: `data/sample/` anchors were never refreshed after
  the index was rebuilt 2026-10-08.
- Server spawn cost dominates: 5 budgets ≈ 3–4 min on memcore (index load
  ~20–40 s each). Nothing here writes the live service.

## Reproduce

```
cd I:\tools\ann
python bench_budget_recall.py --queries 300 --ks 1,5,10 \
    --budgets 8000,16000,24000,32000,0 --scratch I:\tmp\ann_budget_memcore
# sample (regrow anchors first):
python bench_budget_recall.py --index I:\tools\ann\data\sample\ann_index.jsonl \
    --queries 300 --ks 1,5,10 --budgets 500,1000,2000,0 --scratch I:\tmp\ann_budget_s5
```

Raw logs: `docs/D5-2026-10-11-memcore-300q.txt`,
`docs/D5-2026-10-11-memcore-fullscan.txt`, `docs/D5-2026-10-11-sample-300q.txt`.
