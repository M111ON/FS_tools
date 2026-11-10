# HANDOFF — LC_tilecube v3 (เลือกคู่ประตูด้วยความน่าจะเป็นร่วม + select/test split + 9 มิติ + selector C + pipeline เต็มบน SIFT1M)

สถานะ ณ 8 พ.ย. 2026 · ภาษา: ไทย + ศัพท์เทคนิคอังกฤษ · ต่อจาก `HANDOFF_LC_tilecube_v2.md` (ของเดิมไม่ได้แก้)

## เป้าหมายรอบนี้

ตามขั้นต่อไปใน v2: (1) ลด lookup ของกุญแจผลคูณ 6 มิติ โดยเปิดเฉพาะคู่ประตูที่ความน่าจะเป็นร่วม P₁×P₂ สูง แทนเปิดผลคูณเต็ม (2) แบ่ง query เป็นชุดเลือก S กับชุดทดสอบ (3) ขยายเป็น 9 มิติ (3 label) (4) เขียน selector ใน C วัดเวลาจริง (5) ลอง SIFT1M เต็ม เทียบ IVF/HNSW

## ไฟล์ใหม่ (วางใน `I:\tools\ann\experiments\LC_tilecube\` ไม่แก้ของเดิม)

- `lc_label_bucket8.py` — ตัวทดลองหลัก: probability LUT ต่อฝั่ง (canonical สมมาตร 48, G=6) คูณกันเป็น J แล้วเลือกคู่ด้วย `jthr` (J ≥ eps), `jtop` (K คู่ที่ J สูงสุด), `jmass` (สะสม mass ถึง τ) เทียบ baseline `full`, `sign2`, `dist3`, `prodcano` (v7) · แบ่ง query ครึ่ง/ครึ่ง (เลือก S/eps และ sigma บนครึ่งเลือก รายงานบนครึ่งทดสอบ) · เพดาน lookup บนชุดเลือก · รองรับ 6D และ 9D · รัน `python lc_label_bucket8.py sift 100000 1000 6,9 direct,rot` ล็อก `v8_sift.log` (utf-8)
- `lc_select_bench.c` — วัดเวลา selector (sort 15 ระยะ / top-3 ระยะ / LUT + joint top-K) พร้อมตรวจ overlap กับ top-K เต็ม · คอมไพล์ `gcc -O2 -march=native lc_select_bench.c -o lc_select_bench.exe -lm` · รัน `lc_select_bench.exe 0.5 6`
- `lc_pipe.c` — pipeline เต็ม single-thread ใน C: project 128→6 → 2 label BCC → LUT top-4 ต่อฝั่ง → joint top-K → hashed bucket (sorted keys + binary search) → dedup → re-rank 128 มิติ top-1 · คอมไพล์ `gcc -O3 -march=native -ffast-math lc_pipe.c -o lc_pipe.exe -lm`
- `lc_pipe_run.py` — ตัวคุม: ฝึกด้วย sigma จริงจาก `sift_learn` (rms ของ offset ใน lattice ระหว่าง learn query กับ NN จริงในฐานข้อมูล, สเกล 1/S), ดัมพ์ LUT ให้ C (`pipe_dump/params_S*.bin`), รัน C, baseline faiss IVF-Flat nlist=1024 และ HNSW M=16 efC=64 (single-thread ตอน search) · `python lc_pipe_run.py check` (C เทียบ Python ที่ 100k) · `python lc_pipe_run.py run 1000000 2000 direct 4 0.45,0.6,0.7,0.85,1.0,1.25 4,6 1,2,4 hnsw` ล็อก `v8_pipe.log`
- ล็อก: `v8_sift.log`, `v8_pipe.log` (รอบหลังสุดมาจาก run ข้างบน)

## ผลที่ได้

**เลือกคู่ด้วยความน่าจะเป็นร่วม (SIFT 100k, 1000 query, select=500/test=500, L=1, เพดาน ≤8 lookup):** `jtop` K=4–6 ลด lookup จาก 19–26 (v7) เหลือ 3–6 โดย recall ใกล้เคียงหรือสูงกว่า prodcano เล็กน้อย (direct6 ≤5% work: jtop 0.748 ที่ 6.0 lookup เทียบ prodcano 0.692 ที่ 5.3, ≤2%: 0.602 เทียบ 0.576) ที่ work เท่ากัน jtop ชนะ `full` (225 คู่) ได้ เพราะ full ต้องใช้ S หยาบกว่าเพื่อให้ work ไม่เกิน (L=4 ≤0.1% rot9: jtop 0.324 เทียบ full 0.154) ขยายกริด eps ถึง 0.3 แล้ว `jthr` ใกล้เคียง `jtop` แต่ไม่ชนะชัด (direct6 L=1 ≤5%: 0.706 ที่ 4.9 lookup) และถ้าไม่มีเพดาน jthr/jmass เลือก config ที่เปิดเป็นหลักสิบถึงหลายร้อยคู่ จึงแนะนำ `jtop` K=4–6 เป็นค่าหลัก

**Select/test split:** ช่องว่างระหว่าง recall ชุดเลือกกับชุดทดสอบส่วนใหญ่ ±0.03 (บางช่องทดสอบสูงกว่า) ไม่พบหลักฐานว่าตัวเลข v6/v7 เอนเอียงมาก (แต่ 500 query ต่อชุด ความต่าง 2–3 จุดยังเป็นสัญญาณรบกวน)

**9 มิติ:** ที่ L=1 ไม่ดีกว่า 6 มิติ ที่ L=4 งบต่ำ (≤0.1–0.25%) ดีขึ้น (jtop ราว 0.30–0.45 ที่ 8–24 lookup เทียบ 6D ราว 0.23–0.31) แต่ selector ช้าลง ~1.5× และ prodcano ล่มใต้เพดาน lookup (~0.01) ยังไม่คุ้มชัดเจน

**Selector ใน C (ns ต่อ query ทุกฝั่งรวม, LUT จาก sigma=0.5 สมมติ):** 6D K=6: sort15 575 / near3 408 / LUT+jointtop 202 · 6D K=4: 551 / 380 / 123 · 9D K=6: 840 / 627 / 305 การตัดเหลือ top-4 ต่อฝั่งให้ชุดคู่ตรงกับ joint top-K เต็ม 96.8% ที่ K=6 (mass ratio 0.997) และ 100% ที่ K=4

**C เทียบ Python (N=100k, direct6 L=1 K=6):** ตรงกันภายใน ~1 จุด (S=1.5: recall 0.615 เทียบ 0.621, candidates 1.44% เทียบ 1.50%) ยืนยันว่า LUT ชุดเดียวกันให้ผลเหมือนกัน

**SIFT1M, single-thread, 2000 query (เลือก config บนครึ่ง A รายงานครึ่ง B), recall ชุดทดสอบ / candidates ที่สแกน / µs ต่อ query:**

| งบ candidates | LC | IVF1024 ใกล้เคียง | HNSW ใกล้เคียง |
|---|---|---|---|
| ≤0.5% | 0.533 / 0.47% / 1491 | nprobe=4: 0.744 / 0.445% / 229 | ef=16: 0.804 / 0.045% / 125 |
| ≤1% | 0.717 / 0.87% / 2697 | nprobe=8: 0.873 / 0.878% / 485 | ef=32: 0.891 / 0.069% / 173 |
| ≤2% | 0.829 / 1.70% / 5127 | nprobe=16: 0.950 / 1.72% / 861 | ef=64: 0.950 / 0.112% / 275 |

selector ใช้เพียง 2–7 µs, lookup ราว 20–330 µs ส่วน re-rank คือ 90%+ ของเวลา (~300 ns ต่อ candidate จากการอ่านสุ่ม เทียบ ~50 ns ของ IVF) และ recall ต่อ candidate ต่ำกว่า IVF มาก (0.53 เทียบ 0.74 ที่ ~0.45%) การฉาย 128→6 มิติคือคอขวดตามที่ v2 สงสัย

## ข้อควรระวัง (ห้ามอ่านเกินจริง)

- เป็น SIFT เท่านั้น ยังไม่ลอง embedding จริง
- IVF ลองแค่ nlist=1024 และ HNSW แค่ M=16/efC=64 ไม่ได้ปรับจูน ค่า candidates ของ HNSW คือจำนวนการคำนวณระยะ ไม่เทียบตรงกับ LC/IVF
- โค้ด C ของ LC ยังไม่ปรับจูน (layout เวกเตอร์ยังเป็นแบบสุ่ม ไม่ได้เรียงตาม bucket, ไม่มี SIMD เฉพาะทาง) ความเร็วของ selector ในหน่วย ns เป็นการเทียบสัมพัทธ์ (โค้ดมีกิ่งมาก)
- eps ของ jthr ชนขอบกริดใน 9D บางช่องหลังขยาย ยังไม่ได้ยืนยันว่ากริดครอบคลุมพอ
- ความน่าจะเป็นร่วมสมมติฐานฝั่งอิสระต่อกัน ไม่ได้ทดสอบความสัมพันธ์ของ PC ที่หมุนแล้ว
- ที่ SIFT1M ใช้ 4 level และ S ถึงแค่ 1.25 (S=1.5 ที่ L=4 ให้ recall 0.944 แต่ใช้ 6.1% candidates / 13.7 ms จึงตัดออกจากกริดสุดท้าย) · รัน 2000 query ครั้งเดียว ไม่มีการทำซ้ำเพื่อดูความแปรปรวนของเวลา

## ขั้นต่อไปที่แนะนำ

1. ทดสอบกับข้อมูลที่มิติภายในต่ำจริง (embedding ที่ PCA 6 มิติเก็บความแปรปรวนได้มาก) เพราะ SIFT แพ้ที่การฉายไม่ใช่ selector
2. ลองใช้โครงสร้างนี้เป็น coarse quantizer แทน k-means ของ IVF (แก้ทั้ง recall ต่อ candidate และ layout)
3. เก็บเวกเตอร์เรียงต่อเนื่องตาม bucket (ก๊อปต่อ level) เพื่อลดต้นทุน re-rank ต่อ candidate แล้ววัดใหม่
4. ขยายกริด eps/S ใน 9D, ทดสอบ jtop แบบ K ปรับตามความมั่นใจ (ผสม jmass) และตรวจสมมติฐานฝั่งอิสระ
5. ค้างจาก v2: ตรวจ "3 tetra" และไอเดีย parity สองเลน (ยังไม่ได้ทำ)

## จุดตัดสินใจที่เปิดอยู่

ใช้ `jtop` K=4–6 เป็นกฎเลือกคู่ (ผลปัจจุบันชี้ทางนี้) · จะหยุดลงทุนกับ SIFT แล้วย้ายไปข้อมูลมิติต่ำ หรือลองแปลง LC เป็น coarse quantizer ก่อน · จะคง 6 มิติเป็นค่าเริ่มต้น (9 มิติคุ้มเฉพาะ L=4 งบต่ำและ selector ช้ากว่า) หรือไม่ · quad frustum (ประตูแกน) คงไว้ในชุด probe หรือไม่ (v2) ยังไม่ได้ทดสอบซ้ำในแนวนี้
