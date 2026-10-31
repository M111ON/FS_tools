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
- `phase_e_projection.py` — is the dir3 loss the projection or the direction set



## The first qrels-based router measurement (MIRACL Thai, 2026-10-30)

Everything above compares the router against brute force on the same vectors.
That measures self-consistency, not relevance — the memcore corpus has no ground
truth. This run is the first one scored against human relevance labels.

Setup: MIRACL Thai dev, 200 queries, 1,249 positives (all found), 20,000
distractors from the 542,166-doc corpus, embedded with the real ONNX int8 e5-small
(`passage: ` / `query: ` prefixes, the same `sanitize()` as `build_index.py`).
`scripts/miracl_local.py` fetches the raw files, `scripts/prep_miracl_th.py`
builds the set, `scripts/eval_router.py` scores it.

```
BASELINE 1 — brute force vs qrels:  rec10 0.987  hit10 0.995  mrr10 0.942
```

The embedder is fine (0.987 recall@10 over 20k candidates; the absolute number is
inflated versus a full 542k corpus, so do not compare it to MTEB).

Router cost on top of that, per level:

| level | router | probes | scan | rec10 | keep |
|---|---|---|---|---|---|
| 6  | kmeans | 1 | 17.7% | 0.712 | 0.704 |
| 6  | kmeans | 4 | 70.0% | 0.979 | 0.962 |
| 6  | dir3   | 1 | 17.6% | 0.576 | 0.522 |
| 14 | kmeans | 2 | 15.6% | 0.844 | 0.782 |
| 14 | kmeans | 8 | 57.4% | 0.977 | 0.968 |
| 14 | dir3   | 2 | 14.5% | 0.656 | 0.562 |
| 26 | kmeans | 1 |  4.0% | 0.641 | 0.572 |
| 26 | kmeans | 2 |  8.1% | 0.780 | 0.727 |
| 26 | kmeans | 4 | 16.3% | 0.905 | 0.853 |
| 26 | kmeans | 8 | 32.0% | 0.960 | 0.933 |
| 26 | dir3   | 1 |  3.8% | 0.275 | 0.214 |
| 26 | dir3   | 4 | 15.2% | 0.726 | 0.629 |
| 26 | dir3   | 8 | 31.0% | 0.912 | 0.845 |

Three things this settles:

1. **K=26 with 4 probes is the operating point**: 16.3% of the corpus scanned,
   rec10 0.905 against a 0.987 ceiling. The router costs about 8% of recall at
   one sixth of the scan.
2. **Going deeper pays.** Efficiency here means rec10 / scan. It rises from 4.0
   (K=6, one probe: 0.712 / 17.7%) to 5.4 (K=14, two probes: 0.844 / 15.6%) to
   9.6 (K=26, two probes: 0.780 / 8.1%). The 6 -> 14 -> 26 ladder is not
   decoration; each level buys a better scan/recall trade.
3. **Bucket balance is not routing.** Both K=26 routers are near the ideal bucket
   size (kmeans largest bucket 6.3%, `dir3` 7.2%, ideal 3.8%), yet `dir3` keeps
   0.214 at one probe against kmeans' 0.572. Evenly spread and still pointing the
   wrong way — the same lesson as phase B.

## Correction: what actually costs the dir3 arm

The first version of this section claimed that the two 0.214 values (phase B's
pure projection, and `dir3` keep here) were "not a coincidence". Two 3-decimal
matches from different corpora and different metrics are not evidence, and the
claim is withdrawn. `phase_e_projection.py` replaces it with a mechanism test:
hold K=26 and the probe count fixed and vary only how many PCA dimensions the
partition is built on.

| dims | var% | p=1 keep | p=2 keep | p=4 keep |
|---|---|---|---|---|
| 3 | 9.8 | 0.245 | 0.417 | 0.655 |
| 8 | 19.1 | 0.417 | 0.574 | 0.752 |
| 16 | 27.2 | 0.479 | 0.642 | 0.801 |
| 32 | 37.2 | 0.528 | 0.671 | 0.822 |
| 64 | 50.5 | 0.532 | 0.715 | 0.838 |
| 384 | 100.0 | 0.572 | 0.728 | 0.853 |

keep climbs monotonically with dimension count, so the projection is the loss. And
at three dimensions a learned partition and the fixed direction set are
statistically indistinguishable: kmeans-3d 0.245 versus `dir3` 0.214, a paired
difference of +0.031 with a 95% CI of [-0.007, +0.069] — not separated.

So the honest statement is not "geometry cannot route". It is: **384 dimensions
cannot be compressed to 3 and still route, whatever the partition looks like.**
The 26 faces are an addressing structure, not a routing structure — a cleaner
reason for the two-layer split than the one originally given.

Paired bootstrap on the same 200 queries (95% CI of the mean difference):

```
kmeans-384d p=4 - kmeans-3d  p=4    +0.198  [+0.163, +0.234]  real
kmeans-384d p=4 - dir3       p=4    +0.225  [+0.185, +0.261]  real
kmeans-384d p=1 - kmeans-3d  p=1    +0.327  [+0.276, +0.374]  real
kmeans-3d   p=1 - dir3       p=1    +0.031  [-0.007, +0.069]  not separated
```

Also corrected: a single arm's interval at rec10 0.905 over 200 queries is about
+-4% at 95% (binomial SE 0.021), not the +-3% claimed earlier. Paired comparisons
are far tighter, and those are what the table above uses.

Limits to state before quoting any of this:

- Positives are 6.1% of this corpus (1,249 / 20,339); in the full 542k corpus they
  would be 0.23%. Topic clusters here are therefore tighter than reality and the
  numbers flatter any partition. A 50k-distractor rerun (positives 2.4%) checks the
  direction of that effect.
- The qrels hard negatives were **not** included: `miracl_local.py` keeps only rows
  with label 1, so every non-positive is a plain distractor.
- `kmeans` is retrained on the whole corpus each run. The frozen-centroid path
  production would use is measured in phase C on memcore, not on MIRACL.
- Distractors are every 20th document, not a uniform sample, so scan percentages
  lean towards the front of the corpus.
- These numbers do not transfer to the memcore corpus: MIRACL is clean prose with
  separable topics, while memcore mixes code and notes. Use MIRACL to confirm the
  router mechanism, and tune the real operating point on labelled memcore data.

Not tested: the full 542k corpus, efSearch-style reranking, the shape-phase pool,
and whether kmeans buckets survive an embedding swap.

## Phase F — D1 decided: address on the CENTRED vector (2026-10-31)

Owner decision D1 = B. `phase_f_centred.py`, 20,000 rows, mean frozen on the
first 40% (what production does: freeze at epoch start, append after).

### Address: the win is LENGTH, not uniqueness

| arm | unique (12 letters) | collided | largest group | mean letters/lookup |
|---|---|---|---|---|
| raw | 95.78% | 844 | 34 | **7.19** |
| centred, frozen mean (40%) | 98.67% | 265 | 15 | **3.83** |
| centred, oracle mean (100%) | 98.63% | 274 | 11 | 3.82 |

Centring does not rescue uniqueness — it was already 95.78% at 12 letters. It
**halves the address length** (7.19 -> 3.83 letters, -47%), which is what the
head+tail ladder actually pays for.

### Correction to an earlier claim

An earlier note said the raw address space "collapses, 5.79% unique, 94% of rows
collide". That is **not true at 12 letters** and must not be quoted. The 5.79%
figure belongs to a much shorter address (4 levels x 16 values = 65,536 keys),
not to the 12-letter one measured here. The real raw cost is length (7.19 of 12
letters), not collision.

### Freezing the mean is mandatory, measured

Recomputing the mean on the full 100% moves **18,442 / 20,000 addresses (92.21%)**.
The mean is therefore part of the epoch: `epoch = (embedder_id, mean_hash)`.
Change either one and a new epoch opens; old addresses stay valid inside the old
epoch (`include/chain_ident.h` already carries the field).

### Routing: no gain, so do not claim one

kmeans K=26, truth = brute force in the SAME space as the arm.

| probes | raw recall@10 / scan | centred recall@10 / scan |
|---|---|---|
| 1 | 0.6225 / 4.5% | 0.5985 / 4.3% |
| 2 | 0.7810 / 8.9% | 0.7625 / 8.6% |
| 4 | 0.8755 / 17.5% | **0.8820 / 16.5%** |

Occupancy is more even (max 6.9% -> 6.1%, min 0.5% -> 1.4%), but recall is flat:
slightly worse at 1-2 probes, marginally better at 4. **Centring is an addressing
change, not a routing change.**

### The cost that must be stated with every number

Top-10 overlap raw vs centred = **0.5395**. Only 5.39 of 10 nearest neighbours are
the same after centring — the metric becomes Pearson correlation, not cosine.
Every recall figure measured on raw vectors changes meaning; do not compare across
the two without saying so.

### What D1 changes in the code

- Address derivation takes a frozen mean (384 floats = 1.5 KB per epoch).
- `epoch` must carry `mean_hash`; a mismatch refuses to resolve rather than
  silently answering from a different space.
- Nothing in the search path changes: the index keeps raw vectors and raw cosine.

## Phase G — D2 phase 1: the f16 sidecar passes its gate (2026-10-31)

`phase_g_f16_gate.py` + `ann/scripts/convert_jsonl_to_f16.py`. Offline only: no
production path was touched.

Corpus: 54,064 rows x 384 (the live index after the D3 delete; `ann-sync`
rebuilt it at 19:47 from `entries JOIN embeddings`).

### Load and size

| | size | load |
|---|---:|---:|
| JSONL (text) | 212.3 MB | 7.74 s |
| f16 sidecar | **39.6 MB** | **0.08 s** |

5.4x smaller, 96x faster to load. (An earlier note said "parse ~50 s" — that was
wrong for this file; the measured cost is 7.74 s.)

### Loss

```
f32 sidecar bit-identical to the JSONL : True     <- the container itself is lossless
element |dx|  max 6.104e-05   mean 7.783e-06
cosine        min 1.000000 (6dp)   rows below 0.9999: 0
```

The 0.09% figure quoted earlier was per-element relative precision, not loss of
recall. Measured neighbour loss:

```
top-10 f16 vs f32 = 0.9985   (9.98 of 10 neighbours identical)
```

### Container recall, kmeans K=26, truth = f32 brute force

| probes | f32 recall@10 / scan | f16 recall@10 / scan | drop |
|---|---|---|---|
| 1 | 0.6180 / 4.3% | 0.6165 / 4.3% | +0.0015 |
| 2 | 0.7520 / 8.5% | 0.7525 / 8.5% | -0.0005 |
| 4 | 0.8790 / 16.7% | 0.8780 / 16.7% | +0.0010 |

Occupancy identical (max 6.9%, min 1.5%, 26/26 used).

**Gate 0.5%: PASS.** Worst drop 0.0015, and at probes=2 the f16 arm is
*infinitesimally better* — i.e. the difference is noise, not a trend.

### Two corrections that change the phase-2 plan

1. **The JSONL is not the source of truth.** `ann-sync` (scheduled) runs
   `build_index.py`, which reads `entries JOIN embeddings` and writes
   `ann_index.jsonl`. Vectors live in the DB, so a lost or corrupted JSONL is
   rebuilt with **no re-embedding**. The earlier statement "JSONL is the source
   of truth, writer failure = re-embed 542k docs" was wrong.
2. **A stale sidecar is undetectable with this header.** Row i of the binary is
   row i of the JSONL by position only; `ann-sync` rewrites the JSONL on a
   schedule, and a reorder would silently serve the wrong vector for every row
   after the first change. Phase 2 must add a guard — the cheap one is 16 bytes
   of source size + mtime in the header, refused on mismatch (the sync always
   rewrites the whole file, so mtime is a reliable signal here).

Not done (phase 2, needs the guard first): reader in the load path, writer in
`build_index.py`, bench scripts, backward compatibility.
