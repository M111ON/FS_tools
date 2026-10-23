# HANDOFF — 2026-10-23 (session 2) — sanitizer 503 class + hourglass/pair shift algebra

From: opencode-ann-h4q (OpenCode, ANN/DWGLS)
To: next session (ann follow-on)
Repos: `I:/tools/ann` (branch `main`, commit `bd1ce04` UNPUSHED at handoff time),
`I:/DWGLS-native-fs` (branch `feat/geo-native-fs`)
Companion receipts: `I:/DWGLS-native-fs/docs/ANN-UPGRADE-2026-10-23-PHASE0.md`
(append section "Session 2"), `I:/tools/ann/docs/ANN-UPGRADE-2026-10-23-PHASE0.md`

---

## 1. Done — sanitizer 503 class (ann, commit bd1ce04)

Root cause, code-traced: `lz_sanitize` asked the vocab bitmap **before** NFKC. That
bitmap claims every one of the 2682 NFKC-delta codepoints is in vocab, so raw delta
codepoints (roman numerals, vulgar fractions, halfwidth katakana, …) were handed to
the tokenizer and the whole class 500/503'd.

Fix: NFKC first, gate the **output** codepoints, drop only what the vocab cannot
represent. `I:/tools/ann/src/ann_search.c:407`.

The bitmap was guessed; now derived:
- `scripts/probe_vocab.py` reads the sidecar's own `tokenizer.ggml.tokens` →
  `scripts/nfkc_vocab.json` — **13,829 representable / 52,170 dropped**.
- `scripts/gen_nfkc_table.py` emits `src/nfkc_table.h` (**2808 entries**) and
  **refuses to write** if printable ASCII would be dropped.

Caught in the same hour: SPACE is representable only because sentencepiece maps
U+0020 → U+2581 before lookup. The raw lookup declared space out-of-vocab and
concatenated every query. Repaired, then pinned with `tests/space_probe.py`
(spaced vs unspaced twin ⇒ must differ; the broken build made them identical).

No-regression receipts:
- `tests/ab_text_parity.py` vs `build/ann_search_prev.exe` (pre-session build,
  spaces kept): **12/12 identical top-5**, with **A/control 12/12** run first
  (same exe twice over two identical corpora). The earlier 0/12 was the space bug,
  not ranking drift.
- `bench_warm_recall.py` byte-identical across both builds: R@1 1.0000, R@5 0.9200,
  10501/9647 scored.
- `make test` ALL PASS. Live `:8096` runs the rebuilt binary: entries 62913,
  routed=1, k=64, embed ok, tag 62913/62913.

## 2. Done — hourglass / frustum-pair shift algebra (DWGLS)

`I:/DWGLS-native-fs/tests/test_addr_orbit.c` T12–T15 (**35/35 ALL PASS**, +64 lines,
no new file, already wired into `GEO_FAST`):

| oracle | result |
|---|---|
| T12 | `(i<<1) % 20736` → only **10,368 images** (even modulus) — arithmetic shift is NOT a permutation |
| T13 | `20736 = 2^8 × 81`; rot8 on `u = i/81` is a bijection over the whole field, exact inverse, rot8^8 = identity |
| T14 | `d ^ 1` is an involution over `[0,12)`: **6 pairs, 0 fixed points** ⇒ crossing the shared centre = 1 XOR, composes to `d ^ (k&1)` |
| T14 | `d ^ 6` (face+3) stays in range for only **8/12** — `12 = 4×3` is not a power of two |
| T15 | k centre-crossings collapse to one XOR (no table, no walk) |

Already locked in the same file, not re-derived: generation ladder `2^a × 3^b`
(T4: 4×3, 16×9, 64×27, 256×81), `face = d>>1 / orient = d&1` (T6), latch pool
`144 × 72 = 10368` with reserved apex ids `{0, 10367}` (T8).

Geometry these oracles encode (owner drawing, read from the construction protocol):
regular hexagon side 2 with centre `G = 0`; 6 sectors from G = **3 hourglasses**;
opposite pair = **point reflection `v → −v`** (A↔D, B↔E, C↔F verified from the
coordinates); inner hexagon r = 1 = homothety **÷2** (same angles); mid ring
r = √3 = the ⌀2 semicircle intersections. `core/isometric_map.h:106` `IM_CROSS`
(0↔9, 1↔10, 2↔11, 3↔6, 4↔7, 5↔8) is the same self-inverse pairing.
`frustum_trit.h:8-12`: FACE_COUNT 6, COSET_COUNT 9, GEAR_MESH 54 = 9×6.

## 3. NOT done (open, ordered)

1. **Push `bd1ce04`** — `I:/tools/ann` main is 1 commit ahead of `origin/main`.
2. **Ann latch relayout** — `I:/tools/ann/src/ann_search.c:622` is still flat
   `int c = bucket % 144;`. Encode the cell as `(pair << 1) | end` so the centre
   crossing becomes `^1`. Gate: no recall change on the 300q oracle **and** bucket
   occupancy unchanged (the rotation probe in memory #7775 is the template for a
   no-op check: an orthogonal transform moved 0/20,000 labels).
3. **G2.5 receipt** — prove closing `+X` does not close `−X` (the gate that decides
   whether the 3-pair collapse is legal). `tools/pattern_probe.c:163`
   `FACE_ID(a,d) = a*6+d` + the 6-bit face mask is the starting point.
4. **3-pair id-space decision** — 72 vs 144 ids per anchor, and whether the
   **1-bit fingerprint** (which end of the hourglass was walked = tombstone) is kept.
   Pool math that forces the choice: wang6 + 3 gen = **5,440 anchors**; 6 faces and
   anchor = **32,640 > 10,368** pool ⇒ unaffordable; 1 bit and anchor =
   **5,440 ≤ 10,368** ⇒ affordable.

## 4. Context the next session needs

- K is **saturated at 62.9k rows**: max bucket 26,533 (K=8) → 10,725 (K=24) →
  5,303 (K=64) with recall flat; cv stays .35–.47 as K grows. Next lever is
  **placement/hierarchy, not a bigger table** (memory #7775).
- Zone-order walk (`ANN_WALK_ORDER=zone`, commit `515ea50`) is implemented but
  **inert at current scale** — at K=64 the walk uses 43% of budget so the cut never
  fires and order cannot matter. Revisit at ~2.3× n or a lower budget.
- The Python oracle for 300q @ 62.9k rows does **not** run on this box (~1.5 GB free
  RAM, 92 MB ndarray alloc fails). Use a machine with headroom or add a C-side oracle.
- Parked board cards: `task-tcce8f5f2` (latch 3-axis geometry, active, parked at
  "latch 3-axis geometry still parked"), `task-t5289298c` (frustum rescope: overlay
  L6 + Visit direction).
- Honesty note carried forward: the frustum rescope guidance is marked
  **"guidance only"** — `core/frustum_trit.h` and `core/frustum_gcfs.h` exist but no
  consumer ships; do not present them as live.
