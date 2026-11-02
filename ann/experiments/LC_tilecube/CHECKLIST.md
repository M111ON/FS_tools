# LetterCube Nested — Checklist (ทำทีละประเด็น)

## A. Deterministic seed (climate change enabler)
- [x] A1. ทดสอบ lettercube_proof.js — junction ผ่านทุกกรณี; letter เหมือนเดิมเฉพาะ deterministic seed
- [x] A2. Port ตรรกะ LetterCube เป็น Python (lettercube_py.py) พร้อม deterministic seed — junction 2700/2700, deterministic → 0 diff, non-det → 1000 diff (ตรงกับ JS)
- [x] A3. ทดสอบ regrow บน Python port: 512+512, junction 2752/2752, old cubes identical 512/512 (deterministic)
- [x] A4. ทดสอบ capo shift: seed เปลี่ยน → junction ยังต่อ (540/540), rows เปลี่ยนตาม seed ไม่พัง

## B. Nested cube (onion peel) 2×2×2
- [x] B1. Nested 2-level: 8 sub-cube 2×2×2, junction 12/12 ทั้ง level0 และ level1
- [x] B2. 3-letter corner view วิ่งข้าม level: O(1) route ตรงกับ direct lookup 8/8 corners (lettercube_nested.py)
- [x] B3. Scaling (4×4×4 single level, 144/144) vs layering (2× 2×2×2, 12/12) แยกกลไกแล้ว

## C. Collision / signature uniqueness
- [x] C1. Collision rate: 5³/10³/20³ ทุก axis 0 signature collisions (distinct row_keys == distinct sigs)
- [x] C2. Finite signature space (13,440): n_rows=1000 → 31 ambiguous keys; n_rows=13440 → 3525 ambiguous. letter ใช้เป็น address (key) ได้เฉพาะ grid เล็ก (≤100 rows/axis); grid ใหญ่ต้องใช้ letter เป็น hint ไม่ใช่ key (collision_sim.py)

## D. Capo shift / rotation กับ nested
- [x] D1. Rotation/capo shift: junction ยังต่อ (12/12) ทั้ง single และ nested, capo shift preserves junctions: True
- [x] D2. Chain mode cross-level: junction 12/12 ทั้ง level, adjacent supers share row key: True

## E. Integration กับ LC_tilecube (ANN)
- [x] E1. Seed-derived projection = fixed seeded random matrix (climate-change-capable, ไม่ขึ้นกับ data) — ทดสอบแล้วใน run_exp.py (proj=rand): lowrank 0.93, SIFT 0.28, BEIR 0.05-0.10
- [x] E2. Recall ด้วย seed-derived projection: ต่ำกว่า PCA บน real data (rand < pca ทุกกรณี) — trade-off: climate-change-capable แต่ recall ต่ำ. ทางเลือก: hybrid (seeded random สำหรับ address stability + PCA-based re-rank) หรือ prefix-stable PCA (lock root centroid + record allocation) ตาม handoff v2 #7

---
shortcut: ผลจาก lettercube_proof.js มาจาก sim ที่ยังไม่ได้ทดสอบบนเบราว์เซอร์จริง; Python port จะเป็น independent verification.
"