# EXPERIMENT 2026-11-08 — product-key join ข้ามลูก (v6 ขั้นต่อไป ข้อ 1)

ไฟล์: `beam8_pkjoin.py` / `beam8_pkjoin.log` (UTF-8; smoke 200 query อยู่ `pkjoin_smoke.log`)
รัน: `python -u beam8_pkjoin.py 500 stats unix tex` (ใช้ ~5-9 นาทีต่อชุด, ยังไม่วัดเวลา latency จริง)

## Setup
ตาม v6: PCA 24D แบ่ง 8 ลูก x 3D (interleaved), GT = flat L2 top-5, nq=500 (seed 3), baseline = IVF256 interpolate
- ต่อลูก: k-means K cell ใน 3D (K = 8/16/32)
- table = subset ของ m ลูก (m=2,3) → key = tuple cell id, inverted list key→points
- query: จัดอันดับ key ทั้งหมด (K^m) ด้วย joint score = sum ||q - centroid||^2 ของลูกใน subset (isotropic-Gaussian joint log-prob), เปิด top-T key (T = 0.2%-16% ของ K^m)
- รวม nsub table (1,2,4,8,16 เลือกสุ่ม seed คงที่): union (v>=1) / agreement ใน >=2 หรือ >=3 table
- cost = cand ไม่ซ้ำ + จำนวน key probe (T*nsub) + 8K cell-distance; IVF256 cost = cand + 256

## ผล (best recall@5 ภายใน cost budget; IVF256 ในวงเล็บ)
| ชุด | cost<=4% | cost<=8% | cost<=16% |
|---|---|---|---|
| stats | 0.799 (0.873) | 0.901 (0.937) | 0.953 (0.970) |
| unix | 0.858 (0.898) | 0.932 (0.957) | 0.970 (0.986) |
| tex | 0.868 (0.902) | 0.940 (0.960) | 0.975 (0.988) |

- ที่ cost <=2%: ต่ำกว่า IVF 0.05-0.15 ทั้งสามชุด (stats 0.621 vs 0.757, unix 0.700 vs 0.789, tex 0.706 vs 0.821)
- เสมอ/ชนะ IVF (±0.01) เฉพาะที่ cost >= ~10% ของ N (เช่น tex m=2 K=32 8 table v>=3: cost 9.55% rec 0.975 vs 0.971) — ไม่คุ้มเชิงใช้งาน
- config ที่ใกล้ IVF สุดซ้ำทุกชุด: m=2 K=32 nsub=8 v>=3 (stats cost 3.7% 0.799 vs 0.864 ; unix 3.65% 0.858 vs 0.887 ; tex 3.64% 0.868 vs 0.893)
- agreement (v>=3) ชนะ union ในแทบทุกจุดบน Pareto; m=3 K=32 แย่สุด (cell เล็กเกิน, key probe มาก)
- ต้นทุน probe ไม่ใช่คอขวด: d(cand) กับ d(cost) ใกล้กันที่ cost ต่ำ; ช่องว่างหลักมาจากคุณภาพการเลือก cand

## ข้อสรุป
1. product-key join ลดต้นทุนสร้าง list ได้จริง (ไม่ต้อง 8c% N) แต่ **recall/cost ยังแพ้ IVF256 ทุกชุดในช่วง cost <10%** → ยังไม่ใช่ชัยชนะ
2. เทียบ v6 ข้อ 4 (agreement บน exact top-c: stats cand 2.98% rec 0.828 vs IVF 0.859, ต่าง -0.03): ที่ cand ~3% ตัวนี้ได้ ~0.78 (ต่าง ~-0.09) → quantize เป็น cell 3D ทิ้ง recall ราว 0.05 เทียบ exact ต่อลูก; ราคาของการทำให้ lookup ถูก
3. ตามทิศใน v6: จำกัดด้วยข้อมูลที่ 3D x 8 projection เก็บ (~42% variance) ไม่ใช่โครงสร้าง index
4. ที่ยังไม่ได้ลอง: m=2 K=48/64 (K ใหญ่ที่ m=2 ดีขึ้นตามแนวโน้ม K=8→16→32), v>=4, nsub>16, projection อื่นที่ไม่ใช่ PCA, วัดเวลาจริง (C/numba), SIFT

## ข้อจำกัด
รันครั้งเดียว/seed เดียว, nq=500, subset ของ table สุ่ม seed คงที่ (ไม่ optimize), นับ distance 3D = 1 หน่วยเท่า 384D (ลำเอียงเข้าข้าง IVF เล็กน้อย), ties ที่ threshold อาจเปิด key เกิน T เล็กน้อย
