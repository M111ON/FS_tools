# HANDOFF — LC_tilecube v5 (draft) · Hourglass/Tesseract + union กับ IVF · overlap test: 6D LC ไม่เพิ่ม recall ที่ cost เท่ากัน

## เป้าหมายรอบนี้
ดูว่า primitive ของ DWGLS (hourglass 24-cell, tesseract frame seek) เสริม LC_tilecube v4 ได้อย่างไร และ union geometry+IVF คุ้มไหม

## ไอเดีย (จากเจ้าของโปรเจกต์)
- hexagon = 3 hourglass pair, subdivide x4 → 24 cell (6x4 หรือ 8x3); LC โครงตรงกับ tesseract (8 cell = axis x sign, 24 หน้า)
- เก็บข้อมูลใน 4D แล้วอ่านแบบ frame seek (pin frame แล้ว seek slot) เพื่อลดข้อจำกัดมิติน้อยของ geometry
- แผน: union กับ IVF เสมอ; geometry (ประหยัด memory) ก่อนสำหรับ bucket เล็ก/threshold ต่ำ แล้วเสริมด้วย IVF/HNSW/FTS

## สิ่งที่มีอยู่แล้วใน DWGLS (I:\DWGLS-native-fs)
- `core/geo_tesseract_addr.h` fixed-frame 4D addressing, idx=(axis<<1)|sign, adjacency XOR (deg 3) / true tesseract (deg 6)
- `core/geo_frame_seek.h`, `core/geo_tesseract_dense.h` (เก็บ 4 cube derive 4 antipodal)
- `tests/test_addr_orbit.c` T12-T15 (commit b866519): `(i<<1)%20736` ไม่ใช่ permutation; rot8 บน i/81 เป็น bijection; crossing = `d^1` (6 pair, 0 fixed), k ครั้ง = `d^(k&1)`
- `docs/TESSERACT-BINOMIAL-READINGS-2026-10-22.md`: audit; "no code joins" 16-node กับ 6-direction → การต่อเข้า LC เป็น assembly ครั้งแรก ต้องเป็น experiment

## ผลทดสอบ (overlap_union.py, v12 `stats`, 6D, top-1 hit, test half 326 q)
เกณฑ์: union recall − IVF(nlist256) ที่ cost รวมเท่ากัน (LC% + IVF%)
- LC rescue (P(LC hit | IVF miss)) = 0.10–0.61 ขึ้นกับ budget แต่ **gain ≈ 0 หรือติดลบทุกช่อง** (ดีสุด +0.004 ที่ nlist256 p=8, อยู่ใน noise ±0.05)
- nlist=64 ติดลบชัด (−0.03…−0.18) — IVF เดี่ยวใช้ cost เท่ากันได้ recall สูงกว่า
- สรุป: บน embedding (PR~80) LC 6D เป็นตัวเสริม IVF ไม่คุ้มที่ cost เท่ากัน สอดคล้องข้อสรุป v4

## ข้อควรระวัง
- cost = ผลรวม candidate% (ไม่หัก candidate ที่ซ้ำกัน → ประเมินสูงไปเล็กน้อย); baseline = IVF256 interpolate
- วัดเฉพาะ top-1 recall, หมวด `stats` หมวดเดียว, 6D เท่านั้น (9D ยังไม่ได้รันใน test นี้), ไม่ได้จูน IVF
- ยังไม่ได้ทดสอบ gate "bucket เล็ก → เชื่อ geometry" แยก และยังไม่ได้ทดสอบแนว 4D multi-frame (w = หลาย projection)
- Layer 2 (v±c) ไม่ลด RAM (ΔH=ΔI): RAM 4x มาจาก uint8; ใช้ XOR/mod3 บน end/path ถ้าต้องการ bijection address
- memory ที่ geometry ประหยัดได้จริง = แทน HNSW graph (~100+ B/vec) ไม่ใช่ vector

## ขั้นต่อไป (เรียงตามราคาถูก)
1. รัน `overlap_union.py stats 1000 9` และหมวด `unix`,`tex` (ยืนยันว่า 6D ไม่ใช่ข้อยกเว้น)
2. ทดสอบ gate: precision ของ LC เมื่อ bucket เล็ก (|bucket|<T) → ถ้าสูงมาก ใช้เป็น early-exit ได้โดยไม่ต้อง union
3. ทดลอง 4D multi-frame: w = เลือก 1 ใน 4 projection (PCA ชุดต่าง/metric-learned) + XOR probe cell ข้างเคียง วัด recall vs candidates เทียบ LC 6D เดี่ยว
4. engineering v4 ที่ค้าง: D แบบพารามิเตอร์ + int8 path ใน lc_pipe3.c (ไม่ขึ้นกับ geometry)

## จุดตัดสินใจที่เปิดอยู่
- ถ้าข้อ 1-2 ยังไม่เห็นประโยชน์ ย้ายเป้าหมายของ geometry ไปที่ "แทน HNSW graph/routing ชั้นแรก" และใช้ engineering v4 กับ IVF เป็นหลัก
- ข้อ 3 เป็นทางเดียวที่แก้ "มิติน้อยเกินไป" จริง (เพิ่มข้อมูล ไม่ใช่ relabel address)


## [เพิ่ม] ผลทดสอบเพิ่มเติม (label-bucket LC เท่านั้น)
- memory vs recall (stats, 42,269x384): HNSW graph M=4/8/16/32 = 49/81/144/272 B/vec; IVF overhead ~0-4 B/vec (vector เรียงตาม list); LC ~4-12 B/vec (ประมาณ ไม่ได้วัด) → IVF ต่ำกว่าและ recall สูงกว่า LC (0.72 vs 0.42 ที่ cand ~0.85%)
- multi-frame (multiframe.py, frame = 6D cubic cell จาก PCA, union จริง): F=2/4 ดีกว่า single 6D ~+0.03-0.04 ที่ cand เท่ากัน (ใน noise ±0.05) ช่องว่างถึง IVF ~0.4 · จำลองแบบง่าย ไม่ใช่ BCC+door ของ LC จริง และ frame ไม่ใช่ Fruit

## [แก้ขอบเขตข้อสรุป]
ผลทดสอบทั้งหมดข้างบนใช้ได้กับ LC **label-bucket (v8) เท่านั้น** ยังไม่ได้ทดสอบ LC แบบ **Snake -> Fruit/Onion** (SYSTEM-DOCUMENT.md v1.0): grow 6 ทิศ, branch, cut->fruit, graft, grape cluster; Fruit มี embedding_dim/adapter/CommonSpace ของตัวเอง (fruit_dim.py กฎ D1-D4). ข้อสรุป "ข้อจำกัดมิติแก้ไม่ได้ / multi-frame ไม่ช่วย" ใช้กับ frame แบบ PCA ที่จำลองเท่านั้น

## [แนวคิดเจ้าของโปรเจกต์ 2026-11-08] 4D = subgraph ของ node
- ข้อมูลใน node บันทึกเป็น onion; seed ที่ใส่ใน node ล็อก scope/range; เหมือน image sequence: เปลี่ยน header -> frame ที่ seek ข้างในเปลี่ยนตาม
- ตัวอย่าง: ข้อมูล 1M แบ่ง 4 ชุด ย่อเป็นชั้น (4<16<256<1024); seed บอกว่าข้อมูลจอดที่ไหนทีละชั้น; **ไม่สร้างรอไว้ล่วงหน้า** มี 5 พิกัดก็นับ 5 แค่รองรับการขยาย

## [เพิ่ม 2026-11-08 ท้ายเซสชัน] ผลต้นไม้ 4>16>64>256 (treeseed.py, stats 42,269x384, 652 q, true top-5)
- placement = greedy nearest-child descent (ตำแหน่งกำหนดจากข้อมูล) · recall@5 / ครบ 5 / cand: beam1 0.29/3%/0.45% · beam4 0.63/21%/1.75% · beam16 0.87/56%/7.0%
- flat IVF256 ที่ cand เท่ากัน: nprobe1 0.52/15%/0.43% · nprobe4 0.78/46%/1.67% · nprobe16 0.93/75%/6.4% → tree แย่กว่า flat IVF
- **insert 20% โดยไม่ rebuild ผ่าน:** recall@5 แทบเท่า build ใหม่ (beam4 0.625 vs 0.631; beam16 0.881 vs 0.872) = จุดขายที่ยืนยันแล้ว (sparse + ขยายได้)
- boundary: P(NN อันดับ1 อยู่ cell เดียวกับ query) L1(4)=0.76, L2(16)=0.57, L3(64)=0.44, L4(256)=0.33 → seed ถูกเสมอ แต่ seed ของ query กับของเพื่อนบ้านแยกกันที่ขอบ cell (discontinuity ของ partition discrete; ใน 384D จุดส่วนใหญ่อยู่ใกล้ขอบ)

## [สรุปทิศ] seed 3 แบบ + matryoshka
| seed จาก | path = | similarity |
|---|---|---|
| A hash ปกติของ embedding (sha256, ที่ ingest_fingerprint.py ใช้) | identity | ไม่มี |
| B hash ของ scope/metadata (source, เวลา, frame, header) | ขอบเขต exact | ไม่ต้องใช้ (scope ตรงเป๊ะ) |
| C LSH/learned code | ความใกล้ | มี แต่ชน boundary ตามตารางข้างบน |
- ภาพเจ้าของโปรเจกต์ (matryoshka ~8 ชั้น): seed เดินถึงชั้น 7 = ทางเข้า, ชั้น 8 = vector DB; ชั้นระหว่างทางเดินตาม seed ที่สร้างแรกสุด (ต้อง hash/สร้าง seed จาก embedding ก่อน)
- ข้อเสนอ: ชั้นบน = seed B (scope), ชั้น 7 = code C ไม่กี่ bit, ชั้น 8 = vector DB เล็ก (fan-out 4: 4^7=16,384 ทางเข้า, 1M/16K ~ 60 vec/leaf -> brute-force) · ได้ผลเมื่อ query รู้ scope ล่วงหน้า (เหมือน source/since/days ของ memcore_query); ไม่มี scope + seed A = scan ทุก leaf
- หมายเหตุ: Matryoshka representation learning (prefix 64>128>256>384) ใช้ได้เฉพาะโมเดลที่เทรนแบบนั้น; all-MiniLM-L6-v2 (v12) ไม่ได้เทรนแบบนี้เท่าที่ทราบ

## [สถานะไฟล์ใหม่ใน LC_tilecube] (ไม่แตะของเดิม)
overlap_union.py (union กับ IVF) · multiframe.py (multi-frame 6D) · treeseed.py (tree + boundary + insert) · logs: overlap_union.log, multiframe.log

## [ข้อสรุปรวม v5]
1. label-bucket LC 6D/9D ไม่มีช่องตลาดบน embedding เทียบ IVF (recall/cost/memory) และ union/multi-frame ไม่ช่วยในระดับที่ปิดช่องว่าง ~0.4
2. จุดที่ยืนยันแล้ว: insert โดยไม่ rebuild ใช้ได้, sparse allocation ใช้ได้, hourglass/tesseract address (T12-T15, XOR crossing, frame seek) เป็นของ DWGLS ที่พิสูจน์แล้ว
3. Snake/Fruit/Onion ปัจจุบัน = โครง identity/growth + routing แบบ hash ยังไม่มี similarity search จึงยังเทียบกับ IVF/HNSW ไม่ได้

## [ขั้นต่อไป (แทนรายการเก่าด้านบน)]
1. วัดแบบ B+C: scope = subforum (stats/unix/tex), ชั้น 7 = SimHash 4-6 bit, ชั้น 8 = brute-force; recall@5 vs candidates เทียบ flat IVF
2. ทดสอบ boundary duplication (ตัวใกล้ขอบเก็บซ้ำ 2 cell) วัด recall@5 vs memory
3. อ่าน HANDOFF-2026-10-14d.md (DWGLS: anchor+frustum+CAPO) ว่าแก้ boundary แล้วหรือยัง
4. ร่าง spec node = 4D subgraph (header/seed/layer/frame seek, ชั้นไหนใช้ seed ชนิดไหน, ชั้น 8 รับอะไร)
5. engineering v4 ที่ค้าง: D แบบพารามิเตอร์ + int8 path ใน lc_pipe3.c

## [จุดตัดสินใจที่เปิดอยู่]
- seed ของชั้น 7 มาจากแบบ B หรือ C (หรือผสม) · ชั้น 8 = brute-force / IVF เล็ก · ปิดทิศ LC-as-quantizer สำหรับ embedding แล้วใช้ geometry เป็น address/scope/frame seek หรือไม่
