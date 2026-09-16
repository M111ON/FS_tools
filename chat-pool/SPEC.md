# chat-pool SPEC — cross-app group-chat pool + tower geometry (DWGLS)

Locked 2026-09-16. Code: `pool.py`, `tower.py`, `chat_tower.h` (stdlib/C only).

## 1. B-slice — pool (durability)

`log.jsonl`, append-only, one JSON/line (UTF-8). v:2 format:

```json
{"v":2,"t":1789539028.27,"app":"hermes","kind":"code","text":"...",
 "prev":"<prev-hash|GENESIS>","rkey":19450,"enc":730,"hash":"<sha256>"}
```

- `hash = sha256(prev+app+kind+text)` — INTEGRITY chain (0/0 measured).
- `rkey` = exact port of `core/rdh_capture.h` (RDH_CAPTURE_144) — ADDRESS
  hint only. Never integrity: single-RDH collides 1800/6072, flip-blind 9.04%.
- `enc = rkey % 1440` — free frame_seek view.
- Append = single `os.write` O_APPEND + fsync (cross-app atomic). Rules: append
  only, never edit by hand, never run `demo` outside tests (it wipes).

Flush (hourly, task `chat-pool-flush`): `flush_emit()` = sha256-changed →
`outbox/<ts>.json`, else SKIP. `push_outbox()` reuses
`hermes_pool_cli.py embed --session chat-pool` (venv python), archives `.pushed`,
empty = no-op. `.flush_state.json` = `{sha, offset}`.

## 2. A-slice — tower (geometry)

Tower = 1 tesseract = 1152 chunk-slots (**32×36**), 18 towers = FULL 20736.
Chunk = 144B. `towers/t<t0>/{header.json, cells.hex}`, fps recomputed on load.

Header: `{beacon: 111*(pair+1), t0, count, pair, view: A/B, tess}`.
`tower_idx = t0//1152`, `tess = idx%18`, view A = 32×36, B = 36×32.
Time-seed (LOCKED): `pair = (idx + PIS60[(cycle*7)%60]%9)%9`, cycle = idx//18 —
logical t only (replayable), shift = pair bijection (uniformity exact).

- **Ticker (dual-hand, LOCKED):** fast `enc(t) = (t*37)%1440`, bijection
  1440/1440, invertible. Slow hand Pisano-60 table `phase=Fib(t%60)%10`;
  zero-beacons at phase 0,15,30,45 (96/rotation, every 15 ticks, even spread).
  Pisano is NOT bijection/invertible → landmarks only, never addressing.
  Repdigit beacons `frame_enc(3k)=111k` (t=3→111 … 27→999); beacon k ↔ pair k.
  1440 = 24×60 = π(9)×π(10).
- **Sibling (B-twinview, LOCKED):** 9 pairs (i,i+9), transpose link.
  Proven 20736/20736, fixed 0. Transpose cycles: 2 fixed anchors + 10×115.
  Rule: single-writer canonical view, readers lock-free.
- **Fingerprint (LOCKED fp_vec8):** 144B → 9×16B sums, center v4,
  8 line-sums. 141568/141644 distinct (coll 0.054%), 16B. mod9 kept as
  1B prefilter only. kNN probe = random (1.1x) → vec8 is checksum, not feature.
- **p4m block (LOCKED):** tower 1152 + 4 header-sentinels = 1156 = 34×34 =
  4×(17×17). Sentinels = block-center 2×2 D4-orbit
  `{(16,16),(16,18),(18,16),(18,18)}` carrying
  (beacon,t0,count,pair) — self-describing, no sidecar. Tiling class p4m
  (all 6 generators PASS ×3 periods). Payload grouped Q0..Q3 ×288.
- `20736 = 18×32×36 = 4×72²`; factor table {4,8,9}→[16,32,36,64,72,81];
  9-cell full grid kept (zero-sum needs center); C4 proven, full-D4 impossible
  (axial-flip stabilizer mismatch) → C4+chirality.

## 3. Proven numbers (facts.jsonl: 6072 lines → 211497 chunks)

| test | result |
|---|---|
| ticker bijection/invert/roundtrip | 1440/1440, 0/211497 |
| mod9 distribution | uniform ~23.5k/bucket |
| chunk dedup (exact) | 33.03% |
| tower shootout 32 vs 36 | F-coll 0 both → dual axes, use both |
| sibling A-shift / B-twinview | 20736/20736, fixed 0, inv 0 |
| fp shootout vec8 vs mod9 | 0.054% vs shared → vec8 locked |
| tower pack | 184 towers, roundtrip mm 0 |
| E2E pool→towers / live demo | PASS (5 apps, beacon 111) |
| push | PASS (searchable, no-op, cleaned) |
| C parity (chat_tower.h vs tower.py) | 3/3 vectors byte-exact |
| RDH port parity vs rdh_capture.h | 5/5 (caught 1 axis swap) |

## 4. Limits (honest)

- `×8`=D4, `60=4×15`, missing-8/12345679 = base-10 coincidences, not mechanism.
- RDH sees low nibbles only — address, never link.
- Transpose duality = layout duality, not literal dimensions.
- vec8 ≠ semantic signal. Semantics live in encoder (pool.db/fastembed);
  geometry is MAP. Next F1: tower-routed recall vs global.

## 5. Ops

Hourly: `schtasks chat-pool-flush` → `python pool.py` (flush+push or SKIP).
Manual: `pool.py demo` (wipes!), `tower.py` self-check, `latest --session chat-pool`.
Layout: `pool.py tower.py chat_tower.h SPEC.md | log.jsonl towers/ outbox/`.
GPU: `gpu_fp_vec8.cu` (sm_61, 1 thread/chunk, XOR sink, bilingual comments) —
parity 0/11520 vs CPU, 0.234ms vs 165.9ms (709×) on 10-tower batch.
`gpu_transpose.cu` (A 32×36 → B 36×32 reorder, uint4 moves, 1 thread/slot) —
parity 0/1,658,880 bytes vs CPU, 0.138ms on 10 towers; mapping == sibling().
Vulkan: `vulkan_fp.comp` (1 invoc/chunk, bit-identical uint wrap) on reused
harness `I:/DWGLS/bench/vulkan_bw_pull.c` (CHUNK 144, sums 32B, wg=(n+255)/256,
push 36, C ref + full dump) — parity 0/92160, upload 642MB/s (same wall),
device-read 0.48GB/s (their cooperative-1024B pattern hits 29.2 — headroom
via WG-cooperation restructure, future detail).
`gpu_chain.cu` (fp+transpose, one transfer pair, phased events) — parity 0/0;
10 towers: CPU 1.29ms (fp 1.09+tr 0.20), GPU resident 0.53ms (2.4×),
GPU full-path 5.9–12ms (WDDM transfers lose). Rule: compute proven on GPU;
end-to-end must ride the fast bridge (DRamTile/Gear2), never naive copies.
