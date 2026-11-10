# EXPERIMENT 2026-11-08 — Beam Addressing: upper-bound probe (ยังไม่ใช่ Test 1-3 เต็มของ handoff)

อ้างอิง: `Experiment_Beamaddressing_handoff.md` (ไม่ได้แก้ไฟล์นั้น)
สคริปต์: `beam_upper_bound.py` · log: `beam_upper_bound.log` · รัน: `python -u beam_upper_bound.py 1000 stats unix tex`

## ตรรกะของ probe
address/cell ใดๆ ที่สร้างบน projection d' มิติ (3D beam = XY/XZ/YZ, 8 pyramids, 24 vertices, angular cell, magnitude shell)
ได้ recall ไม่เกิน "อ่าน C จุดที่ใกล้ที่สุดใน projection นั้นแบบ exact" ที่ cand เท่ากัน
จึงวัดเพดานนี้ก่อน (ถูก ไม่ต้องเขียน pyramid/fold) — projection = PCA centered
- `euclid` = ตำแหน่งปลาย beam (ทิศ + ความยาว); `dir` = ทิศอย่างเดียว (ความยาวถูกทิ้ง)
- ข้อมูล/GT/metric เดียวกับ B+C: beir_embeddings stats/unix/tex, recall@5 vs cand%

## ผล (recall@5, stats; unix/tex ใกล้เคียง)
| cand | 0.5% | 2% | 8% | 32% |
|---|---|---|---|---|
| IVF256 | 0.540 | 0.806 | 0.939 | 0.990 |
| 3D euclid | 0.111 | 0.314 | 0.662 | 0.957 |
| 3D dir | 0.065 | 0.261 | 0.648 | 0.940 |
| 6D euclid | 0.329 | 0.602 | 0.882 | 0.993 |
| 9D euclid | 0.513 | 0.777 | 0.947 | 0.998 |
| 9D dir | 0.568 | 0.811 | 0.957 | 0.998 |
| 12D dir | 0.686 | 0.884 | 0.976 | 0.998 |
- variance ที่ PCA 3D/6D/9D/12D ครอบคลุม: ~11% / 18% / 25% / 29% (stats)
- 3D (ขอบเขตของ beam addressing ตาม handoff) ต่ำกว่า IVF256 มากทุกจุด: ที่ cand 8% ได้ ≤0.66 เทียบ 0.94 (unix 0.69, tex 0.71 เทียบ ~0.96)
- ต้องไปถึง ~9D ขึ้นไปจึงเท่า IVF256 และนั่นคือ exact scan ใน 9D (เพดาน) ไม่ใช่ address จริงที่จะได้ recall เท่านั้น
- ทิศอย่างเดียว (`dir`) ≥ ตำแหน่งเต็ม (`euclid`) เมื่อ ≥6D → ความยาว beam ไม่ได้เพิ่มความคล้าย (สวนกับสมมติฐาน "Beam Length = Similarity")

## ข้อสรุป
1. Test 1 (3D beam/8 pyramids/24 vertices vs PCA 6D) จะไม่ชนะ IVF: เพดานของ 3D ต่ำกว่า IVF เองอยู่แล้ว และ 6D ก็ต่ำกว่า
2. คอขวดคือ projection ลง 3D เสีย variance ~89% — ไม่ใช่รูปแบบ address ที่ใช้ (สอดคล้องข้อควรระวัง "Projection Loss" ใน handoff)
3. ความไม่ทับกันของ beam = unique identity ไม่ใช่ similarity (จุดต่างกันย่อมมีทิศ/ความยาวต่างกัน ไม่ว่าจะใกล้หรือไกล)

## ที่ยังไม่ได้ทดสอบ / ข้อจำกัด
- projection อื่นที่ไม่ใช่ PCA (metric-learned, หลาย projection union แบบ multiframe, random) — อาจต่างจากเพดานนี้
- Test 2 (Foldable Hash 2^n) และ Test 3 (multi-probe level 0/1/2) ยังไม่ได้ทำ: เป็นเรื่องของโครง address ซึ่งถูกจำกัดด้วยเพดานข้างบนอยู่แล้ว
- Foldable Hash ในบทบาท identity/scope address (ไม่ใช่ similarity) ยังไม่ได้ประเมิน
- รัน 1 ครั้ง, seed query = 3

---
## แก้ความเข้าใจ (จากเจ้าของโปรเจกต์): 3D x 8 ไม่ใช่ 3D ชุดเดียว
8 pyramids/cubes แชร์แค่จุดศูนย์กลาง แต่ละลูกถือข้อมูลคนละชุด → รวม 24 มิติ (8 บล็อก x 3D) ส่วน probe ด้านบนจำลองแค่ 3D ชุดเดียว (ตามถ้อยคำ "octants" ใน handoff)
สคริปต์: `beam8_blocks.py` · log: `beam8_blocks.log` (tex รันต่อหลังบันทึกนี้ อาจยังไม่ครบ)

### วิธี
บล็อก = PCA 24 มิติแรก แบ่ง 8 ลูกละ 3 มิติ (layout consecutive = (0,1,2),(3,4,5)..; interleaved = (b,b+8,b+16))
แต่ละลูกให้ candidate = c จุดใกล้สุดใน 3D ของลูกนั้นแบบ exact (เพดานของ cell scheme ใดๆ บนลูกนั้น); รวมแบบ union หรือ vote>=2 แล้ว rerank exact
cand% = จุดไม่ซ้ำที่ต้อง rerank; เทียบ flat IVF256 ที่ cand เท่ากัน

### ผล (recall@5; union)
| c ต่อลูก | stats cand→recall | IVF256@same | unix cand→recall | IVF256@same |
|---|---|---|---|---|
| 0.5% | 3.9% → 0.41 | 0.88 | 3.9% → 0.46 | 0.92 |
| 1% | 7.7% → 0.60 | 0.94 | 7.6% → 0.66 | 0.96 |
| 2% | 14.8% → 0.78 | 0.97 | 14.7% → 0.84 | 0.99 |
| 4% | 27.6% → 0.92 | 0.99 | 27.3% → 0.95 | 1.00 |
- tex ให้ pattern เดียวกัน (c=1%: 7.6% → 0.67 เทียบ IVF 0.96)
- interleaved ≈ consecutive (ต่างไม่เกิน ~0.02) → วิธีแบ่งมิติให้ลูกต่างๆ ไม่ใช่ตัวแปรหลัก
- vote>=2 (ต้องอยู่ใน list ≥2 ลูก) แย่กว่าอีก: cand ลดมากแต่ recall ลดเร็วกว่า (stats c=2%: cand 1.1% recall 0.44 เทียบ IVF256 ที่ cand 1.1% ≈ 0.72)
- สำหรับอ้างอิง: การจัดอันดับด้วยผลรวมระยะทั้ง 8 ลูก (=24D exact, ดู beam_upper_bound.log) ได้ 0.964 ที่ cand 2% แต่ต้องคำนวณทั้ง N (scan) ไม่ใช่ address

### ข้อสรุปหลังแก้
1. 3Dx8 ดีกว่า 3D ชุดเดียวมาก (ได้ 24D) แต่เมื่อสร้าง candidate ด้วย address ต่อลูกแล้ว union ยังแพ้ IVF256 ที่ cand เท่ากันทุกจุด (stats ที่ cand ~8%: 0.60 เทียบ 0.94) แม้ในกรณี exact ต่อลูก
2. เพดาน 24D รวม (0.964@2%) เหนือ IVF256 ได้ แต่ต้องรวมระยะทางทุกลูกกับทุกจุด — จะเป็น address ที่ใช้งานได้ต้องหาวิธีสร้าง candidate จากผลรวมโดยไม่ scan ซึ่ง union/vote ต่อลูกยังทำไม่ได้
3. ช่องว่างระหว่าง union (0.60) กับ sum (≈0.96) มาจากการที่ neighbor แท้ไม่ได้อยู่ใกล้ในทุกลูกพร้อมกัน (แต่ละลูกเห็นแค่ 3 มิติ)

### ข้อจำกัด
- บล็อกมาจาก PCA (ใน handoff ไม่ได้ระบุว่าแต่ละลูกควรเอามิติใด); ยังไม่ได้ลอง metric-learned/random partition ของ 384 มิติ
- ไม่ได้ทดสอบ multi-index แบบจับคู่ cell (เช่น product-cell join) ที่อาจดึง candidate จากผลรวมได้ดีกว่า union
- รันครั้งเดียว, seed query = 3

---
## แก้ไขบันทึกด้านบน + ทดสอบ "pair/agreement constraint" และ "angular > 360"
สคริปต์: `beam8_pair_angle.py` · log: `beam8_pair_angle.log` (stats, unix)

### แก้ข้อผิดพลาดในส่วน "3D x 8" ก่อนหน้า
บันทึกว่า "vote>=2 แย่กว่า union" **ผิด**: เทียบที่ cand เท่ากัน vote>=2 ดีกว่า union มาก
(stats: vote>=2 cand 3.97% → recall 0.70; union cand 3.91% → recall 0.41) แต่ยังแพ้ IVF256 (0.885 ที่ cand 3.97%)

### Part P — ข้อบังคับความสอดคล้อง (item ต้องอยู่ใน top-c ของอย่างน้อย k จาก 8 ลูก)
สมมติฐานผู้ใช้: pair/ข้อบังคับระหว่างลูกช่วยให้หาข้อมูลง่ายขึ้น. ผล (cand% = จุดไม่ซ้ำที่ rerank):
| ตั้งค่า | stats cand → recall (IVF256@same) | unix cand → recall (IVF256@same) |
|---|---|---|
| c=1% k>=2 | 0.30% → 0.211 (0.516) | 0.32% → 0.279 (0.556) |
| c=16% k>=5 | 0.58% → 0.615 (0.568) | 0.77% → 0.748 (0.680) |
| c=16% k>=4 | 2.98% → 0.828 (0.859) | 3.25% → 0.892 (0.903) |
| c=16% k>=3 | 12.35% → 0.944 (0.963) | 12.40% → 0.970 (0.982) |
- ยิ่งบังคับให้ตรงกันหลายลูก (k สูง) และ list ต่อลูกกว้าง (c สูง) ผลยิ่งเข้าใกล้/เท่า IVF256 ที่ cand เท่ากัน (ที่ cand ~0.5-0.8% เหนือกว่า +0.05..+0.07)
- **ข้อควรระวังเรื่องต้นทุน (สำคัญ):** cand นับเฉพาะจุดที่ผ่านด่านแล้วต้อง rerank แต่การสร้าง list ต่อลูกแล้วนับ vote ต้อง touch รวม 8c% ของ N
  (c=16% → 128% ของ N, มากกว่า scan ทั้งหมด). แถว "ชนะ IVF" ข้างบนจึงยังไม่ใช่ชัยชนะจริงเรื่องต้นทุน
- จะได้ต้นทุน ~ cand ต้องทำ agreement เป็น product key ของ cell ข้ามลูก (join) แทนการรวม list ต่อลูก — นี่คือสิ่งที่ LC v8 ทำที่ 2-3 ด้าน (ต้องมี lookup เป็น 15^m door combos และ k>=4-5 ต้องใช้ key 12-15D ซึ่ง combo ระเบิด)
- ข้อสรุปเชิงทิศทาง: constraint แบบ "ต้องตรงกันหลายลูก" ช่วยจริงในแง่ precision ต่อ cand; คำถามที่ยังเปิดคือสร้าง index ให้ได้ต้นทุน lookup ต่ำหรือไม่

### Part A — angular resolution เกิน 360 ต่อ scale
address ต่อลูก = มุม 3 ระนาบ (XY, XZ, YZ) quantize เป็น B ช่อง; อ่านจุดที่ทั้ง 3 มุมอยู่ในหน้าต่าง ±tol องศา:
| tol | B=360 union (stats) | B=1440 | continuous (B=∞) |
|---|---|---|---|
| 30° | 29.3% / 0.898 | 28.8% / 0.892 | 28.6% / 0.891 |
| 15° | 9.2% / 0.527 | 8.8% / 0.507 | 8.7% / 0.503 |
- B=1440 และ continuous แทบเหมือน B=360 (unix เช่นกัน) → **ขยายมุมเกิน 360 ที่ scale เดียวกันไม่ช่วย**: recall/cand ถูกกำหนดด้วย tolerance (ขนาดบริเวณที่อ่าน) และจำนวนมิติ ไม่ใช่ความละเอียดของ address
- B=36 (10°/ช่อง) หยาบเกิน: หน้าต่างปัดขึ้นเกินจริง (tol 15° ได้ cand 21% แทน ~9%) แปลว่าความละเอียดมีผลแค่ตอนหยาบกว่า ~1° ต่อช่อง
- หน้าต่างมุม 3 ระนาบแย่กว่า top-c แบบระยะทางเล็กน้อยที่ cand เท่ากัน (stats vote>=2 cand 4.3% → 0.62 เทียบ 0.69 ของ top-c)
- ความละเอียดมุมเพิ่มประโยชน์ได้อย่างเดียวคือใช้เป็น address/identity ที่ละเอียดและ fold ได้; ไม่เพิ่ม recall ของ similarity

### ข้อจำกัด
- "pair letter" ถูกจำลองเป็นข้อบังคับ "agree ข้ามลูก" (ไม่ได้ใช้ letter/junction จริงของ lettercube_py ซึ่งเป็น constraint เชิงโครงสร้าง/seed ไม่ได้มาจากข้อมูล)
- ยังไม่ได้ทดสอบ product-key join จริง (ต้นทุน lookup), ไม่ได้นับ latency
- block มาจาก PCA 24 มิติแรก (interleaved), รัน 1 ครั้ง; Part A ใช้ 300 query
