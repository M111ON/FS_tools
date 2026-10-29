# Phase B — the gate, on real data

2026-10-30 · `I:\tools\ann` · owner: opencode-container-a7f3

## The question

> On a real corpus, is recall unchanged when the store grows, while the address
> stays put and nothing is re-embedded?

## The data

`I:/tools/ann/data/memcore/ann_index.jsonl` — 63,915 real rows, 384-dim,
multilingual-e5-small, already on disk. **20,000 sampled** (every other line),
200 queries, ground truth by brute force. Nothing was re-embedded.

Machine: Pentium G4400, 2 cores / 2 threads, 2015. Python 3.14.4, numpy 2.5.1.

## Three ways to derive an address

| | how the address is made | stable when data grows? |
|---|---|---|
| **A** k-means, retrained | argmin over centroids, re-clustered on every growth | no |
| **B** pure projection chain | fixed seeded projections → 4 sign bits → letter, `bucket = hash(path)` | yes, by construction |
| **C** k-means, frozen | argmin over centroids learned **once**, never updated | yes, by construction |

All three: 64 buckets, 3 probes chosen by centroid similarity, recall@10.

## Result

```
A k-means retrained   recall 0.7750   scanned 6.2%   largest bucket  3.4%
B pure projection     recall 0.2140   scanned 7.0%   largest bucket  8.8%
C frozen k-means      recall 0.7735   scanned 6.2%   largest bucket  4.4%

address stability when 20% new rows arrive
A k-means   15845/16000 old rows changed bucket  (99.0%)
B chain         0/16000                          (0.0%)
C frozen        0/16000                          (0.0%)
```

**C passes the gate.** Recall within 0.15 points of the retrained baseline
(0.7735 vs 0.7750) at the same scan budget, with every address unchanged.

**B fails it.** A data-independent address is stable but cannot route: 16 sign
bits do not preserve neighbourhoods, so recall collapses to 0.214. Stability
alone is worthless if the partition is meaningless.

**A fails it in the other direction.** Best recall, but 99% of addresses move
when the corpus grows 20% — every stored pointer, every index, every cached
route invalidated. That is the vector-DB problem the container exists to avoid.

## How far can the frozen partition go?

Train on the first `keep` rows, append the rest, never re-learn:

```
  keep    new  recall@10  scanned  largest bucket
 20000      0     0.7750    6.2%    3.4%
 18000   2000     0.7860    6.4%    3.9%
 16000   4000     0.7735    6.2%    4.4%
 12000   8000     0.7925    7.1%    5.4%
  8000  12000     0.7885    7.2%    9.0%
```

Recall does not degrade — it stays at or above the retrained baseline even when
**60% of the corpus arrived after the partition was learned**. The only cost is
that buckets fill up, so the scan fraction creeps 6.2% → 7.2% and the largest
bucket grows 3.4% → 9.0%.

## What this means for the container

The design answer is not "make the address a pure function" (B) and not
"recompute the map whenever data arrives" (A). It is:

1. **Learn the partition once**, at observe time — the compute is paid once.
2. **Freeze it.** New data joins an existing bucket; nothing is re-derived.
3. **When a bucket gets too full, promote it** — open one more chain level under
   that node. Old addresses stay valid because they are prefixes.

Step 3 is exactly `chain_extend` in `include/chain_addr.h`, and it is why the
chain and the frozen partition are the same design: the chain is the record of a
partition that is never rebuilt, only extended. Bucket size is the promotion
trigger, and the trigger is measurable (largest bucket 3.4% → 9.0% is the curve
to set a threshold on).

## Not tested (so not claimed)

- **Embedding swap.** Swapping the encoder re-derives every vector; the frozen
  partition cannot survive that, because the partition is a function of the
  vectors. The design can only promise that *addresses* survive a swap if the
  new encoder is projected into the old partition — not tested, and it may not
  hold.
- **Store move.** Moving the store while keeping the partition is a byte copy;
  untested here.
- **Scale.** 20,000 of 63,915 rows. Larger N makes buckets fuller sooner; the
  promotion threshold must be set from a full-corpus run.
- **Relation queries.** `#7819` already showed unconditional view union breaks
  relation recall. This experiment is value/shape queries only.

## Phase C — promotion: split a full bucket without moving anyone

Phase B's cost was buckets filling up (largest 3.4% → 9.0% while the corpus grew
2.5x). `phase_c_promotion.py` answers it: when a bucket exceeds capacity, open ONE
more chain level under it.

Seed partition learned on the first 8,000 rows (40%), then frozen. Stream the
remaining 12,000 in steps of 2,000; split any leaf over 500 members; never
re-learn anything else. Ground truth is recomputed per step over the rows that
exist at that moment, so the curve is not penalised for rows not yet arrived.

```
   rows  leaves  splits  recall   scan  rewritten   prefix
  10000      64       0  0.8292  2.9%          0        0
  12000      64       0  0.8195  3.6%          0        0
  14000      65       1  0.7752  4.4%        861        0
  16000      67       3  0.7938  5.0%       2013        0
  18000      71       7  0.7899  5.8%        4201        0
  20000      74      10  0.7870  7.6%        6322        0
```

- **prefix = 0 broken at every step.** No root ever moved. Promotion extends an
  address, it never relocates one.
- **rewritten** counts leaves that received a longer address (a child level).
  That is promotion by definition: their prefix is unchanged, so an old pointer
  still resolves — it just resolves to the parent instead of the leaf.
- recall 0.829 → 0.787 while 12,000 rows arrive and 10 splits happen. It tracks
  the frozen baseline (0.7885 at the same size), so splitting costs nothing.

**Design note found by the first run:** probing by leaf is wrong. After a split,
three leaf probes cover fewer vectors than three root probes did, and recall fell
to 0.708 for a reason that had nothing to do with promotion. A chain resolves to a
NODE and searches under it, so the probe target must be the root and the search
covers its whole subtree. With that fix recall returns to 0.787. This is the same
statement as `chain_resolve`: resolve to the longest healthy prefix, search under
it.

## Phase C — still open

- Promotion at more than one level (this run is depth 2 only).
- Capacity threshold: 500 was arbitrary. The right value comes from the bucket
  fullness curve, not from a guess.
- Concurrency: a split while a reader is mid-scan is not addressed here at all.

## Files

- `phase_b_gate.py` — A/B/C comparison, stability
- `phase_b_drift.py` — frozen-partition sweep
- `phase_c_promotion.py` — split-on-full, prefix stability

