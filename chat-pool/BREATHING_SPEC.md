# DWGLS Breathing File System — Spec v0.1

รวมกฎเหล็ก + multi-pointer + vision จากบทสนทนา 2026-09-17 (chat-pool thread)
เป้าหมาย: แปลง file system / db ปัจจุบันให้เป็น breathing FS โดยไม่ทำลายของที่มี
สถานะ: DRAFT — รอ owner ตรวจก่อน implement

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

### ทางเลือก B — log-first (ขยาย pool ตรงๆ)
ทุก record ทุกตารางไหลผ่าน log.jsonl ก่อน (append-only เดียว) — DB เป็นแค่
materialized view ที่สร้างใหม่ได้จาก log — สอดคล้อง "ข้อมูลอยู่เดิม" สุด
แต่ย้ายหนัก ทำทีหลังถ้า A พิสูจน์ว่าเวิร์ค

### กติกาทั้งสองทาง
- INSERT = capture (rkey ติดมาเสมอ) / UPDATE ห้าม (เขียนใหม่เป็น line ใหม่)
- DELETE ห้าม (ถ้าจำเป็น = tombstone line เท่านั้น)
- Active memory = set ของ pointer ที่ session/agent ถืออยู่ชั่วคราว —
  ไม่เขียนกลับเป็น state ของ DB

## 5. สิ่งที่ยังเปิด (ต้องตัดสินใจก่อน implement)

1. vision level กี่ระดับ และแต่ละระดับ "รวม" อะไรบ้าง (ถ่อมไว้: ระดับเดียว =
   เซลล์เดียว ตามที่ทำอยู่ตอนนี้)
2. bond เขียนกลับเข้า log จริงไหม รูปแบบ line หน้าตายังไง
3. multi-human/multi-agent: pointer ต่อ agent แยกกันสุด หรือมีมุมมองร่วมได้
4. RDH 144 พอไหมสำหรับ FS จริง (20736 เซลล์) หรือต้อง stack หลายชั้น

## 6. ตัวอย่างการอ่านแบบ pointer (จากโค้ดจริงวันนี้)

```python
# pointer เกิดที่จุดอ่าน — ระบุ "จุด" ได้จาก rkey ของ line ใดก็ได้
r0 = pool.tail(50)[0]["rkey"]          # จุด = 19450 (เซลล์ x=10,y=135)
pool.neighbors(r0)                      # เห็นเฉพาะเซลล์ที่ชี้ → [(19450, [line])]
# อยากมองจุดอื่น = pointer ใหม่ อ่านใหม่ — ไม่มี state ตกค้าง

# HTTP: GET /neighbors?rkey=19450
```

## 7. หลักฐานอ้างอิง (locked)

- tower geometry locked 2026-09-16 พิสูจน์บน 211,497 chunks (F1)
- geometry = MAP/address, ไม่ใช่ semantic recall (VERDICT จาก F1)
- single writer / lock-free readers, append-only, never demo live
- pool_server + neighbors + bridge: demo 21/21, ใช้งานจริงบน :8390
