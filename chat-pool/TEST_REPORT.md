# chat-pool — รายงานระบบทดสอบ และคู่มือใช้งาน

อัปเดต: 2026-09-20

---

## ตัวเลขรวม

```
9 suites · 408 assertions · 23s wall time · 0 failures
โค้ดหลัก     7,042 บรรทัด  (pool.py + pool_server.py)
โค้ดเทสต์    2,152 บรรทัด  (9 ไฟล์)
ตัวเรียก      72 บรรทัด    (run_all.py)
```

---

## รายงานผลล่าสุด

```
  PASS  pool.py demo            1.1s  POOL-SELFCHECK: 306/306 PASS
  PASS  ctx_plane               1.5s  CTX-PLANE SELFCHECK: PASS
  PASS  ui_check                0.5s  138,016 bytes · 15/15 invariants
  PASS  stream_check            8.9s  14/14 PASS
  PASS  prune_check             2.0s  13/13 PASS
  PASS  reconnect_check         3.9s  14/14 PASS
  PASS  error_check             1.1s  6/6 PASS
  PASS  curator_check           0.1s  37/37 PASS
  PASS  latency_check           4.0s  8/8 PASS
  ── 9/9 PASS  (23s wall time) ──
```

---

## วิธีใช้งาน

### รันทั้งหมดพร้อมกัน

```bash
python run_all.py
```

- Exit 0 = ทุกอย่างเขียว
- Exit 1 = มีตัวแดง แสดงชื่อ + สาเหตุ
- ใช้เวลา ~23 วินาที

### รันแยกตัว

```bash
python pool.py demo           # core logic 306 assertions
python ctx_plane.py demo      # ระนาบ 144×144
python ui_check.py            # dashboard JavaScript + 15 invariants
python stream_check.py        # SSE + ขา input 14 checks
python prune_check.py         # ตัวกรองขยะ 13 checks
python reconnect_check.py     # SSE backoff formula + server reconnect 14 checks
python error_check.py         # error ทุกอันมีประโยคภาษาไทย 6 checks
python curator_check.py       # CURATOR_RULES ทุกตัวจับข้อความจริง 37 checks
python latency_check.py       # SSE ไม่ deadlock ใต้ latency สูง 8 checks
```

### เมื่อไหร่รันอะไร

| สถานการณ์ | รัน |
|---|---|
| เขียนโค้ดเสร็จ | `python run_all.py` |
| แก้ bug | `python run_all.py` (ดูตัวที่พัง) |
| เพิ่ม CURATOR_RULES | `python curator_check.py` |
| แก้ dashboard | `python ui_check.py` |
| แก้ SSE/stream | `python stream_check.py` + `python reconnect_check.py` |
| แก้ error message | `python error_check.py` |
| ก่อน deploy | `python run_all.py` (ทุกตัวต้องเขียว) |

---

## รายงานละเอียดแต่ละ Suite

---

### 1. `pool.py demo` — 306/306

ทดสอบ core logic บน in-memory + temp file ไม่ต้องมีเซิร์ฟเวอร์

| ครอบคลุม | จำนวน |
|---|---|
| register_app / append / hash chain | ~80 |
| curator_hits / curator_protect | ~40 |
| block (write/read/timeline) | ~50 |
| plane (address, cell, bond) | ~60 |
| inbox / DM / mentions | ~30 |
| prune / score / protect | ~20 |
| edge cases (empty, duplicate, long) | ~26 |

---

### 2. `ctx_plane.py demo` — PASS

ทดสอบระนาบ 144×144: address calculation · cell assignment · bond drawing · explore

---

### 3. `ui_check.py` — 15/15 invariants

ตรวจ dashboard JavaScript บนหน้าเว็บจริง

| # | invariant | ถ้าหาย = |
|---|---|---|
| 1 | `send()` ล้างช่องใน `if(r.ok)` เท่านั้น | ข้อความหายตอนส่งไม่ผ่าน |
| 2 | `send()` มี `.catch` บอกผู้ใช้ | rejection ลอยเปล่า |
| 3 | `setIdentity` เรียก `listen()` | feed ไม่ตาม identity |
| 4 | `#id-sel` สร้างจาก `identity` | ตัวเลือกไม่ตรงค่าจริง |
| 5 | popup/tooltip เป็น `popover` | ถูกตัดบนมือถือ |
| 6 | `canvas.width` ตั้งเฉพาะเมื่อขนาดเปลี่ยน | สะดุดทุกเฟรม |
| 7 | popup clamp เทียบ viewport | ตัดบนจอกว้าง |
| 8 | header เขียนสั้น + tooltip ยาว | กิน 3 แถว |
| 9 | options เริ่มซ่อนหลัง ⚙ | chrome 38% จอ |
| 10 | ไม่มีศัพท์บนหน้าจอ | คนอ่านงง |
| 11 | error แปลเป็นไทย ไม่พิมพ์ดิบ | ข้อความ error โกหก |
| 12 | ทุก filter rule มีคำไทย + ทุกคำไทยมี rule | ชื่อ code หลุดบนจอ |
| 13 | บันได pinch = ห้าขนาดที่ `#pv-win` มี | ซูมไปขนาดที่เลือกกลับไม่ได้ |
| 14 | นิ้วที่สองทิ้งการลาก + กลืนคลิก | แผนที่กระโดด / เปิดช่องที่ไม่แตะ |
| 15 | ที่อยู่ของผังบล็อก: คีย์ · ภาพ · คลิก · แว่น ตรงกัน | ผังวาดที่หนึ่ง คลิกรายงานอีกที่ |

#13–15 รันโค้ดจริงใน node (สกัด `pvWrap` `pvB` `pvXY` `pvRkeyAt` `pvDerive` `pvValAt`
ออกจากสคริปต์ที่เสิร์ฟจริง) ไม่ใช่จับ regex — เพราะเวอร์ชัน regex **ผ่านทั้งที่บั๊ก**:
ทั้งสองฟังก์ชันอ้าง `PV.bOrig` ครบ แต่เลขคณิตยังผิด (#15 จับ 336–8064 ที่อยู่ที่ไม่ตรง)

**mutation-tested** — ทุก invariant พิสูจน์แล้วว่ากัดจริง (ชุดหลังสุด **7/7**)

---

### 4. `stream_check.py` — 14/14

ทดสอบ SSE + ขา input บน socket จริง (ไม่ใช่ mock)

| ทดสอบ | วิธี |
|---|---|
| bad-body-answers | POST `{broken json` → ต้องตอบ 400 |
| empty-body-answers | POST `` → ต้องตอบ 400 |
| non-object-answers | POST `[1,2,3]` → ต้องตอบ 400 |
| bad-body-blames-body | error message ต้องบอก "JSON object" |
| serving-after-bad-body | health ยัง 200 หลัง bad body |
| stream-up | SSE เชื่อมต่อได้ |
| no-backfill | ไม่ส่ง backlog ตอนเชื่อมต่อ |
| live-past-window | บรรทัดใหม่มาถึงแม้ log ยาวกว่า window |
| frame-has-tick | frame มี seq number |
| server-survives-rst | hard RST → เซิร์ฟเวอร์ไม่ตาย |
| health-after-rst | health ยัง 200 หลัง RST |
| reconnect-after-drop | reconnect สำเร็จ |
| no-traceback | ไม่มี Python traceback ใน stderr |

---

### 5. `prune_check.py` — 13/13

ทดสอบตัวกรองขยะ: raw mode · prune on/off · protect reasons · score calculation

---

### 6. `reconnect_check.py` — 14/14

ทดสอบ SSE reconnect สองชั้น:

**Server side (5 checks):**
- เปิด SSE → รับบรรทัด → hard RST → เซิร์ฟเวอร์รอด → reconnect → บรรทัดใหม่มาถึง

**Formula side (9 checks):**
- สกัดสูตรจาก `pool_server.py` แล้วจำลองใน Python
- cap = 4 · base = 3000ms · sequence = `3→6→12→24→24→24...`
- reset เป็น 0 ตอน onopen → delay กลับ 3s

```
สูตร: delay = 3000 × 2^(min(retry, 4) - 1) ms

retry 1 → 3,000ms  (3s)
retry 2 → 6,000ms  (6s)
retry 3 → 12,000ms (12s)
retry 4 → 24,000ms (24s)
retry 5 → 24,000ms (24s cap)
onopen  → reset เป็น 0
```

---

### 7. `error_check.py` — 6/6

ทดสอบ error ทุกอันมีประโยคภาษาไทย

ยิง junk 39 ครั้ง ทุก endpoint ทุกวิธีที่ผิด แล้วแยก error เป็น 3 ชั้น:

| ชั้น | ความหมาย | ผ่านเมื่อ |
|---|---|---|
| **NAMED** | pattern จับได้ → ประโยคบอกสาเหตุจริง | ทุกอันต้องเป็น NAMED |
| **STATUS** | pattern ไม่จับ แต่รหัสสถานะอยู่ในตาราง | ไม่ควรมี |
| **GENERIC** | ไม่จับอะไรเลย | ต้องเป็น 0 |

| สายไฟว่า | toast บอก |
|---|---|
| `body must be a JSON object` | ข้อมูลที่ส่งไปอ่านไม่ได้ |
| `no ctx index yet` | ยังไม่มีข้อมูลความจำกลาง |
| `token required` | ยังไม่ได้เข้าสู่ระบบ |
| `title required` | ต้องมีชื่อเรื่อง |
| `status must be one of` | สถานะไม่ถูกต้อง |
| `ctx_plane unavailable` | อ่านข้อมูลความจำกลางไม่ได้ |

---

### 8. `curator_check.py` — 37/37

ทดสอบว่าทุก CURATOR_RULES rule จับข้อความจริงได้

| Rule | จับอะไร | ตัวอย่างที่จับ | ตัวอย่างที่ไม่จับ |
|---|---|---|---|
| test-word | คำทดสอบล้วน | `test`, `ping`, `hello` | `การทดสอบจบแล้ว` |
| test-probe | คำสั่งทดสอบ | `test: ok`, `probe=1` | `probe ตัวนี้ทำงาน` |
| stub | ข้อความว่างเปล่า | `…`, `..` | `ได้เลย` |
| ack | คำรับทราบ | `ok`, `ครับ`, `รับทราบ` | `ok ครับ จะลองดู` |
| sess-id | session ID | `ses_abc123def456gh` | `session ใหม่` |
| hex-blob | hex ยาว 32+ ตัว | `a1b2c3d4...` | `ข้อความทั่วไป` |
| b64-blob | base64 ยาว 60+ ตัว | `YWJj...==` | `base64 encode` |
| punct-soup | เครื่องหมายล้วน | `...!!!---` | `ข้อความทั่วไป` |
| loud | ตัวอักษรซ้ำมาก 8+ ตัว | `AAAAAAAAAAAAAAAAAAAA` | `ข้อความปกติ` |

ทุก rule ต้อง:
- `catches-<rule>`: ยิงข้อความที่ควรจับ → ต้อง True
- `spares-real-<rule>`: ยิงข้อความคุยจริง → ต้อง False

---

### 9. `latency_check.py` — 8/8

ทดสอบว่าเซิร์ฟเวอร์ไม่ deadlock ใต้ latency สูง

| ทดสอบ | จำลองอะไร |
|---|---|
| slow-client-gets-all | client อ่านช้า (sleep 500ms) → ได้ครบ 5 บรรทัด |
| fast-client-gets-lines | slow client ยังเชื่อม → fast ยังได้บรรทัดใหม่ |
| server-healthy | health ยัง 200 ระหว่าง slow read |
| survives-rapid-cycles | เปิดปิด SSE 10 ครั้งเร็ว → เซิร์ฟเวอร์ไม่พัง |
| delivery-after-rapid | หลัง cycles → บรรทัดใหม่ยังมาถึง |
| no-traceback | ไม่มี Python traceback |

---

## สิ่งที่แก้ไปทั้งหมด (4 ระยะ)

### ระยะ 1: ขา input

| จุดที่พัง | ก่อน | หลัง |
|---|---|---|
| json.loads ไม่มี guard | ปิด socket เงียบ (status 0) | 400 + error message |
| GET endpoint ไม่ guard int() | 6 endpoint ตอบไม่ได้เลย | 400 "?n=\<int\> required" |
| error label โกหก | "not registered" ทั้งที่ body พัง | 400 "JSON object" |
| composer ทิ้ง draft | ข้อความหายตอนส่งไม่ผ่าน | ข้อความยังอยู่ + toast แจ้ง |
| SSE ไม่มี backoff | วน 3s ตลอดไป | 3→6→12→24s cap + หยุดที่ 401 |

### ระยะ 2: management (แผง ☰)

| จุดที่พัง | ก่อน | หลัง |
|---|---|---|
| เปลี่ยน identity stream ไม่ตาม | feed ยังโชว์ admin | stream.url มี as= ใหม่ |
| ทะเบียน error ซ่อนทั้งกล่อง | "ต่อไม่ได้" = "ยังไม่มีใคร register" | ⚠ โหลดทะเบียนไม่ได้ |
| Escape ตีกัน | ปิด modal แล้ว scrim ค้าง | ปิดตามลำดับชั้น |
| /flush เงียบ | ปุ่มไม่ตอบอะไรเลย | มี catch + error message |

### ระยะ 3: ระนาบ

| จุดที่พัง | ก่อน | หลัง |
|---|---|---|
| popup ถูกตัด | ปิดปุ่มหายบนมือถือ | popover top-layer |
| แว่นสะดุด | 27.7ms/เฟรม (27 fps) | 2.6ms/เฟรม |
| canvas realloc ทุกเฟรม | 30 ครั้ง/30 draw | 0 ครั้ง |
| pvGraphEdges สร้างใหม่ทุกครั้ง | 13,572 objects ×4/เฟรม | memoised |
| chrome 38% จอ | 272px จาก 709px | 121px (17%) |

### ระยะ 4: ภาษา + เทสต์

| ทำอะไร | ผล |
|---|---|
| แปล 174 ข้อความเป็นภาษาคน | ไม่มีศัพท์ internal บนหน้าจอ |
| แปล error ที่จุดแสดง | raw text เก็บใน tooltip |
| error_check.py | 39 probes · 25 errors · 25 named · 0 status-only |
| curator_check.py | ทุก rule จับข้อความจริง + spare ข้อความคุย |
| latency_check.py | ไม่ deadlock ใต้ slow client + rapid cycles |
| CURATOR_RULES ↔ RULE_TH สองทิศ | orphaned translation จับได้ |

---

## โครงสร้างไฟล์

```
chat-pool/
├── pool.py              3,225 บรรทัด  core logic
├── pool_server.py       3,817 บรรทัด  HTTP server + dashboard JS
├── ui_check.py            354 บรรทัด  dashboard invariant checker
├── stream_check.py        354 บรรทัด  SSE + input path checker
├── prune_check.py         299 บรรทัด  curator prune checker
├── reconnect_check.py     319 บรรทัด  SSE backoff + reconnect checker
├── error_check.py         379 บรรทัด  error translation checker
├── curator_check.py       118 บรรทัด  CURATOR_RULES regression checker
├── latency_check.py       257 บรรทัด  SSE latency stress checker
├── run_all.py              72 บรรทัด  run all 9 suites
├── SPEC.md              architecture spec
├── USER_GUIDE.md        user manual (Thai)
└── TEST_REPORT.md       this file
```

---

## หมายเหตุ

- ทุกเทสต์ใช้ temp dir ไม่แตะ log จริง (25,273 บรรทัด)
- ทุกเทสต์ cleanup ตัวเอง (ไม่มี file ค้าง)
- ทุก invariant mutation-tested พิสูจน์แล้วว่ากัดจริง
- เซิร์ฟเวอร์จริง (port 8390) ไม่ถูกแตะระหว่างทดสอบ
- `probe` app ใน apps.json เป็นของเดิม (registered 17 กันยายน) ไม่ใช่ของทดสอบ
