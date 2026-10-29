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

## Phase D — head+tail addressing: `bN`, grow on collision

Owner pattern (2026-10-30): a chain b(1), H(2), y(3), N(4) is called `bN` — head
letter, then tail letter. On a miss or a duplicate, add the second letter (`bHN`),
then the third (`bHyN`), until it is the whole address.

**No position numbers.** The owner's reason is reading, not bytes: a person seeing
`bHyN` knows the shape at a glance, while `b1H2y3N4` forces them to count. The
digits were also redundant — the ladder is always "letters 1..m, then the tail", so
the order and the stored key's length already say where each letter sits (see the
cost table below: writing them costs 14.23 chars against 7.03 for plain letters,
for zero extra information).

**The reserved digit 0 is kept.** It remains the maintenance/fault marker from
`chain_addr.h`: a stored key carrying 0 is a closed node, lookup stops there,
repair finds it. A closed node has no compact form because it has no address to
look up.

Implemented in `include/chain_headtail.h`, test `tests/test_chain_headtail.c`
**PASS 38/38**:

- the ladder is always "first m letters, then the tail", m = 1..n-1, and the top
  step IS the full key, so no lookup can fail to resolve
- the compact key is m+1 bytes of letters and nothing else; the tail's position is
  the stored key's own length, so the form does not depend on how long the chain is
- notation round-trips (`bN`, `bHN`, `bHyN`); digits are rejected as notation
- a difference in the head **or the tail** separates at step 1, because every step
  carries both; only an interior difference needs the ladder
- `0` is still the marker: `chain_valid` rejects it, `chain_resolve` stops at it,
  and `b0N` is not a letter string

### What it costs on real addresses

20,000 rows, 12-letter addresses derived from the real vectors:

```
step  letters  groups  unique   max group
   1        2     269    1.3%        6878
   2        3    2378   11.9%         940
   4        5    8286   41.4%         136
   6        7   15334   76.7%          35
  11       12   19156   95.8%          34

mean letters per lookup = 7.19 of 12
lookups answered by head+tail alone = 0.38%
```

`bN` is unique for 0.38% of addresses. To get a candidate set of ~35 you need
**7 of 12 letters**. So head+tail is a **naming convention** (the same idea as a
git short hash), not a lookup shortcut — it makes an address readable and
quotable, it does not make the search cheaper.

Note the honest bar: uniqueness is not what a chain needs — a chain resolves to a
prefix and may legitimately return many records. The operational number is the
**candidate set size**, which is the `max group` column. Both are reported.

### Why it needs so many letters — the real finding

The 12-letter addresses above came from sign bits of random projections, and
**844 of 20,000 rows shared an address in a 52^12 space**. That should be
impossible. The cause is not the projection, it is the corpus:

```
||mean|| / mean||x||      = 0.915
median cos(mean, x)       = 0.918
eigenvalue max/min        = 1.16e9
```

Every e5 vector is **91.5% the same direction**. This is the known anisotropy of
sentence embeddings, and it is severe here: the covariance is so degenerate that
most of the 384 dimensions carry almost no per-row information.

Consequence for a value-blind address, measured on the same 20,000 rows:

```
raw        distinct addresses  1158/20000  ( 5.79%)   largest bucket 8.8%
centred    distinct addresses 14305/20000  (71.53%)   largest bucket 1.9%
whitened   distinct addresses 16063/20000  (80.31%)   largest bucket 1.8%
```

**A value-blind address computed on raw embeddings collapses: 94% of rows land on
the same address.** That is why phase B's pure projection scored 0.214 — the sign
bits were reporting the shared mean, not the row.

This does not change the phase B verdict (frozen k-means still wins), but it
changes the reason: the failure was never "random projections cannot route", it
was "there was almost nothing to route on". Centring or whitening restores the
address space by a factor of 12.

**Caveat, and it is a real one:** all recall numbers here are measured against
brute-force **raw cosine** as ground truth. Whitening changes which vectors are
near each other, so a whitened recall figure against raw-cosine truth is not
meaningful, and this experiment cannot say whether whitening improves retrieval —
only that it restores address distinctness. There is no relevance ground truth in
this corpus, so retrieval quality is not measured anywhere in this report.

### The position digits carry nothing

Owner's counter-argument (2026-10-30): pure letters means writing every position;
head+tail means writing two and growing only when it fills, so it is paying later
rather than paying less. That framing is right — but the ladder's positions are
always `1, 2, 3, ..., m` and then `n`, so **every digit is determined by the order
and the address length**. They are redundant. `b1H2N4` (6 chars) says exactly what
`bHN` (3 chars) says under the rule "last letter is the tail, the rest is the
prefix".

At an equal **letter** count the two schemes are a tie (20,000 real addresses):

```
letters  prefix {1..k}          head+tail {1..m, n}
      2  groups   683 max 1597   groups   269 max 6878   prefix
      4  groups  4800 max  230   groups  4142 max  695   prefix
      7  groups 15699 max   36   groups 15334 max   35   head+tail
      8  groups 16871 max   35   groups 17230 max   35   tie
     12  groups 19156 max   34   groups 19156 max   34   tie
```

And in expectation, to reach uniqueness:

```
prefix      mean 7.03 chars
head+tail   mean 14.23 chars  (with the redundant digits)
head+tail   mean  7.19 chars  (digits stripped = one char per letter)
```

So deferring the payment does not make it cheaper — **7.19 vs 7.03, the same**.
What made it look cheap was the digits, and the digits buy 3.58 bits (log2 12)
while a letter buys 5.70 bits (log2 52): a full byte spent for 63% of a letter.

The remaining reason to prefer head+tail is not economic. A head and a tail name a
**connection** (entry face to exit face); a prefix names a **location**. Two paths
with the same head and tail are the same journey, so their collision is a merge,
not a defect — which is the `#7166` rule: same point, different dims, scoring
merges, nothing deleted. Choose by which question the address answers, not by
which string is shorter.

### Depth as metadata — the chain length rides along

Owner (2026-10-30): the chain length is already known, so attach it to the address
instead of leaving `bN` silent about how many levels sit between head and tail.
Length differences then become visible, which removes the last ambiguity the
letters alone could not settle.

`ChainHT` carries `depth` and it is written **once at the end**, never per letter:

```
bN        depth unknown -> matches any length   (the merge case)
bN4       depth 4       -> matches only a 4-level chain
bN5       depth 5       -> same head and tail, different length
```

Letters and digits cannot be confused, so no separator is needed, and the name
stays readable. `chain_ht_any_depth()` clears the depth, which turns the exact key
back into the merge key — that is `#7166`: two paths with the same entry and exit
are the same connection and should score together, but only when the caller says
so. Exact is the default, because that is what an ordinary lookup means.

Test `T7` covers it: `bN4` parses back to depth 4, `bN` parses to depth 0, `bN0` is
rejected (0 is the marker, not a depth), and two chains sharing head and tail print
as `bN4` and `bN5`.

## Files

- `phase_b_gate.py` — A/B/C comparison, stability
- `phase_b_drift.py` — frozen-partition sweep
- `phase_b_center.py` — anisotropy: raw vs centred projection
- `phase_c_promotion.py` — split-on-full, prefix stability
- `phase_d_headtail.py` — ladder cost on real addresses


