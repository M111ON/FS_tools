# HANDOFF — LC_tilecube v4 (engineering ฝั่ง re-rank ชนะจริง · LC เป็น coarse quantizer ไม่ชนะ IVF · embedding จริง LC แพ้ชัด)

สถานะ ณ 8 พ.ย. 2026 · ภาษา: ไทย + ศัพท์เทคนิคอังกฤษ · ต่อจาก `HANDOFF_LC_tilecube_v3.md` (ของเดิมไม่ได้แก้ ไฟล์ v8 ทั้งหมดยังอยู่ครบ)

## เป้าหมายรอบนี้

ตามขั้นต่อไปใน v3 + ข้อจำกัดใหม่จากผู้ใช้: **RAM ต้องต่ำ** (เวอร์ชันแรกกิน >2.5 GB ใช้ไม่ได้) (1) ลดต้นทุน re-rank (90%+ ของเวลา) ด้วยการเรียงเวกเตอร์ตาม bucket (2) ลด RAM + เสริมความเร็ว (3) ลองใช้ LC เป็น coarse quantizer แทน k-means ของ IVF (4) สำรวจข้อมูลใน `I:\tools\ann\data` และยืนยันบน embedding จริง

## ไฟล์ใหม่ (วางใน `I:\tools\ann\experiments\LC_tilecube\` ไม่แก้ของเดิม)

| ไฟล์ | หน้าที่ | คำสั่ง |
|---|---|---|
| `lc_pipe2.c` + `lc_pipe_run2.py` | (v9) เทียบ layout สุ่ม vs ก๊อปเรียงตาม bucket (float32) — **กิน RAM มาก ไม่แนะนำ** | `python lc_pipe_run2.py N nq direct nlev Ss Ks Ls` · log `v9_layout.log` |
| `lc_pipe3.c` + `lc_pipe_run3.py` | (v10) **ตัวหลัก**: base เก็บ `uint8` + flag ต่อบิต `1`=contig `2`=early-abort `4`=prefetch `8`=prefix index `16`=bitset dedup | `gcc -O3 -march=native -ffast-math lc_pipe3.c -o lc_pipe3.exe -lm -lpsapi` · `python lc_pipe_run3.py 1000000 2000 direct 4 1.0 4,6 2,4 0,7,23,31` · log `v10_lowram.log` |
| `lc_pipe4.c` + `lc_pipe_run4.py` | (v11) LC เสนอ bucket → centroid 128 มิติ (uint8) จัดอันดับ → สแกน M อันดับแรก · M=0 = สแกนทั้งหมด (sanity) · `sc` 0 = `‖q−c‖²`, 1 = `‖q−c‖²−var` | `python lc_pipe_run4.py 1000000 2000 direct 4 1.0 6,12 4 0,4,8,12,16,24 0` · log `v11_cq.log` |
| `lc_embed_confirm.py` | (v12) รัน framework v8 บน embedding จริง (384 มิติ) เทียบ IVF/HNSW | `python lc_embed_confirm.py stats 1000 6,9` · log `v12_embed.log` |
| `pca_check.py`, `pca_check2.py`, `pca_check3.py` | สำรวจ PCA variance ของข้อมูลแต่ละชุด | `python pca_check.py` |

lay ที่แนะนำของ `lc_pipe3`: **23** (contig + EA + PF + bitset) · RAM ที่ใช้จริง peak ~**650–680 MB** ที่ SIFT1M, 4 level (เดิมคาด >2.5 GB)

## ผลที่ได้

### 1) การเรียงเวกเตอร์ตาม bucket (v9, float32)
re-rank เร็วขึ้น ~2× recall เท่าเดิมเป๊ะ (K6 L4: total 4852 → 2492 µs, ns/candidate 261 → 123) แต่ต้อง **ก๊อปเวกเตอร์ต่อ level (+512 MB ต่อ level)** → ใช้ไม่ได้ตามข้อจำกัด RAM

### 2) uint8 + contig + early-abort + bitset (v10, SIFT1M, 2000 query, single-thread)
SIFT เป็นจำนวนเต็ม 0–255 → เก็บ `uint8` ระยะทางเท่า float เป๊ะ (โค้ดหยุดพร้อมข้อความถ้าข้อมูลไม่ใช่ uint8-exact) recall ตรงกันทุกโหมด (ตรวจแล้ว)

| K / L | recB | cand% | uint8 สุ่ม (µs) | ชุดที่ดีที่สุด (µs) | ns/candidate |
|---|---|---|---|---|---|
| 4 / 2 | 0.605 | 0.69 | 1109–1349 | 265–310 | ~150 → ~33 |
| 4 / 4 | 0.775 | 1.29 | 2179–2451 | 518–623 | ~150 → ~32 |
| 6 / 2 | 0.681 | 0.94 | 1457–1864 | 357–417 | ~145 → ~31 |
| 6 / 4 | 0.829 | 1.70 | 2850–3220 | **681–859** | ~150 → ~30–42 |

(v3 เดิม K6 L4 ≈ 5127 µs → ~700 µs ≈ **~7×**, RAM ~650 MB)

ตัวช่วยเรียงตามผลจริง: uint8 (RAM 4× และอ่านน้อยลง) → contig (~3×) → early-abort (~25%) → bitset dedup 125 KB แทน `stamp[]` 4 MB (~15–20% ที่ L=4; ~17k candidates/query สุ่มอ่าน stamp ทุกตัวเป็นต้นทุนซ่อนอยู่) · prefetch ช่วยบ้าง ไม่คงที่ · **prefix index (flag 8) แทบไม่ช่วย** (อยู่ใน noise) ไม่ควรเก็บไว้ในค่าเริ่มต้น

### 3) LC เป็น coarse quantizer (v11, SIFT1M) — ผลเชิงลบ
LC เสนอ bucket (K×L คู่) แล้วจัดอันดับด้วย centroid 128 มิติ สแกนแค่ M อันดับแรก (recall ชุดทดสอบ B / candidates ที่สแกน)

| config | recB | cand% |
|---|---|---|
| K6 L4 M=ทั้งหมด (23.8 bucket) — เท่า pipe3 ✔ | 0.831 | 1.72 |
| K6 L4 M=16 | 0.778 | 1.11 |
| K6 L4 M=8 | 0.614 | 0.53 |
| K12 L4 M=24 (จาก 46.8) | 0.826 | 1.35 |
| K12 L4 M=ทั้งหมด | 0.902 | 2.68 |
| **IVF1024 nprobe=4 / 8 / 16** (อ้างอิง) | 0.744 / 0.873 / 0.950 | 0.445 / 0.878 / 1.72 |

centroid ranking ลด candidates ได้ ~22% ที่ recall เท่าเดิม (K12 M24 เทียบ K6 เต็ม) แต่ **ที่ budget เท่ากัน IVF นำเกือบทุกจุด** (เช่น ที่ ~0.88%: IVF 0.873 เทียบ LC ~0.74) เหตุผลที่น่าจะเป็น: bucket ของ LC มาจากการฉาย 6 มิติ การจัดอันดับด้วย centroid แก้ได้แค่ลำดับ ไม่ได้ทำให้ NN ตกใน bucket ที่ถูกมากขึ้น (k-means สร้าง cell จาก 128 มิติเต็ม) · `sc=1` (`d²−var`) แย่กว่า `sc=0` (ลองที่ 100k เท่านั้น) · ข้อดีที่เหลือ: scan ของเราเร็ว ~30–40 ns/candidate เทียบ faiss IVF ~50 ns

### 4) สำรวจข้อมูลใน `I:\tools\ann\data`

| ชุด | มิติ | จำนวน | PCA-6 เก็บ variance | participation ratio |
|---|---|---|---|---|
| SIFT learn | 128 | 100k (base 1M) | **53%** | **8** |
| BEIR CQADupStack (all-MiniLM-L6-v2, normalised) | 384 | 457,199 รวม 12 หมวด (16.7k–68k ต่อหมวด) | 18–20% | 80–90 |
| FiQA | 384 | 57,638 | 18% | 93 |
| SciFact | 384 | 5,183 | 23% | 65 |
| memcore V | 384 | 54,261 | 23% | 66 |

**ข้อสรุป: SIFT เป็นข้อมูลมิติต่ำกว่า embedding ทุกชุดที่มี** สมมติฐานเดิม ("SIFT แพ้เพราะการฉาย ลองข้อมูลมิติต่ำจะดีขึ้น") ทดสอบด้วยชุดในเครื่องนี้ไม่ได้ ชุดที่มีเป็นขั้วตรงข้าม

### 5) ยืนยันบน embedding จริง (v12, หมวด `stats`: 42,269 เวกเตอร์ 384 มิติ, 652 query, select 326 / test 326)
recall = NN จริงอยู่ใน bucket ที่เปิด (re-rank แบบ exact จะเจอแน่) · cell เป็น recall ชุดทดสอบ

| budget (candidates) | IVF nlist=256 | HNSW M16 (ndis) | LC 6D jtop L=1 | LC 6D jtop L=4 | LC 9D jtop L=4 |
|---|---|---|---|---|---|
| ≤1% | 0.721 (nprobe=2 @0.85%) | 0.902 (ef=8 @0.64%) / 0.957 (ef=16 @0.92%) | 0.340 | 0.402 | 0.580 |
| ≤2% | 0.819 (nprobe=4 @1.67%) | 0.985 (ef=32 @1.43%) | 0.488 | 0.448 | 0.687 |
| ≤5% | 0.905 (nprobe=8 @3.24%) / 0.945 (nprobe=16 @6.4%) | — | 0.699 | 0.647 | 0.724 |
| ≤10% | — | — | 0.785 | 0.782 | 0.758 |

IVF ได้ recall 0.72 ที่ ~0.85% ส่วน LC 6D ต้องใช้ ~5% (**~6× candidates**) และ 9D L=4 ~2% (~2.4×) แต่ต้องเปิด ~64 lookup ต่อ query → **LC แพ้ชัดบน embedding จริง** ตรงกับการคาดจาก PCA-6 ที่เก็บแค่ ~19% (`stats` var@6 = 0.188, @9 = 0.250)

## ข้อควรระวัง (ห้ามอ่านเกินจริง)

- เวลาทั้งหมด: รันครั้งเดียว ต่างกันรอบต่อรอบราว 10–20% (เห็นจาก lay เดียวกันต่างรอบ เช่น K6 L4 contig+EA 1085 vs 1050 µs, EA+PF 1097 vs 814 µs) ตัวเลข µs เป็นแนวโน้ม ไม่ใช่ค่าแม่นยำ
- v12 รันเฉพาะหมวด `stats` หมวดเดียว query ชุดทดสอบ 326 ตัว ความต่างของ recall ระหว่างชุดเลือก/ทดสอบถึง ±0.05–0.07 บางช่อง (เช่น L=4 ≤0.5%: 0.071>0.101) ใช้ดูแนวโน้มได้ ไม่ใช่ตัวเลขนิ่ง
- v12 เป็นเวอร์ชัน Python วัดเฉพาะ recall vs candidates **ยังไม่มีเวลา** และ HNSW ที่แสดงเป็นจำนวน distance computation (ไม่เทียบตรงกับ candidates ของ LC/IVF) · IVF/HNSW ไม่ได้จูน (nlist 64/256, M=16/efC=64)
- uint8 ใช้ได้เฉพาะข้อมูลจำนวนเต็ม 0–255 แบบ SIFT embedding 384 มิติต้องทำ int8/fp16 quantization ก่อน และ C ยังล็อก `D=128` (ยังไม่ได้ทำ D แบบพารามิเตอร์)
- v11 ลองที่ S=1.0 เท่านั้น, `sc=1` ลองเฉพาะ 100k · 9D ที่ SIFT1M ยังไม่ได้ลองใน pipeline C
- ผลของ prefix index/prefetch อยู่ใน noise ไม่ควรสรุปว่า "ไม่ช่วยเลย" แค่ไม่เห็นผลชัดในการทดสอบนี้

## ขั้นต่อไปที่แนะนำ

1. **ตัดสินใจทิศทางหลักก่อน**: LC 6 มิติเหมาะเฉพาะข้อมูลที่ PCA-6 เก็บ variance ได้สูง (แบบ SIFT PR~8) บน embedding PR~80 แพ้ชัด ถ้าเป้าหมายคือ embedding จริงในโปรเจกต์ ควรเลิกใช้ LC เป็นตัวหลักของ retrieval
2. **เอาส่วน engineering ไปเสริม IVF**: uint8/int8 + bucket-contiguous layout + early-abort + bitset dedup ใช้ได้กับ IVF ทั้งหมด (scan ~30–40 ns/candidate เทียบ faiss ~50) ต้องทำ D แบบพารามิเตอร์ + int8 quantization ก่อน
3. ถ้าจะลอง LC กับ embedding ต่อ: ลองฉายด้วยตัวที่เรียนรู้ (เช่น metric learning ให้ 6–9 มิติรักษา NN) แทน PCA ล้วน, หรือใช้ LC เป็นแค่ตัว pre-filter ชั้นแรก (เช่น routing ไป partition ใน memcore) ก่อน — ยังไม่ได้ทดสอบ
4. ค้างจากเดิม: ขยายกริด eps/S ใน 9D, ตรวจสมมติฐานฝั่งอิสระ, "3 tetra" และ parity สองเลน (v2) · รัน v12 ซ้ำกับหมวดอื่น (`unix`, `tex`) และวัดเวลาจริง

## จุดตัดสินใจที่เปิดอยู่

- จะหยุดลงทุนกับ LC สำหรับ embedding แล้วย้ายไปใช้ส่วน engineering กับ IVF หรือไม่ · จะทำ D แบบพารามิเตอร์ + int8 path ใน `lc_pipe3.c` ก่อนหรือไม่ · `lay=23` เป็นค่าเริ่มต้นของ pipeline (prefix index ไม่รวม) · ใช้ `jtop` K=4–6 เป็นกฎเลือกคู่ต่อไป (จาก v3) หรือไม่ · คง 6 มิติเป็นค่าเริ่มต้น (9D ดีกว่าบน embedding ที่ L=4 แต่ใช้ ~64 lookup)
