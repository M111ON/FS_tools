# LetterCube / LC_tilecube — Session Report 2026-11-03

## 1. ภาพรวม

Session นี้ทดลอง LC_tilecube (LetterCube LSH) กับ data หลายแบบ (synthetic 3D, SIFT 128D, BEIR 384D) วัด recall@1, scan%, latency แยกตาม dim และประเภท data. แล้วขยายไปทดสอบ deterministic seed (climate change), nested cube (onion peel), collision, hybrid IVF+LC, scale/level/latency.

## 2. ผล Baseline (run_exp.py)

LC_tilecube = 3D projection (rand/pca) + BCC bucket (15 doors). วัดบน n=8K-12K.

| data | dim | proj | best recall @scan≤2% (L=1, K=4) | probe ms/q |
|---|---|---|---|---|
| syn_gauss3d | 3 | rand | 0.959 | 0.003 |
| syn_mix3d | 3 | rand | 0.991 | 0.007 |
| syn_lowrank128 | 128 | pca | 0.997 | 0.002 |
| syn_lowrank128 | 128 | rand | 0.930 | 0.003 |
| syn_random128 | 128 | rand/pca | 0.02-0.04 | 0.002 |
| siftsmall128 | 128 | pca | 0.190 (L1), 0.320 (L4) | 0.005 |
| beir_*_384 (3 domains) | 384 | pca | 0.11-0.17 (L1), 0.14-0.19 (L4) | 0.003-0.006 |

**ข้อสรุป:** 3D projection ต่ำเกินไปสำหรับ high-dim (PCA3 เก็บ variance ~16.7%). คอขวดคือ projection ไม่ใช่ bucket shape.

## 3. Staged Tests (run_staged.py)

PCA non-overlap subset ต่อ level:
- lowrank128: 0.710 → 0.937 (ช่วย structured data)
- sift 128: 0.320 → 0.110 (แย่ลง)
- beir 384: 0.190 → 0.107 (แย่ลง)

→ ไม่ใช่ universal fix. ช่วยเฉพาะ data ที่ variance กระจุก.

## 4. Deterministic Seed / Climate Change (lettercube_py.py)

Python port ของ LetterCube ยืนยันผล lettercube_proof.js:
- Junction: 2700/2700 ทุกลำดับ (order-independent)
- Deterministic seed → 0 diff signature; non-det → 1000 diff
- Regrow 512+512: junction 2752/2752, old cubes identical 512/512
- Capo shift: seed เปลี่ยน → junction ยังต่อ (540/540)

**→ climate change capable ได้ถ้า implement deterministic seed (seed+row key, seed+pos)**

## 5. Nested Cube / Onion Peel (lettercube_nested.py)

- 2-level 2×2×2: junction 12/12 ทั้ง level0/level1
- 3-letter corner view: O(1) route ตรงกับ direct lookup 8/8 corners
- Scaling (4×4×4, 144/144) ≠ Layering (2×2×2×2, 12/12) — กลไกคนละตัว
- Capo shift/rotation: junction ยังต่อ 12/12 (single + nested)
- Chain mode cross-level: junction 12/12, adjacent supers share row key

## 6. Collision (collision_sim.py)

- Row signatures: 0 collisions ทุก grid size (5³/10³/20³)
- Finite space (13,440): letter ใช้เป็น address ได้เฉพาะ grid เล็ก (≤100 rows/axis)
- n_rows=1000 → 31 ambiguous keys; n_rows=13440 → 3525 ambiguous
- → grid ใหญ่ต้องใช้ letter เป็น hint ไม่ใช่ key

## 7. Hybrid M5 (run_M5.py)

LC + IVF: dedup union + exact rerank. n=4K, nq=100.

| combo | ivf_np | LC_L | ivf_rec | lc_rec | union_rec | scan% | LC_unique |
|---|---|---|---|---|---|---|---|
| M5-A | 1 | 1 | 0.590 | 0.060 | 0.620 | 1.95 | 3 |
| M5-B | 2 | 1 | 0.770 | 0.060 | 0.780 | 3.50 | 1 |
| M5-C | 2 | 4 | 0.770 | 0.230 | 0.820 | 3.91 | 5 |
| M5-D | 4 | 1 | 0.890 | 0.060 | 0.900 | 6.83 | 1 |

Baseline: IVF nprobe=4 alone = 0.890 @ 6.69%

**→ LC unique contribution จำกัด (1-5 queries). LC L4 ช่วยลด IVF nprobe (4→2) ได้ แลก recall 7%.**

## 8. Recall Boost (run_recall_boost.py)

| method | SIFT recall | SIFT scan% | BEIR recall | BEIR scan% |
|---|---|---|---|---|
| M0 PCA3D L1 K4 | 0.060 | 0.22 | 0.010 | 0.14 |
| M1 multi-proj union | 0.070 | 0.33 | 0.030 | 0.28 |
| M3 PCA3D L4 K4 | 0.230 | 0.71 | 0.070 | 0.50 |
| M4 adaptive K | 0.040 | 0.12 | 0.010 | 0.06 |
| M2 IVF nprobe=4 | 0.890 | 6.69 | 0.870 | 6.87 |
| M2 IVF nprobe=8 | 0.970 | 13.46 | 0.940 | 13.37 |
| M2 IVF nprobe=16 | 1.000 | 26.52 | 0.980 | 26.29 |

**→ IVF dominates recall. LC family ได้ recall ต่ำกว่ามากที่ scan เทียบกันได้.**

## 9. Scale + Level + Latency (run_scale_level_latency.py)

### Scale (L=4, nprobe=2)

| N | ivf_rec | lc_rec | union_rec | scan% | LC_unique | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| 4K | 0.770 | 0.230 | 0.820 | 3.91 | 5 | 0.53 | 0.72 |
| 10K | 0.820 | 0.270 | 0.840 | 3.00 | 2 | 0.65 | 0.84 |
| 100K | 0.610 | 0.350 | 0.710 | 1.50 | 10 | 2.18 | 3.31 |
| 1M | 0.440 | 0.300 | 0.560 | 1.07 | 6 | 17.42 | 25.42 |

### Level sweep (N=10K, nprobe=2)

| L | ivf_rec | lc_rec | union_rec | scan% | LC_unique | overlap | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.820 | 0.090 | 0.830 | 2.51 | 1 | 4.3 | 0.31 | 0.44 |
| 2 | 0.820 | 0.140 | 0.830 | 2.65 | 1 | 6.8 | 0.38 | 0.48 |
| 4 | 0.820 | 0.270 | 0.840 | 3.00 | 2 | 14.2 | 0.60 | 0.75 |
| 8 | 0.820 | 0.390 | 0.860 | 3.78 | 4 | 24.5 | 1.01 | 1.32 |

**ข้อสรุป scale/level:**
- LC_unique ไม่ scale กับ N (คงที่ 2-10) แต่ relative value เพิ่มเมื่อ IVF ตก
- LC_unique scale กับ L (ทวีคูณ)
- Overlap เพิ่มตามทั้ง N และ L
- Pareto sweet spot: L=4 (0.84 recall, 3.00% scan, 0.60ms)
- Latency ที่ 1M: p50=17ms, p95=25ms

## 10. บทบาทที่เหมาะสมของ LetterCube

ตามที่เสนอไว้: หยุดบังคับให้ Geometry ทำหน้าที่แทน Vector Similarity.

| โมดูล | หน้าที่ |
|---|---|
| LetterCube Grid | Spatial Address |
| Snake | Incremental Growth |
| Boundary | Data Isolation |
| Jump | Domain Routing |
| IVF | Semantic Candidate Search |
| Exact Rerank | จัดอันดับความใกล้เคียงจริง |

LC เป็น complementary candidate source: ช่วยลด IVF nprobe โดยเติม candidates ที่ IVF coarse cells พลาด. คุ้มค่าเมื่อ scan budget จำกัด (mobile/edge) ที่ต้องการ recall ปานกลาง (0.8) ที่ scan ต่ำ (<4%).

## 11. ไฟล์ที่สร้าง

- `lc_label_bucket.py` — base LSH (Label + Bucket)
- `lc_label_bucket2.py` — PCA non-overlap variant
- `run_exp.py`, `run_staged.py`, `run_recall_boost.py`, `run_M5.py`, `run_scale_level_latency.py` — experiment scripts
- `lettercube_py.py` — Python port of LetterCube
- `lettercube_nested.py` — nested cube tests
- `lettercube_rotate.py` — capo shift/rotation tests
- `lettercube_chain.py` — chain mode cross-level
- `collision_sim.py` — finite signature space test
- `CHECKLIST.md` — checklist with all items completed
- `REPORT.md`, `exp_results.csv` — baseline results

## 12. Shortcuts / ข้อจำกัด

- ผลทั้งหมดวัดบน numpy vectorized, n=4K-1M, nq=50-200
- BEIR 384D ทดสอบเฉพาะ android domain
- lettercube_proof.js ยังไม่ได้ทดสอบบนเบราว์เซอร์จริง
- IVF nprobe=2 คงที่ — ถ้าเพิ่ม nprobe ตาม N ผลจะต่าง
- Build time ไม่รวมใน latency
- LC_unique อาจเปลี่ยนที่ scale ใหญ่ขึ้น (data distribution dependent)
"