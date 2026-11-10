# HANDOFF — LC_tilecube v6 (draft) · Beam Addressing (3letter + 8 Pyramids) + Foldable Hash (2^n)

## เป้าหมายรอบนี้
จากรูปทรง Octahedron ที่มี rotation 3แกนตัดเส้นเหล่านั้นออกมา เรียงเป็นเส้นตรงแล้ว plot เป็น 2D graph จะได้กราฟที่มีข้อมูลแสดงเป็นเส้นทะแยงมุม หลังจากนั้นโปรเจ็ก xy+xz, xy+yz จะได้3ด้านของ cube ที่เป็นเส้นทะแยงมุมเช่นกัน ซึ่งถ้าค่าโตแบบเป็นเส้นตรงเราจะได้ลากจุดปลายทางมาเชื่อมกันแล้วเกิด tetrahedron แต่ในความเป็นจริง slope เหล่านั้นมันจะzigzag เหมือนฟันปลา เราทำบบนี้แล้วเอามาวางให้อยู่ในปรูป 8cube(2³) มันจะmap เข้ากับ truncated Octahedron ได้พอดี หลังจากนั้นใช้คอนเซปต์ **"Beam Addressing"** (การลากเส้น Beam จากจุดศูนย์กลาง) ที่เชื่อมโยงกับ Angular Address จากจุด center แล้วลองทดสอบว่าถ้าใช้หลักการนี้ในการเก็บข้อมูลมันให้ผลลัพธ์เป็น **Similarity Search** ที่ดีกว่า/เร็วกว่า LC_tilecube v4/v5 หรือไม่

## ไอเดีย (จากเจ้าของโปรเจกต์)
- **Beam Addressing:** ทุกค่าข้อมูลในกราฟ ถูกลากเส้น (Beam) จากจุด (0,0,0) ไปหาข้อมูลตอนที่ Project เป็น Cube
    - **Beam ไม่มีวันทับกัน 100%** (เว้นแต่ค่าตำแหน่งจะเหมือนกันเป๊ะ) แต่ **สามารถมีความยาว (Magnitude) เท่ากันได้** (ข้อมูลที่อยู่บนวงกลม/sphere เดียวกัน)
- **3letter Mapping (XY, XZ, YZ):** การ Project ข้อมูลลงระนาบทั้ง 3 นี้ คือการมองเห็น 3 ด้านของ Cube พอดี
- **8 Pyramids (Shared Center):** 
    - เอาโครงสร้าง Beam นี้ไป Rotate/Mirror 7 ชุดใน `2x2x2` Cube (O_h symmetry)
    - จะได้ **8 พีระมิด (Octants)** ที่มีจุดยอด (Apex) ร่วมกันที่ (0,0,0) 
    - สิ่งนี้คือ DNA เดียวกับโครงสร้าง Cube 8 ลูก (ที่เกิดจาก 3-Octahedron Compound)
- **24 Vertices:** 8 พีระมิดที่แชร์ Center นี้ จะมีจุดยอดรอบนอก/กึ่งกลางหน้า รวมกันเป็น **24 Vertices** ซึ่งใช้เป็น Fine Hash (Anchor) ได้
- **Sawtooth Slope (ฟันปลา):** เมื่อคลี่ Cone/Pyramid ออกมา จะได้ Slope แบบ Discrete (Ladder Shift) ทำให้ใช้เทียบ Pattern Matching (Phase Alignment) เพื่อหา Similarity ได้
- **Foldable Hash (Angular Address):** ใช้ `2^n` (Bit Shift) ในการทำ Ladder Shift (4 < 16 < 64 < 256) เพื่อ Fold/Unfold Address ได้เร็วระดับ 1 Cycle (Lossless 100% และไม่ทำให้ Space แคบลงถ้าใช้ Multi-Probe)

## สิ่งที่มีอยู่แล้วในระบบ (อ้างอิง)
- `lettercube_rhombi.h` (LCR_COMP) — ยืนยันว่า 3letter (แกน X, Y, Z) ทำงานได้
- `lc_label_bucket*.py` (v3-v8) — มีระบบ Ladder Shift และ LUT (Joint Probability) ที่ทดสอบแล้ว
- ข้อมูลจาก v5: ยืนยันว่า **LC label-bucket 6D/9D แพ้ IVF** ในแง่ Recall/Cost บน embedding จริง
- ข้อมูลจาก v4: `insert 20% โดยไม่ rebuild ผ่าน` (Sparse + ขยายได้ = จุดขายที่ยืนยันแล้ว)

## ผลทดสอบ (Beam Addressing / Foldable Hash)
*(หมายเหตุ: ยังไม่ได้เขียนสคริปต์ทดสอบ — ส่วนนี้จะเว้นว่างไว้ให้ Agent เติมหลังรัน)*
- **Test 1:** ทดสอบ Beam Projection (XY, XZ, YZ) → สร้าง 8 Pyramids → วัดว่า Beam ที่ไม่ทับกัน ให้ Recall ดีกว่า PCA 6D แบบเดิมไหม?
- **Test 2:** ทดสอบ Foldable Hash (`2^n` Shift) → วัดว่า Bit Shift (Fold/Unfold) ให้ความเร็วและ Recall ดีกว่า Ladder Shift แบบคูณ 4 ธรรมดาไหม?
- **Test 3:** ทดสอบ Multi-Probe (Level 0, 1, 2) บน Foldable Hash → ดูว่าแก้ Boundary Problem ได้จริงหรือไม่?

## ข้อควรระวัง
- **Space Collapse:** ถ้าทำ Foldable Hash แบบ "ตัดทิ้ง" (Truncate) ข้อมูลจะกองรวมกัน (Collision สูง) ต้องใช้ **"Multi-Probe (Shift Ladder)"** หรือ **Bitwise XOR Fold** เท่านั้น
- **Sawtooth Phase Matching:** การจับคู่ฟันปลาต้องแม่นยำเรื่อง Phase (จุดเริ่มต้น) ถ้า Phase เพี้ยน แม้ข้อมูลจะคล้ายกัน ก็จะ Match ไม่ติด
- **Projection Loss:** การฉายภาพลงระนาบ XY, XZ, YZ ยังคงเป็น Linear Projection (Lossy) ถ้าใช้กับ embedding 384D อาจจะยังต้องแก้เรื่อง Variance Coverage (เหมือนที่ v5 เคยเจอ)
- **ทิศทาง (Direction):** ต้องแยกให้ชัดว่า Beam Length (Magnitude) คือ Similarity ส่วน Beam Direction (8 Pyramids + 24 Vertices) คือ Address/Identity

## ขั้นต่อไป (เรียงตามราคาถูก)
1. **สร้างสคริปต์ `beam_addressing.py`:** ทดสอบการ Project 3 ระนาบ (XY, XZ, YZ), สร้าง 8 Pyramids, และดึง 24 Vertices
2. **สร้างสคริปต์ `foldable_hash.py`:** ทดสอบ `2^n` Bit Shift (Fold/Unfold) + Bitwise XOR ในการทำ Hash
3. **ทดสอบ Similarity:** ใช้ข้อมูล SIFT 100k หรือ memcore 384D → เทียบ Recall ระหว่าง `Beam Addressing` vs `LC v5` vs `IVF`
4. **ทดสอบ Multi-Probe:** ค้นหาหลาย Level พร้อมกัน (Level 0, 1, 2) เพื่อแก้ Boundary Problem
5. **ทดสอบกับ Matryoshka:** เอา Foldable Hash ไปใส่ใน Layer 7 (code C) ของ Seed 3 แบบ (ตาม Handoff v5)

## จุดตัดสินใจที่เปิดอยู่
- **จะใช้ Foldable Hash แบบไหน?** 
    - แบบ A: Shift Ladder (`n+2` หรือ `n-2`) แล้วค้นหาหลาย Level (Multi-Probe)
    - แบบ B: Bitwise XOR Fold (เช่น `a ^ b`) 
    - แบบ C: ผสมกัน (Shift Ladder + XOR)
- **จะเอา Beam Addressing ไปแทนที่ LC v5 หรือ Union กัน?** (เนื่องจาก v5 บอกว่า LC แพ้ IVF ถ้าใช้ Beam Addressing แล้ว Recall ดีขึ้น จะ Union หรือแทนที่เลย?)
- **8 Pyramids + 24 Vertices เพียงพอต่อการแยกแยะข้อมูล หรือต้องใช้ Icosphere L2 (162 cells) ต่อยอด?**

---

**ข้อความถึง Agent:** 
*"เราเพิ่งค้นพบว่า Beam Addressing (การลากเส้นจาก 0,0,0 ไปหาข้อมูลใน 3 ระนาบ) สามารถ Map เข้ากับ 3letter (XY, XZ, YZ) และ 8 Pyramids (ที่แชร์ Center จาก 2x2x2 Cube) ได้พอดีและมันก็ map เข้ากับ truncated Octahedron ได้ ซึ่งจุดนี้จะให้ Signature ที่ไม่ทับซ้อนกัน (Unique Similarity) เราใช้ `2^n` (Bit Shift) ในการทำ Foldable Hash