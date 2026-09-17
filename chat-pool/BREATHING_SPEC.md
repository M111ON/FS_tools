# DWGLS Breathing File System — Spec v0.1

รวมกฎเหล็ก + multi-pointer + vision จากบทสนทนา 2026-09-17 (chat-pool thread)
เป้าหมาย: แปลง file system / db ปัจจุบันให้เป็น breathing FS โดยไม่ทำลายของที่มี
สถานะ: §1-§5 ตัดสินครบ 4 ข้อ + implement แล้ว (v0.2, 2026-09-17) — เหลือ §4 ทาง A/B

---

## 1. กฎเหล็ก (immutable)

1. **ข้อมูลอยู่ที่เดิมเสมอ** — append-only, never edit, never move, never compact.
   ข้อมูลไม่รู้ตัวว่าถูกย่อหรือขยาย (ไม่มี state ใดๆ เกี่ยวกับการถูกอ่าน)
2. **Address จาก content ตอน capture** — ทุก chunk ถูกเขียน = ถูก capture
   (RDH_CAPTURE_144 → rkey, ring*144+wedge ∈ [0,20736)) พร้อมกัน ไม่ใช่ background job
3. **Integrity = sha256 chain** — RDH เป็น ADDRESS เท่านั้น (MAP ไม่ใช่ semantics:
   kNN บน vec8 = random 1.1x; vec8 = checksum ไม่ใช่ feature; 1800/6072 collisions)
4. **Breathing อยู่ที่ pointer ไม่ใช่ storage** — สิ่งที่ย่อ/ขยายคือ vision ของ
   pointer ที่กระทบ active memory ของผู้อ่านเท่านั้น
5. **Multi-pointer, ไม่มี pointer กลาง** — pointer เกิดที่จุด read ทุกจุด
   (per-read state) เกิด-ตายในการอ่านครั้งเดียว <1ms ไม่มี stream/subscription
6. **อ่านตรงจุดเท่านั้น** — vision เปิดที่จุดที่อ่านจริงจุดเดียว ไม่ลากระหว่างทาง
   อยากมองกว้าง = read ครั้งถัดไปที่จุดใหม่ (explicit ต่อครั้ง)
7. **Determinism จาก logical tick** — replay/สถานะต้องสร้างจาก tick logic
   ห้ามพึ่ง wall clock (ตรงกฎ tower เดิม)

## 2. โมเดล 3 ชั้น

```
DATA (นิ่ง)      : chunks มี address (rkey) + hash — เก็บอย่างเดียว ไม่รู้ตัว
POINTER (ชั่วคราว): เกิดที่จุดอ่าน (rkey + vision level) — ตายเมื่อจบการอ่าน
VISION (ผลลัพธ์)  : สิ่งที่เห็น = ผลของการเปิด pointer — ไม่ถูกเก็บ เกิดใหม่ทุกครั้ง
```

- **bond** = ความสัมพันธ์จริงระหว่างข้อมูล (uses/produces/validates/ตัดสินจาก
  content) — ถ้ามี ต้องเขียนเป็น data เหมือนข้อมูลอื่น (immutable line) ไม่ใช่
  สิ่งที่ geometry เดา — geometry ไม่ตัดสินความหมาย
- **neighbors** = ไม่ใช่ข้อมูลที่เก็บไว้ — คือ "สิ่งที่ปรากฏ" เมื่อ pointer เปิด
  vision ที่จุดนั้น (ตอนนี้ pool ทำ: คืนเฉพาะเซลล์ที่ชี้; วงกว้าง = อ่านเพิ่มเอง)

## 3. สิ่งที่มีแล้วใน pool (พิสูจน์แล้ว 2026-09-17)

- ทุก line มี `rkey` (capture ตอน append) + sha256 chain + `to/tags` v3
- `neighbors(rkey)` — vision จุดเดียว, `GET /neighbors?rkey=`
- L1 block map (`_coarse_map`) + L2 plane map (`_plane_map`) — map ล้วน ไม่มี content
- bond เป็น line ธรรมดา (`kind:"bond"`) — `pool.bonds_for()` / `GET /bonds?hash=`
- pointer layer: `open_pointer()` · `sighting()` (line kind:"pointer") ·
  `pointers_at()` / `GET /pointers` — per-read, ไม่มี pointer state ในระบบ
- stacking: `rkey(data, phase=d)` · `sub_map()` · `_axis()` — คำนวณตอนอ่าน
- dashboard heatmap 144×144 (canvas) — คลิกช่อง = pointer ใหม่, ซูม L1 block ได้
- registry (apps.json), inbox (mention/DM), memory bridge → magic context

## 4. แผนที่ไปสู่ Breathing DB (ขั้นตอนต่อยอด ไม่ทำลาย)

### ทางเลือก A — พาเลเยอร์ทับ SQLite ที่มี (แนะนำเริ่ม)
คง SQLite ทุกตารางเดิม (context.db / pool.db ใช้ต่อได้ทุกอย่าง)
เพิ่ม **ชั้น address** ที่ทำงานเหนือ row:
- ทุกตารางข้อมูลเพิ่มคอลัมน์ `rkey` (capture ตอน INSERT — เขียนเสริมด้วย
  trigger ได้ ไม่แก้โค้ดเดิม)
- **Vision API แทน SELECT แบบเดิม**: อ่านด้วย "จุด + ระดับ" เช่น
  `read_at(rkey)` คืนเฉพาะ row ในเซลล์นั้น; การ join/ดึงข้ามเซลล์ = เจตนา
  ชัดเจนของผู้อ่าน (pointer ใหม่) ไม่ใช่ query ทั่วไป
- ผล: DB เดิมยังใช้เต็มรูปแบบ (MCP/magic context ไม่พัง) แต่ "การมอง"
  เริ่มเป็น pointer-based แล้ว

### ทางเลือก A — ผลการทดลอง (2026-09-17, `ctx_plane.py` — prototype read-only)
พิสูจน์บน `I:\cortexkit\magic-context\context.db` (245MB, ตาราง `memories` 962 rows)
โดย**ไม่แตะ DB จริงเลย** — connection เปิด `mode=ro` ทุกครั้ง, content ไม่เคยถูกคัดลอก,
และ scan จะ raise ถ้าไฟล์ต้นทางเปลี่ยนขนาด/mtime ระหว่างอ่าน (ตรวจแล้วก่อน/หลังเหมือนกันเป๊ะ)

| สิ่งที่ทดลอง | ผล |
|---|---|
| capture ตอน scan | 962 rows → **863 เซลล์** (4.16% ของระนาบ) · 116/144 บล็อก · 0.14 วิ |
| index เป็นแค่ cache | `ctx_plane.db` เก็บ **rowid + address** เท่านั้น (content อยู่ที่ context.db) · fingerprint เดิม = ไม่ rebuild · ลบแล้วสแกนใหม่ = address เดิมทุกตัว |
| vision แทน SELECT | `read_at(rkey, level)` L0/L1/L2 ทำงานครบ · map ไม่มี content หลุดเลย |
| stacking | axis เดียวกับ pool (แกน phase) — เซลล์แน่นเกิน budget แตกได้โดยไม่ต้องมีคอลัมน์เพิ่ม |
| **ระนาบร่วม** | **6 เซลล์มีทั้ง memory และ chat line ของ pool** → ข้อมูลคนละระบบลงระนาบเดียวกันได้จริง ไม่ต้องมี coordinate system ที่สอง |

- **trigger แบบ natives**: `memories_ai` ก็ทำงานจาก `new.id/new.content` แบบเดียวกันอยู่แล้ว
  → ของจริงคือ `AFTER INSERT` trigger ที่คำนวณ rkey (index ในโน้ตนี้คือตัวแทนชั่วคราว)
- **เปิดใช้แล้ว**: `GET /ctx/at?rkey=&level=&path=` และ `GET /ctx/stats` (ผ่าน pool_server)
- ⚠️ **แก้ที่เจอตอนทำ heatmap**: L1 เดิมนับเป็น 144 address *ติดกัน* ซึ่งไม่ใช่สี่เหลี่ยม
  (มันพาดข้ามแถว) → แก้เป็น **สี่เหลี่ยมเชิงพื้นที่ (wedge, ring)** แล้ว — ดู §7

### ทางเลือก B — log-first (ขยาย pool ตรงๆ)
ทุก record ทุกตารางไหลผ่าน log.jsonl ก่อน (append-only เดียว) — DB เป็นแค่
materialized view ที่สร้างใหม่ได้จาก log — สอดคล้อง "ข้อมูลอยู่เดิม" สุด
แต่ย้ายหนัก ทำทีหลังถ้า A พิสูจน์ว่าเวิร์ค

### กติกาทั้งสองทาง
- INSERT = capture (rkey ติดมาเสมอ) / UPDATE ห้าม (เขียนใหม่เป็น line ใหม่)
- DELETE ห้าม (ถ้าจำเป็น = tombstone line เท่านั้น)
- Active memory = set ของ pointer ที่ session/agent ถืออยู่ชั่วคราว —
  ไม่เขียนกลับเป็น state ของ DB

## 5. การตัดสินใจแล้ว (owner ตัดสิน 2026-09-17 · ครบทั้ง 4 ข้อ)

1. **Vision levels: L0 + L1** — L0 = เซลล์เดียวที่ pointer ชี้ (เนื้อหาเต็ม,
   ตามเดิม) L1 = zoom out 144 เท่า (บล็อก 12×12 ของ RDH): ตอบเป็น MAP ล้วน —
   รายชื่อ fine-cell ที่มีของ + นับ line (**ไม่มีเนื้อหาข้ามจุดอ่าน**) อยากเนื้อหา
   ไหนชี้ลง fine-cell นั้นต่อเอง อ่านหยาบคือการอ่าน*ที่จุดใหม่* ไม่ใช่การลากของ
   มาให้ — ยังตรงกฎ "ไม่รองรับระหว่างทาง" เป๊ะ
2. **Bond: line ธรรมดา `kind:"bond"`** — text = `<src64hex> <rel> <dst64hex>`
   ผูก*บรรทัดกับบรรทัด* (hash) ไม่ใช่ cell กับ cell (cell รับได้หลายพัน line —
   หลวมเกิน) bond โดน hash/rkey/chain คลุมเองจาก append() โดยไม่แตะกลไกเดิม;
   bond มี rkey ของตัวเอง = อยู่ cell ของมัน ไม่แอบอยู่ cell ปลายทาง
   (address-from-content ไม่ฉัก)
   - rel ชุดปิด: `uses produces validates decides follows contradicts` +
     ทางหนี `x-<อะไรก็ได้>` + `unbinds` (tombstone: dst = hash ของ bond ที่ยกเลิก)
   - one edge per line, แก้/ลบ = เขียน line ใหม่, append-only ไม่มี UPDATE/DELETE
   - หา bond: `pool.bonds_for(hash)` / `GET /bonds?hash=` — blast radius =
     walk จุดต่อจุด (ชี้ → เห็น edges → ชี้ปลายต่อ) — geometry ชี้ที่, bond บอกความหมาย
3. **Pointer: แยกทุกจุด ไม่มี pointer กลาง — แต่ "awareness" เป็น data ได้**
   pointer เกิดที่จุดอ่านครั้งเดียวและตายกับการอ่านนั้น: ระบบ **ไม่เก็บ pointer
   state ที่ไหนเลย** ไม่ในไฟล์ ไม่ใน server (ตาม §1 ข้อ 5)
   สิ่งที่แชร์กันได้ไม่ใช่ cursor สด แต่เป็น **sighting** = line ธรรมดา
   `kind:"pointer"` text `point=<rkey> level=<n>` แปลว่า "มีการอ่านเกิดขึ้นที่จุดนี้"
   (อดีต ไม่ใช่ปัจจุบัน) → ได้ hash/rkey/chain ฟรีจาก append(), ใครอ่านก็เขียน
   ของตัวเอง, อ่านซ้ำ = line ใหม่ ไม่ใช่ edit
   - awareness มาจาก **ข้อมูล** ไม่ใช่ state: `pool.pointers_at(rkey, level)` /
     `GET /pointers?rkey=&level=` ตอบว่า "ใครอ่านจุดนี้ล่าสุด" — ปิด server ทิ้ง
     คำตอบก็เหมือนเดิม เพราะไม่มี state ให้เสีย
   - `open_pointer(..., record=False)` = อ่านเงียบได้ — sighting เป็น *เจตนา*
     ของผู้อ่าน ไม่ใช่ผลข้างเคียงของการอ่าน
   - bond ผูก sighting ได้เหมือน line อื่น (เช่น `sighting —validates→ DECISION`)
   - ผู้ใช้หลายคน/หลาย agent = หลาย pointer ที่จุดเดียวกันโดยไม่ชนกัน
     (`pointers_at` คืนรายคน) — ไม่มี "มุมมองร่วม" ที่ต้องแย่งหรือล็อก
4. **RDH 144 พอ — เป็นระนาบเดียวกลางที่ไม่ขยาย "ชั้น" มาจากแกน capture ไม่ใช่ field size**
   - `RDH_CAPTURE_SCALE(S)` (144S×144S) **ห้ามใช้กับข้อมูลที่เขียนแล้ว**: มันเลื่อน
     ทุก address = ระนาบร่วมกลางแตกเป็นหลายระบบ = ผิด "ข้อมูลอยู่ที่เดิม" ในเชิง
     ที่อยู่ และทำให้ neighbours/blast radius ข้ามแอปเทียบกันไม่ได้อีก
   - **เพดานของ spatial = L2**: 20,736 = 144² มองได้ 3 แบบเท่านั้น
     (20736 เซลล์ / 144 บล็อก / 1 ระนาบ) ชั้นที่ 4 ไม่เพิ่มอะไร → "stack หลายชั้น"
     ต้องมาจากแกนอื่น ไม่ใช่จาก spatial
   - แกนนั้นคือ **phase**: capture เดินบน content ชุดเดิมแต่เริ่มคนละตำแหน่ง
     (`rkey(data, phase=d)`) — ระนาบเดิม ขนาดเดิม แต่เป็น axis อิสระ
     → เซลล์ที่ line เกิน `VISION_BUDGET` (64) ถูก **แตกเป็น sub-map ชั้น d**
     (144 sub-cell ต่อชั้น คูณ 144 ต่อชั้นถัดไป) อยากได้เนื้อหาชี้ลง sub ต่อเอง
   - **คำนวณตอนอ่านเท่านั้น ไม่เก็บอะไรเพิ่ม**: `_axis(line, d)` คิด sub-address
     ใหม่จาก `app+kind+text` ของ line นั้น (ไบต์ชุดเดียวกับที่ append() capture)
     → line เก่าทุกบรรทัดใน log ได้ความสามารถนี้ย้อนหลังทันที ไม่ต้อง migrate
   - **เดินทีละแกน** (แก้ระหว่างทำ heatmap): แต่ละครั้งที่แตก จะเปิดแกน*
     `len(path)+1` เท่านั้น ไม่กระโดดข้ามชั้น — เพราะแมพที่ key มาจากคนละ depth
     จะ**ระบุด้วย path ไม่ได้** ซึ่งทำลาย "walk จุดต่อจุด" ทั้งหมด
     ตอบกลับมาพร้อม `still_over` / `next_depth` ให้รู้ว่าต้องลงอีกกี่ก้าว
     (utility `_depth_for` ยังอยู่สำหรับคำนวณ/ทดสอบ แต่ read path ไม่ใช้มันข้ามชั้น)
   - determinism ยังครบ: content เดียว + depth เดียว = bucket เดียว ทุก replay
     ไม่พึ่ง wall clock
   - ความจุต่อเซลล์ = 144^d × VISION_BUDGET → d=1 = 9,216 lines, d=2 = 1.33M
   - อ่านทางเดียว: `pool.open_pointer(rkey, level=0|1|2)` /
     `GET /neighbors?rkey=&level=0|1|2` (เกิน budget = มาพร้อม `stacked` เอง)
   - **เวลา = ลำดับการเขียน (`seq`) ไม่ใช่ wall clock** — ระนาบที่ tick หนึ่งสร้างจาก
  การนับ line ตามลำดับ append เท่านั้น (กฎเหล็กข้อ 7) กด scrub กลับไปที่ tick เดิม
  ได้ระนาบเดิมทุกครั้ง · `t` ถูกส่งไปด้วยเพื่อ"อ่านให้คนดู"เท่านั้น ไม่ใช่ตัวคำนวณ
- **mask เมื่อระนาบรก**: เลือกจุดแล้วเปิด "รัศมี" 1–3 hop — ทุกอย่างนอกจาก
  บริเวณที่ต่อจากจุดนั้นถูกหรี่/ตัดออกจากภาพ (edge นอกรัศมีหายทั้งเส้น, edge ที่
  ปลายหลุดออกไปเหลือจาง = ยังมีต่อ) · หมายเหตุ: bond ผูก *บรรทัด* แต่ระนาบเห็นถึง
  *เซลล์* → รัศมีเดินที่ระดับเซลล์ และ bond ในเซลล์เดียวกันวาดเป็น self-loop
- **ข้ามระบบจริง**: 10 edges จาก log → memory ในระนาบเดียวกัน
  (`ctx_plane.links()` **derive** จาก convention ที่ memory bridge เขียนอยู่แล้ว —
  "[chat-pool/<app>] <text>" — ไม่มีข้อมูลใหม่ ไม่มี schema ใหม่) · ทดสอบแล้วว่า
  รัศมี 1 hop กระโดดจากเซลล์ chat ไปเซลล์ memory ได้ (1343 → 1488) และระนาบโชว์
  6 เซลล์ที่มีทั้ง chat line และ memory
- **วัดแล้วว่าแบบไหนอ่านออกที่ขนาดไหน** (`pvBench()` วัดจาก renderer จริง:
  ink = % ของระนาบที่เป็นเส้น · hot = % ของ pixel ที่มีเส้น ≥3 ทับกัน)

  | edges | flat | density | bundle | hub (top 12) | degree |
  |---|---|---|---|---|---|
  | 60 | 21.3% · 0.1 | 15.9% · 0.2 | 20.9% · 0.1 | 15.9% · 0.2 | **1.9% · 0** |
  | 200 | 36.8% · 0.2 | 33.4% · 0.2 | 36.2% · 0.2 | 16.1% · 0.2 | 5.8% · 0 |
  | 600 | 69.1% · 3.4 | 68.1% · 3.5 | 68.6% · 3.4 | **16.7% · 0.2** | 19.3% · 0 |
  | 1200 | 84.6% · 20.8 | 84.2% · 20.9 | 84.2% · 20.9 | **18.7% · 0.2** | 35.3% · 0 |

  - **alpha ไม่ช่วย**: density ต่างจาก flat ไม่ถึง 1.5 จุดทุกขนาด และ bundle
    แทบไม่รวม (เส้นสุ่มแทบไม่ซ้ำคู่เซลล์เดียวกัน) — ปัญหาที่แท้จริงคือ *พื้นที่ที่ถูกทา*
    ไม่ใช่การทับกัน (ที่ 600 edges hot แค่ 3.4%)
  - **hub ต้นทุนคงที่**: ~105 เส้น · ~17% ink ที่ทุกขนาด (60 → 1200) → อ่านออกเสมอ
  - degree ink ต่ำที่ขนาดเล็ก แต่ stroke โตตามจำนวนเซลล์ปลายทาง (1,090 strokes ที่ 600)
  - ฉะนั้น `auto` = วาดทุกเส้นจนถึง ~120 เส้น แล้วเปลี่ยนเป็น hub · และโหมดที่ซ่อนเส้น
    **ต้องบอกว่าแสดงกี่เส้นจากทั้งหมด** (มีใน info line) — ภาพที่แอบตัดครึ่งกราฟ
    แย่กว่าภาพที่รก
- **map = address ล้วน = visibility-neutral** (ไม่มีความลับในแผนที่) จึงไม่ต้อง
     กรอง `as_app`; เฉพาะเนื้อหา L0 ที่ต้องกรอง

## 6. ตัวอย่างการอ่านแบบ pointer (จากโค้ดจริงวันนี้)

```python
# pointer เกิดที่จุดอ่าน — ระบุ "จุด" ได้จาก rkey ของ line ใดก็ได้
r0 = pool.tail(50)[0]["rkey"]          # จุด = 19450 (เซลล์ x=10,y=135)
pool.neighbors(r0)                      # L0: เห็นเฉพาะเซลล์ที่ชี้ → [(19450, [line])]
pool._coarse_map(r0)                    # L1: MAP ของบล็อก 12×12 (address+count ล้วน)
pool._plane_map()                       # L2: MAP ทั้งระนาบ (เพดานของ spatial)
# อยากมองจุดอื่น = pointer ใหม่ อ่านใหม่ — ไม่มี state ตกค้าง

# ทางเดียวที่เปิด pointer ได้ (level 0|1|2) — เกิน budget = stacked มาเอง
pool.open_pointer(r0, level=0)                            # อ่านเงียบ ไม่ทิ้งร่องรอย
pool.open_pointer(r0, level=1, app="codex", record=True)  # + ประกาศว่าอ่านแล้ว
pool.pointers_at(r0)                                      # ใครอ่านจุดนี้ล่าสุด
pool.sub_map(r0, depth=1)                                 # แตกเซลล์ที่แน่นเกิน
pool.open_pointer(r0, level=0, path=(1, 42))               # เดินลงทีละแกน ไม่กระโดดชั้น
pool.all_bonds()                  # ทุก edge พร้อม rkey ของทั้งสองปลาย (วาดบนระนาบได้เลย)
pool.timeline()                   # tick = ลำดับการเขียน (seq) — ไม่ใช่ wall clock
ctx_plane.links()                 # edge ข้ามระบบ (log → memory) — derive ล้วน

# bond: edge คือ line
h1, h2 = pool.tail(2)[0]["hash"], pool.tail(1)[0]["hash"]
pool.bond(h1, "uses", h2, app="opencode")   # line kind:"bond" — chain คลุมเอง
pool.bonds_for(h1[:8])                       # edges ที่แตะ h1 (สองข้าง)
pool.unbind(bond_hash)                       # tombstone — ของเดิมยังอยู่

# HTTP: GET /neighbors?rkey=19450        (L0 · เกิน budget = มี "stacked")
# GET /neighbors?rkey=19450&level=1      (L1 block map)
# GET /neighbors?rkey=19450&level=2      (L2 plane map)
# GET /pointers?rkey=19450&level=0       (sightings = awareness จาก data)
# POST /append {"kind":"pointer","text":"point=19450 level=0"}   (ประกาศการอ่าน)
# GET /bonds?hash=<line hash>            (blast radius ทีละจุด)
```

## 7. หลักฐานอ้างอิง (locked)

- tower geometry locked 2026-09-16 พิสูจน์บน 211,497 chunks (F1)
- geometry = MAP/address, ไม่ใช่ semantic recall (VERDICT จาก F1)
- single writer / lock-free readers, append-only, never demo live
- pool_server + neighbors + bonds + bridge + pointer/stacking: **demo 94/94**
  (temp-dir isolated; จำนวน check อ่านจาก source เอง ไม่มีเลขค้างให้เน่า)
- **เจอบั๊กการอ่าน scale เอง**: `tail(1000)` ที่อ่าน bond คือ read ที่ truncate เงียบ ๆ
  — ที่ 5,000 บรรทัดได้ 200/1,000 edges และ **ทุกเส้นที่ได้ ปลายไม่ resolve** ทั้งที่
  อ่านทั้งไฟล์ก็ราคาเท่ากัน (30ms vs 36ms) → การตัดนั้นขาดทุนเปล่า
  แก้เป็น streaming + `tail(None)` = ประวัติทั้งหมด + index ที่ fold จาก log
- **CRLF ที่ไม่มีใครรู้**: log ถูกเขียนด้วย `os.open` ที่ไม่มี `O_BINARY` → fd เป็น
  text mode บน Windows ทำให้ `"\n"` กลายเป็น `"\r\n"` (live log มี CR 33/33 บรรทัด)
  chain ไม่เคยสนใจ (hash คิดจาก content, `_prev()` อ่าน JSON ซึ่งทน \r)
  แต่ **ห้ามคำนวณ byte จากสมมติฐาน** — ตัวกัน cache ใน `_derived_absorb` จึงเช็ค
  "บรรทัดสุดท้ายเป็นบรรทัดของเราไหม" (`_last_line_is`) แทนการบวกความยาว
- **gate ใหม่ `ui_check.py`**: วันที่ JS พังทั้งหน้า (object literal ขึ้นต้นด้วย `(`)
  ไม่มีเทสต์ไหนจับได้ — server ยัง 200 และ demo ยัง PASS ครบ
  ตอนนี้ `node --check` บน <script> ที่ดึงจาก pool_server.py จริง · ไม่มี node = รายงานว่า skip
- `ctx_plane.py` self-check ผ่านบน fixture · ทดลองจริงกับ context.db โดยไม่เขียนเลย
  (size/mtime ก่อน-หลังเท่ากันเป๊ะ) · 0.14 วิ ต่อ 962 rows
- **แก้ L1**: เดิมนับ 144 address ติดกันเป็น "บล็อก 12×12" ซึ่งไม่ใช่สี่เหลี่ยมจริง
  (พาดข้ามแถว) — เจอตอนวาด heatmap แล้วแก้เป็นสี่เหลี่ยม (wedge, ring) + เทสต์กันไว้
  (บทเรียนเดียวกับตอน demo ลบ log: ของที่ดู "น่าจะถูก" ต้องมีเทสต์จับ)
- heatmap ยืนยันจากหน้าเว็บจริง: 28 เซลล์/29 lines บนระนาบ · คลิกช่อง = pointer ใหม่
  · ซูม L1 block ได้ · sub-grid 144 ช่อง + เจาะลงชั้นได้ (`path` ถูกส่งจริง)
- **bonds บนระนาบ**: edge 1 เส้น (`6755d917 —produces→ 1e39b4ed`) วาดจาก rkey 19450 ไป
  446 · เลือกจุดไหน = edge ที่แตะจุดนั้นเปลี่ยนเป็นสีส้ม + คลิกปลายทางเดินต่อได้ (walk จริง)
- **scrubber (tick)**: ลากไป tick 10 → ระนาบเหลือ 11 lines และ **edge หายไป**
  (bond ถูกเขียนที่ tick 22) · tick 25 → edge กลับมา · กด replay แล้วจบที่ 29/29
  · ระนาบที่คำนวณจาก timeline ตรงกับ map ที่ server ตอบเป๊ะ (cross-check แล้ว)
- 2026-09-17: crashed demo เคยลบ log จริง → กู้จาก pool.db chunk + re-append ที่รู้เนื้อหา,
  demo ถูก isolation ใน temp dir ตั้งแต่นั้น (ของจริงแตะไม่ได้แม้ demo พัง)

---

## 8. การวัดสองเรื่อง (2026-09-17 · `bond_scale.py` + `pvBenchRel` บนหน้าเว็บจริง)

### 8.1 การอ่าน bonds ที่สเกลพันเส้น — index ควรมีไหม ควรอยู่ที่ไหน

วัดจาก renderer/storage จริง ไม่ใช่แบบจำลอง (`python bond_scale.py`):

| log lines | bonds | tail(20) | tail(1000) | read ครบ | index cold | index warm |
|---|---|---|---|---|---|---|
| 600 | 100 | 0.3 ms | 3.6 ms | – | 4.4 ms | 0.08 ms |
| 6,000 | 1,000 | 0.3 ms | 8.7 ms | ~40 ms | 50.8 ms | 0.12 ms |
| 22,000 | 2,000 | 0.3 ms | 7.9 ms | ~140 ms | 177 ms | 0.12 ms |
| 55,000 | 5,000 | 0.3 ms | 5.4 ms | **336 ms** | **481 ms** | 0.13 ms |

- `tail(n)` **ไม่โตตามไฟล์อีกแล้ว** (อ่านจากท้ายแบบ streaming) → 0.3 ms ที่ทุกขนาด
- ตัวที่โตคือ **read ทั้งหมด** (336 ms ที่ 55k lines / 15.9 MB) และมันคือ JSON
  parsing ล้วน ๆ — นี่คือราคาที่ index มีไว้จ่ายครั้งเดียว
- **append 1 บรรทัดแล้วอ่านต่อ: 0.05 ms** (จาก 457 ms ถ้า cache ถูกทิ้ง) — เพราะ log
  เป็น append-only index จึงเป็น *fold* ที่รับบรรทัดใหม่ได้โดยไม่ต้องสร้างใหม่
  (`_derived_absorb`) · bond ใหม่ = re-resolve เฉพาะ edge list (10 ms)
- **สรุปที่ตัดสิน**: index คุ้ม และอยู่ "ในโปรเซสที่อ่าน" — fold จาก log, ไม่ persist,
  ไม่เป็นความจริง (log เป็น) · ที่เขียนลงดิสก์ค่อยทำเมื่อ cold start เจ็บ
  และบ้านของมันคือ `pool.db` ที่ flush รายชั่วโมงสร้างอยู่แล้ว
- ตัวกันความถูกต้อง: cache อัปเดตเฉพาะเมื่อ key ตรง **และ** บรรทัดท้ายของไฟล์
  เป็นบรรทัดที่เราเพิ่งเขียน — ถ้ามีโปรเซสอื่นแทรก cache ถูกทิ้งแล้วสร้างใหม่
  (ยอมช้า ไม่ยอมโกหก)

### 8.2 สีตาม rel — ยังอ่านออกเท่าอยู่ hub mode ไหม

วัดบนกราฟเดียวกันเป๊ะ สลับแค่ "มีสี/ไม่มีสี" (`pvBenchRel`) · `mixed` = pixel ของ
เส้นที่ถูกทับด้วย rel **คนละตัว** (อ่าน rel ไม่ได้) · hue ไม่เปลี่ยนจำนวน pixel ที่ถูกทา:

| edges | rel ทั้งหมด (mixed) | rel+hub (mixed) | ราคาของสี |
|---|---|---|---|
| 60 | 9.7% | 4.4% (23 เส้น) | +9.7 pt |
| 200 | 28.2% | 15.4% (71 เส้น) | +28.2 pt |
| 600 | 60.1% | 44.2% (206 เส้น) | +60.1 pt |
| 1,200 | 78.4% | 68.5% (409 เส้น) | +78.4 pt |

- **สีไม่ทำให้ภาพแน่นขึ้นเลย** (68.03% vs 68.33% ที่ 600) — ราคาของมันคือ
  *การระบุได้* ซึ่งแย่ลงเร็วตามจำนวนเส้น: ≤60 เส้นยัง 90% อ่าน rel ได้,
  600 เส้นเหลือ 40%
- **hub ลดราคาของสีด้วย** (ตัด overlap): rel+hub ที่ 600 = 22.9% ink / 55.8% pure
  ดีกว่า rel ล้วนที่ 600 (68% ink / 39.9% pure) มาก
- **ตัดสิน**: เปิดสีตาม rel เป็นค่าเริ่มต้น (ราคาเกือบศูนย์จนถึง ~100 เส้น)
  rel เป็นความหมายของกราฟ ไม่ใช่ของประดับ · เกิน ~200 เส้น legend บอกเองว่า
  "สีเริ่มทับกัน — คลิกจุดเพื่อดู rel ให้แน่" · สีที่หลุดออกไปเป็นของ
  `x-<free>` ซึ่ง hash เอา (ไม่ปฏิเสธ แล้วก็ไม่ชนกับชุดปิด)
- **การเลือกจุดไม่ใช้ hue อีกแล้ว**: เดิมเส้นที่แตะจุดที่เลือกเป็นสีส้ม ซึ่งชนกับ
  `decides` พอดี → เปลี่ยนเป็น **ขาว + หนาขึ้น + วาดท้ายสุด** (รูปทรง ไม่ใช่สี)
- legend บอก `วาด/ทั้งหมด` ต่อ rel (นับตาม content ไม่ใช่ object identity —
  เคยขึ้น 0/1 ทั้งที่ระนาบวาดอยู่) และบอกจำนวนเส้นที่ hub ตัดออก
