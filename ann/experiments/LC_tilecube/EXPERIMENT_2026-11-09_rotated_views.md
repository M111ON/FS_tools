# EXPERIMENT 2026-11-09 — "DNA = Rotated View" (PCA 24D เดียวกัน 3 มุมมอง) vs แบบหั่น 72D

ไฟล์: `beam72_pkjoin.py` (v2 ใหม่) · `beam72v2.log` · v1 เก็บเป็น `beam72_pkjoin_slice_v1.py/.log` · smoke: `beam72v2_smoke.log`
รัน: `python -u beam72_pkjoin.py 1000 stats unix tex` — query seed 7 (v1 ใช้ 3), จำนวนจริง: stats 652 (มีแค่นี้), unix 1000/1072, tex 1000/2906

## Config ที่ล็อก (ไม่เลือกจากผล)
table = 3 views, K=8 k-means cells/view (key 8^3), 16 tables สุ่ม (seed 11), agreement v>=3; เส้นโค้ง = เปิด top-T key (T=3..82) ; v=2/4 พิมพ์เป็น sensitivity เท่านั้น
cost = cand ไม่ซ้ำ + probes (T*16) + views_used*K ; IVF256 cost = cand + 256

## โหมด
| โหมด | คืออะไร |
|---|---|
| base3d | 8 ลูก 3D (PCA 24D) — control |
| rot3d | 8 ลูก x หมุน 0/120/240° รอบ (1,1,1) (Rodrigues) = 24 view, seed k-means ต่างกันต่อ view — "Rotated View" ที่ขอ |
| plane2d | 8 ลูก x XY/XZ/YZ (2D cells) = 24 view |
| slice72 | 3 แกน x 8 ลูก บน PC 0-71 ไม่ซ้ำ (แบบหั่น) — ตัวเทียบ |
| reseed3d | 8 ลูก x 3 seed ไม่หมุน (control: ความหลากหลายจาก seed อย่างเดียว) |
| rot3d_same | หมุนเหมือน rot3d แต่ seed เดียวกันต่อลูก (control: หมุนอย่างเดียว) |

## ผล recall@5 ที่ cost budget (interpolate บนเส้นโค้ง; config ล็อก)
| ชุด | โหมด | 2% | 4% | 8% | 16% |
|---|---|---|---|---|---|
| stats (nq=652) | base3d | 0.632 | 0.772 | 0.881 | 0.951 |
| | rot3d (= reseed3d) | 0.690 | 0.839 | 0.918 | 0.967 |
| | plane2d | 0.591 | 0.775 | 0.877 | 0.947 |
| | slice72 | 0.624 | 0.767 | 0.894 | 0.949 |
| | rot3d_same | 0.568 | 0.738 | 0.871 | 0.950 |
| | IVF256 | 0.756 | 0.871 | 0.934 | 0.969 |
| unix (nq=1000) | base3d | 0.688 | 0.824 | 0.917 | 0.969 |
| | rot3d (= reseed3d) | 0.740 | 0.865 | 0.942 | 0.982 |
| | plane2d | 0.704 | 0.858 | 0.928 | 0.975 |
| | slice72 | 0.696 | 0.828 | 0.933 | 0.970 |
| | rot3d_same | 0.637 | 0.799 | 0.908 | 0.967 |
| | IVF256 | 0.806 | 0.909 | 0.961 | 0.986 |
| tex (nq=1000) | base3d | 0.683 | 0.819 | 0.914 | 0.968 |
| | rot3d (= reseed3d) | 0.755 | 0.873 | 0.946 | 0.983 |
| | plane2d | 0.706 | 0.861 | 0.929 | 0.975 |
| | slice72 | 0.712 | 0.826 | 0.933 | 0.969 |
| | rot3d_same | 0.645 | 0.802 | 0.908 | 0.965 |
| | IVF256 | 0.801 | 0.899 | 0.955 | 0.984 |
(se ของ recall แต่ละจุด ≈ 0.006-0.011 — ดู log)

## ข้อสรุป
1. **rot3d ชนะ slice72 ทุกชุดที่ cost 2-4% (+0.03..+0.07) และ 8% (+0.01..+0.02)** ทั้งที่ใช้แค่ PCA 24D (variance 42%) แทน 72D (66%) → ตอบคำถาม "ดีกว่าแบบหั่นข้อมูลไหม": ใช่ ใน config นี้ (slice72 ได้เปรียบเรื่อง config เพราะ config ล็อกมาจากรอบหั่น)
2. **แต่ไม่ใช่เพราะ "การหมุน":** rot3d กับ reseed3d ให้ตัวเลขเท่ากันทุกหลัก (partition เหมือนกัน เพราะ k-means เป็น rotation-equivariant และการหมุน 120° รอบ (1,1,1) = สลับแกน) และ rot3d_same (หมุนแต่ seed เดียว) แย่กว่า base3d → ผลมาจาก **ความหลากหลายของ partition** (21 partition ต่างกันบนข้อมูลชุดเดียว เทียบ 8) ที่หน่วยความจำ/จำนวน table เท่าเดิม (16)
3. plane2d (XY/XZ/YZ 2D) ≈ base3d..slice72: ไม่ได้ข้อมูลใหม่ ได้ความหลากหลาย partition บ้างแต่ cell 2D หยาบกว่า
4. เทียบ IVF256: rot3d ยังต่ำกว่าที่ cost 2-8% (2%: −0.05..−0.07, 4%: −0.03..−0.04, 8%: −0.01..−0.02), เสมอที่ ~16% (−0.004..−0.001) → **ยังไม่ชนะ IVF** แม้ดีขึ้นจาก v1/24D เดิมชัดเจน
5. ผล v1 (`any` slice: unix 8% 0.956, tex 8% 0.955) สูงเกินจริง ~0.02 เมื่อล็อก config (slice72 locked: 0.933 ทั้งคู่) → ยืนยัน selection bias ที่เตือนไว้

## ข้อจำกัด
- stats มี query แค่ 652 (ขอ ≥1000 ไม่ได้), tex ใช้ 1000 จาก 2906; ผลมาจาก config เดียว (K=8,m=3,nsub=16,v>=3) ซึ่งเลือกจากรอบ slice
- "rotation" ที่ทดสอบเป็น symmetry ของลูกบาศก์ — กับ quantizer ที่ axis-aligned (grid/octant+angle แบบ LC จริง) หรือการหมุนมุมอื่น (เช่น 45°, random) ผลอาจต่าง ยังไม่ได้ทดสอบ (ใช้ k-means cells ไม่ใช่ cell ของ lettercube_py)
- ยังไม่วัดเวลา/หน่วยความจำจริง; นับ 3D distance = 1 หน่วยเท่า 384D; รันครั้งเดียว

## ขั้นต่อไป (เรียงตามราคา)
1. ทดสอบว่าความหลากหลาย partition ขยายได้อีกไหม: nsub 32/64 + seed ใหม่ ต่อ cube (reseed3d scaling), v>=3-5 — น่าจะเป็นตัวแปรหลักตอนนี้
2. ทดสอบ rotation จริงกับ quantizer ที่ axis-aligned (octant/angle grid) และมุมที่ไม่ใช่ symmetry ของลูกบาศก์
3. วัดเวลา/หน่วยความจำของ 16 table เทียบ IVF256
