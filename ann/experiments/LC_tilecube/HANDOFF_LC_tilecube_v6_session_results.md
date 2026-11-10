# HANDOFF — LC_tilecube v6 (ผลเซสชัน 2026-11-08) · B+C, Beam Addressing เพดาน, 3Dx8, agreement constraint, มุม >360

ต่อจาก `HANDOFF_LC_tilecube_v5_draft.md` (ข้อ 1-2 ของ "[ขั้นต่อไป (แทนรายการเก่า)]") และ `Experiment_Beamaddressing_handoff.md`
ไม่ได้แก้ไฟล์เดิมใดๆ — เพิ่มเฉพาะไฟล์ด้านล่าง

## ไฟล์ที่เพิ่ม (LC_tilecube)
| ไฟล์ | อะไร |
|---|---|
| `b_plus_c.py` / `b_plus_c.log` (UTF-16) / `b_plus_c_tex.log` | B+C: scope=subforum + SimHash k=4-6 |
| `b_plus_c2.py` / `b_plus_c2.log` | high-k (6-12) + boundary duplication |
| `beam_upper_bound.py` / `.log` | เพดาน recall ของ address บน projection d' มิติ (3D..24D) |
| `beam8_blocks.py` / `.log` | 3D x 8 cubes: union / vote>=2 ต่อลูก (tex อาจไม่ครบใน log) |
| `beam8_pair_angle.py` / `.log` | agreement constraint (>=k ลูก) + มุม 3 ระนาบ B=36/360/1440/cont |
| `EXPERIMENT_2026-11-08_B+C_simhash_dup.md` | บันทึกละเอียด B+C |
| `EXPERIMENT_2026-11-08_beam_upper_bound.md` | บันทึกละเอียด beam (มีส่วนแก้ไขข้อผิดพลาด) |

## Setup ร่วม
beir_embeddings (all-MiniLM, d=384): stats N=42,269 · unix N=47,382 · tex N=68,184; query <=1000 (seed 3); GT = flat L2 top-5
metric = recall@5 (true top-5 ที่อยู่ใน candidate) vs cand% (จุดไม่ซ้ำที่ต้อง rerank); baseline = flat IVF256 interpolate ที่ cand เท่ากัน
projection ทุกแบบ = PCA centered; รันครั้งเดียว ยังไม่วัดเวลา

## ผลสรุป
1. **B+C (SimHash ชั้น 7)** แพ้ IVF256 ที่ cand เท่ากันทุกจุด (stats k=4 ที่ cand ~6.5%: pca 0.454 vs IVF 0.928). code: pca > rand_ctr > rand_raw.
   k สูงขึ้น (6-12) ช่องว่างแคบลงแต่ยังติดลบ (cand ~8%: Δ −0.23 → −0.05). **boundary duplication (บิตเดียว) ไม่ช่วย** (recall ลด 0.01-0.04, memory x1.5-2)
2. **Beam 3D ชุดเดียว (ตามถ้อยคำ octants ใน handoff)**: เพดาน exact ใน 3D ต่ำกว่า IVF มาก (cand 8%: 0.66-0.71 vs 0.94-0.96); ต้อง ~9D ถึงเท่า IVF. ทิศอย่างเดียว >= ตำแหน่งเต็ม (ความยาว beam ไม่เพิ่มความคล้าย)
3. **3Dx8 (8 ลูกถือข้อมูลคนละชุด, 24 มิติ; ตามที่เจ้าของโปรเจกต์แก้ความเข้าใจ)**: union ของ top-c ต่อลูกยังแพ้ IVF (cand ~8%: 0.60 vs 0.94). เพดานผลรวมระยะ 8 ลูก (=24D exact) ได้ 0.964 ที่ cand 2% แต่ต้อง scan ทั้ง N
4. **agreement constraint (จุดต้องอยู่ใน top-c ของ >=k จาก 8 ลูก)**: ช่วยจริง ต่อ cand: stats c=16% k>=4 → cand 2.98% recall 0.828 (IVF 0.859); k>=5 → 0.58% / 0.615 (IVF 0.568). **แต่ต้นทุนการสร้าง list = 8c% ของ N (c=16% = 128%N)** จึงยังไม่ใช่ชัยชนะจริง
5. **มุมเกิน 360 ต่อ scale ไม่ช่วย**: B=360, 1440, continuous ให้ recall/cand เกือบเท่ากัน (tol 30°: 0.898/0.892/0.891); ตัวกำหนดคือ tolerance + จำนวนมิติ; B=36 หยาบเกินไป
6. **ข้อผิดพลาดที่แก้แล้ว:** บันทึกแรกเขียนว่า vote>=2 แย่กว่า union — ผิด; ที่ cand เท่ากัน vote>=2 ดีกว่า (stats cand ~4%: 0.70 vs 0.41) แต่ยังแพ้ IVF

## ข้อสรุปเชิงทิศทาง
- LC/beam เป็น quantizer บน projection ต่ำมิติ → จำกัดด้วย variance ที่ projection เก็บ (3D ~11%, 24D ~42%) ไม่ใช่รูปแบบ address
- จุดเดียวที่ยังมีช่อง: agreement/product-key ข้ามหลายลูก (ได้ recall/cand ใกล้ IVF) ถ้าทำ index ให้ต้นทุน lookup ~ cand ได้
- มุม/Foldable Hash 2^n ไม่เพิ่ม recall; ยังมีประโยชน์เป็น identity/scope address (ยังไม่ได้ประเมิน)
- สอดคล้อง v5: ปิดทิศ LC-as-quantizer เดี่ยวๆ สำหรับ embedding; geometry ใช้เป็น address/scope/frame seek

## ข้อจำกัด / สิ่งที่ยังไม่ได้ทำ
- "pair letter" จำลองเป็น agreement ข้ามลูก ไม่ได้ใช้ letter/junction จริงของ lettercube_py (เป็น constraint เชิงโครงสร้าง/seed)
- ยังไม่ได้ทดสอบ product-key join จริง (นับ lookup/latency), ยังไม่ได้ลอง projection ที่ไม่ใช่ PCA (metric-learned/random), ยังไม่ได้ลองข้อมูล SIFT/memcore
- Test 2 (Foldable Hash 2^n) และ Test 3 (multi-probe level 0/1/2) ของ beam handoff ยังไม่ได้ทำ (เพดานข้อ 2 จำกัดอยู่แล้ว)
- tex ใน `beam8_blocks.log` อาจไม่ครบ; `b_plus_c.log` เป็น UTF-16

## ขั้นต่อไป (เรียงตามราคา)
1. product-key join ข้ามลูก: key = tuple cell ของ 2-3 ลูกที่ agree, เปิด combo ด้วย joint probability (แนว v8 jtop/jmass) แต่ขยายเป็น k=3-5 ลูกจาก 8; วัด recall vs (cand + lookups) เทียบ IVF256
2. ลอง projection อื่นต่อลูก (ตัวเดียวกับ multiframe/metric-learned) เพื่อดูว่า union/agreement ดีขึ้นไหม
3. อ่าน `HANDOFF-2026-10-14d.md` (DWGLS anchor+frustum+CAPO) ว่าแก้ boundary แล้วหรือยัง (ข้อ 3 เดิม)
4. ร่าง spec node = 4D subgraph; engineering v4 ที่ค้าง (ข้อ 4-5 เดิม)

## จุดตัดสินใจที่เปิดอยู่
- ลงทุนทำ product-key join (ข้อ 1) หรือปิดทิศ LC/beam สำหรับ similarity แล้วใช้ geometry เป็น identity/scope เท่านั้น
- seed ชั้น 7 (Matryoshka, v5): จากการทดสอบ B+C นี้ SimHash/PCA-sign ไม่พอเป็นตัว route
- เจ้าของโปรเจกต์มี approach อีกทางที่กำลังคิดอยู่ (ยังไม่ได้ระบุ) — ทดสอบด้วยชุดข้อมูล/metric เดียวกันนี้เพื่อเทียบ IVF ตรงๆ ได้
