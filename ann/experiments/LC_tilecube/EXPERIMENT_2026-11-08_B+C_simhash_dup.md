# EXPERIMENT 2026-11-08 — B+C (scope=subforum + code ชั้น 7) และ boundary duplication

ต่อจาก `HANDOFF_LC_tilecube_v5_draft.md` ส่วน "[ขั้นต่อไป (แทนรายการเก่า)]" ข้อ 1 และ 2
ไม่ได้แก้ไฟล์เดิมใดๆ — เพิ่มเฉพาะไฟล์ด้านล่าง

## ไฟล์ที่เพิ่ม (โฟลเดอร์ LC_tilecube)
- `b_plus_c.py` + `b_plus_c.log` (stats, unix; log เป็น UTF-16 เพราะ PowerShell `*>`) + `b_plus_c_tex.log` (tex, UTF-8)
- `b_plus_c2.py` + `b_plus_c2.log` (stats, unix, tex; UTF-8)
- รัน: `python -u b_plus_c.py 1000 stats unix tex` · `python -u b_plus_c2.py 1000 stats unix tex`

## Setup
- scope (B) = subforum: ค้นเฉพาะ corpus ของ subforum นั้น (exact partition), ชั้น 8 = brute-force ใน bucket ที่อ่าน
- ข้อมูล: beir_embeddings (all-MiniLM, d=384): stats N=42,269 · unix N=47,382 · tex N=68,184; query ≤1000 (stats 652)
- ground truth = flat L2 top-5; metric = recall@5 (สัดส่วน true top-5 ที่อยู่ใน bucket ที่อ่าน) vs candidate%
- ชั้น 7 (C) = code k bit → 2^k bucket; multi-probe เรียง bucket ตามผลรวม |margin| ของบิตที่ต้องพลิก
- code 3 แบบ: `rand_raw` (random plane ไม่ center), `rand_ctr` (random plane, center ด้วย mean), `pca` (sign ของ top-k PC, center)
- baseline: flat IVF256 (และ IVF1024 ใน run ที่ 2) interpolate recall ที่ cand% เท่ากัน; ใน run ที่ 1 เทียบ IVF nlist=2^k ที่ nprobe เท่ากันด้วย

## ผลข้อ 1: B+C (k=4,5,6; b_plus_c.py)
แพ้ IVF256 ที่ cand เท่ากันทุกจุด ทั้ง 3 ชุดข้อมูล (stats ตัวอย่าง, k=4):
| cand | rand_ctr | pca | IVF256 |
|---|---|---|---|
| ~6.5% | 0.264 | 0.454 | 0.928 |
| ~25% | 0.646 | 0.883 | 0.985 |
| ~50% | 0.871 | 0.972 | 0.998 |
- ลำดับ code: pca > rand_ctr > rand_raw; rand_raw bucket เอียง (entropy 3.84/4, 5.62/6 บิต)
- IVF nlist=2^k ที่ nprobe เท่ากันก็ชนะ pca SimHash (เช่น stats k=4 nprobe=2: 0.680 vs 0.799)
- tex/unix ให้ pattern เดียวกัน (tex k=5 nprobe=4: pca 0.652 vs IVF256@same-cand 0.955)

## ผลข้อ 2 + high-k (b_plus_c2.py)
k=6..12 (leaf เฉลี่ย 660 → 41 → 10 vec บน stats ที่ k=6/10/12), Δ = recall(pca) − IVF256@same-cand ที่ cand ~2% / ~8%:
| ชุด | k=6 | k=8 | k=10 | k=12 |
|---|---|---|---|---|
| stats | −0.45 / −0.23 | −0.34 / −0.13 | −0.24 / −0.09 | −0.17 / −0.05 |
| unix | −0.44 / −0.21 | −0.34 / −0.12 | −0.24 / −0.08 | −0.18 / −0.05 |
| tex | −0.46 / −0.23 | −0.36 / −0.15 | −0.26 / −0.09 | −0.18 / −0.06 |
- k สูงขึ้นช่วยให้ช่องว่างแคบลง แต่ยังติดลบทุกจุด (ที่ cand ~32% เหลือ −0.00…−0.01 ซึ่งแทบเท่ากับสแกนเกือบทั้ง scope)
- `rand_ctr` k=10 แย่กว่า pca มาก (stats Δ −0.54 / −0.39 ที่ ~2% / ~8%)
- boundary duplication (ซ้ำ f=0.5 หรือ 1.0 ของ vector ที่ margin ต่ำสุดไป bucket ข้างเคียงทิศเดียว): **ไม่ช่วย** ที่ cand เท่ากัน recall ต่ำลง ~0.01–0.04 (stats pca10: dup0.0 0.848 → dup0.5 0.821 → dup1.0 0.801 ที่ ~8%) และเสีย memory ×1.5–2.0 ของ id

## ข้อสรุป
1. code ชั้น 7 แบบ SimHash/PCA-sign ไม่พอเป็นตัว route สำหรับ embedding นี้: IVF (learned partition) ชนะทุกจุดที่ cand เท่ากัน
2. ทิศ "scope แล้วค่อย SimHash" ใช้ไม่ได้เป็น similarity search; scope (B) ยังมีประโยชน์ในแง่ขอบเขต/identity เท่านั้น
3. duplication ที่ขอบแบบง่าย (ทิศเดียวตามบิตอ่อนสุด) ไม่คุ้ม memory
4. สอดคล้องข้อสรุปเดิมของ v5: geometry ใช้เป็น address/scope/frame seek ไม่ใช่ quantizer สำหรับ embedding

## ข้อจำกัด (อ่านก่อนอ้างอิง)
- รันครั้งเดียว seed เดียว (plane random seed = 11+k); query สุ่ม seed 3
- metric เป็น coverage ของ top-5 ใน candidate (เทียบเท่า recall@5 หลัง exact rerank); ไม่ได้วัดเวลา/latency
- dup rule ที่ทดสอบง่ายมาก (บิตเดียว); ยังไม่ได้ทดสอบ anchor+frustum+CAPO (HANDOFF-2026-10-14d) ซึ่งเป็นข้อ 3 ของรายการและอาจแก้ boundary ได้ต่างจากนี้
- N แค่ 42–68K ต่อ subforum; ภาพ "1M แบ่ง 16K ทางเข้า ~60 vec/leaf" ยังไม่ได้ทดสอบที่ N นั้น (k=10 บน stats ≈ 41 vec/leaf ใกล้เคียงสัดส่วนนั้น)
- code ที่ทดสอบไม่ใช่ learned code; learned code (k-means/IVF) ก็คือ IVF เอง จึงไม่ได้แยกทดสอบ

## ที่ยังไม่ได้ทำจากรายการเดิม
- ข้อ 3: อ่าน `HANDOFF-2026-10-14d.md` (DWGLS: anchor+frustum+CAPO)
- ข้อ 4: ร่าง spec node = 4D subgraph
- ข้อ 5: engineering v4 (D แบบพารามิเตอร์ + int8 path ใน lc_pipe3.c)
- จุดตัดสินใจเปิดอยู่: seed ชั้น 7 มาจาก B หรือ C; ปิดทิศ LC-as-quantizer สำหรับ embedding หรือไม่ (ผลนี้หนุนให้ปิด)
