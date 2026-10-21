# HANDOFF-2026-10-21-ann-tree-side.md — ผลไม้กับต้น แยกชั้นแล้ว

> **Root:** `I:/tools/ann` (git root `I:/tools`, branch `main`, remote `origin=https://github.com/M111ON/FS_tools.git`)
> **Prior handoff:** `docs/HANDOFF-2026-10-20-ann-isolate.md` (A: perm-sidecar, B: tombstone door)
> **Live ตอนปิดงาน:** `:8096` healthy · entries **62,423** · `routed:1` · `stamps_live 62,423/62,423` · `route_dead:0` · `embed ok` · fog `{replayed:7, shut:7}`
> **Upstream pointer:** `I:/DWGLS-native-fs/docs/HANDOFF-2026-10-21-ann-tree-side-upstream.md`

---

## 0. TL;DR

รอบนี้ไม่ได้แก้ "ประสิทธิภาพ" — มันแก้ **ชั้น**: ป้ายกิ่ง (routing labels) เป็นของ **ต้น** ไม่ใช่ของ **ผลไม้** (vector rows) ตามที่ owner ยืนยันเอง (session 2026-10-10, `DWGLS-native-fs/docs/HANDOFF-2026-10-10-session.md` §5):

> **regrow แตะ vector ไม่ได้** · **capo ย้ายกิ่งไม่มีผลกับของ** (ผลไม้ย้ายที่ได้เหมือนเดิม) · **ผลไม้ของ quadtree ต้นจะหัก ผลไม้ก็ยังเหมือนเดิม เอาไปวางที่ใหม่ได้** · **ปัก tombstone ได้แค่หน้าประตู**

ระหว่างทำ พบว่า **live เสียเงียบมาหลายชั่วโมง**: search ได้ `items_scored: 4` จาก 62,204 แถว ขณะที่ `/health` รายงาน `routed:1` — สุขภาพปลอม

---

## 1. สถาปัตยกรรมที่ลง (ชั้นใหม่)

| ชั้น | เป็นเจ้าของ | ช่องทาง | ห้าม |
|---|---|---|---|
| **ผลไม้** (FRUIT) | vector rows, sid, file, npast | `ann_index.jsonl` | ถือป้ายกิ่ง / ถูก regrow เขียนซ้ำ |
| **ต้น** (TREE) | แต่ละแถวอยู่กิ่งไหน | `<index>.stamp` (8 B/แถว: anchor, anchor2) + `ann_index.order` | ถูก derive จากเวกเตอร์ผลไม้ |
| **ตาราง** (TABLE) | centroid K×dim | `ann_anchors.bin` | จับคู่ order ที่สร้างจากตารางคนละตัว |

`scripts/build_index.py` rebuild ผลไม้โดย**ไม่**เขียน field `anchor` = **ถูกต้อง** ไม่ใช่บั๊ก (ผลไม้ไม่ถือ state ของต้น)

---

## 2. บั๊กที่เจอและแก้ (ทั้งหมดมี test ที่ RED ก่อนแก้)

### 2.1 DEAD ROUTE — live เสียเงียบ (ร้ายที่สุด)
`build_index.py` rebuild ผลไม้ (ไม่มีป้าย) → incremental regrow อ่าน `a[i].anchor` ได้ `-1` ทั้ง prefix → เขียน `(-1,-1)` ทับ sidecar → **ป้ายกิ่งของ 62k แถวตาย** ขณะที่ `/health` บอก `routed:1`

หลักฐาน: sidecar มี 23 แถวที่ route จริง = 62227 − 62204 พอดี (แถวที่เพิ่มหลัง rebuild)

**แก้**: incremental อ่านป้าย prefix จาก **sidecar** (ไม่ใช่จากผลไม้) · ถ้าไม่มีป้าย → **fail loud** `mode:"no_tree"` rc=-3 (ไม่ไปคำนวณ 62k เวกเตอร์เอง)

### 2.2 FULL REGROW ซ่อมไม่ได้
full path เขียนป้ายใหม่ลง jsonl แต่ **sidecar เก่าทับ** (overlay ชนะเมื่อขนาด == n*8) → ผลคือ full regrow "ซ่อม" ได้แค่ 4 แถวจาก 404

### 2.3 FAST PATH "unchanged" ไม่เช็คต้น
stat (ผลไม้) + checksum (ตาราง) ผ่าน แต่ป้ายกิ่งหาย → รายงานว่า "ไม่เปลี่ยน" → ทุกอย่างนิ่งทั้งที่ตาย

### 2.4 APPEND WINDOW — ช่องที่ป้ายหายทั้งตาราง
read path overlay เฉพาะเมื่อ sidecar ขนาด == n*8 เป๊ะ → ช่วงที่ `ann-sync` append แต่ยังไม่ regrow (sidecar สั้นกว่า index) → ป้ายทั้งหมดถูกทิ้ง (วัดสด: `stamps_live 0` จาก 62,323 ทั้งที่ป้ายอยู่บนดิสก์)

### 2.5 DOOR: claim "recall cannot drop" ไม่จริง
door แทนที่ fallback ที่สแกน**ทุกแถว** (`cap = nent`) แต่เองเก็บ `cap = budget` (24k) → สแกนน้อยกว่าที่แทนที่ → **วัดกับ numpy oracle 300 คิวรีจริง: recall@5 ON=0.9307 vs OFF=1.0000, 56/300 คิวรีแย่กว่า**

### 2.6 บั๊ก upstream ที่เจอระหว่าง review diff ตัวเอง
`d48bc08` มี `pseen2 = pseen2` (self-assign) → leak buffer ทุก door query → แก้เป็น `pseen = pseen2`

---

## 3. สิ่งที่ลง (interface ใหม่ — additive ทั้งหมด)

| ของ | รายละเอียด |
|---|---|
| `POST /v1/state/restamp` | ซ่อมป้ายกิ่งโดย**ไม่แตะผลไม้** (jsonl md5 + mtime เหมือนเดิม) — re-route ทุกแถวกับ centroid เดิม → เขียนลง sidecar → เขียน order ใหม่. `rc:0`=ซ่อมแล้ว, `-1`=ไม่มีตารางให้ใช้ (ให้ full regrow), `-2`=alloc fail |
| `GET /health` | เพิ่ม `stamps_live` (แถวที่มีป้ายจริง) + `route_dead` (1 = ป้ายตายเกิน 50% → dead route). `status` ไม่เปลี่ยน (keepalive อิง `status`) |
| `route.mode` | เพิ่ม `dead-route` (ป้ายตาย → เดินทุกแถวแบบ exact แทนที่จะเสิร์ญไม่กี่แถวแอบเป็น top-k) |
| `regrow.mode` | เพิ่ม `no_tree` (ป้ายกิ่งหาย → ล้มเหลวเสียงดัง ไม่ซ่อมเอง) · `race` (มีอยู่แล้วจาก `1e1e342`) |
| env | `ANN_DOOR` (default **OFF**), `ANN_DOOR_EXIT` (default OFF), `ANN_DOOR_EXIT_FLOOR` (แยกจาก trigger floor) |

---

## 4. ผลวัด (ตัวเลขจริง)

### 4.1 Live หลังซ่อม
```
health: entries 62,423 | stamps_live 62,423/62,423 | route_dead:0 | routed:1
search: mode=anchor+p  items_scored=24,000 จาก 62,323  score ~0.86
ผลไม้: md5 327b9612efde… (ก่อนและหลัง regrow เหมือนกันเป๊ะ) · ป้ายในผลไม้ = 0
```

### 4.2 `ann_sync.py --force` จริง (rebuild ผลไม้ → regrow ซ่อมต้น)
```
07:39:43  built base index (28.1 s) from store 62423:62423:62423
07:41:08  regrow ok (anchor, nent=62423 K=8) — index in sync
ผล: stamps_live 62,423/62,423 · route_dead 0 · ผลไม้ md5 เหมือนเดิม
```

### 4.3 A/B ของ door (numpy oracle, live corpus, floor บังคับ 2.1)
| | OFF (anchor+full) | ON (door) |
|---|---|---|
| recall@5 | **1.0000** | **1.0000** |
| recall@10 | **0.9997** | **0.9997** |
| p50 ms | 39.2 | 43.0 |
| items_scored | 62,266 | 62,266 |
| paired | — | 300/300 เท่ากัน, 0 แย่กว่า |

→ door **recall-neutral** หลังแก้ แต่**ช้ากว่า** และที่ floor ผลิต (0.5) คิวรีจริงได้ ~0.86 → มัน**แทบไม่เคยถูกเรียก**บน :8096 ปกติ → **ship OFF** (default ใน `ann_keepalive.py` = "0")

---

## 5. ไฟล์ที่เปลี่ยน

```
ann/src/ann_search.c        tree-side separation + dead-route fail-loud + restamp
                             + door recall contract (cap=nent) + door early exit
                             + graft hard fallback (cap=nent) + truthful /health
                             + the upstream self-assign fix
ann/ann_keepalive.py        ANN_DOOR=0 / ANN_DOOR_EXIT=0 (ship OFF, พร้อมเหตุผล)
                             + WAIT_HEALTHY 180 + port check + STARTUP_GRACE
ann/ann_route_watch.py      heal H2 (dead route) -> restamp -> fallback full regrow
ann/tests/test_stamp_preserve.py   ใหม่ — 9 กลุ่ม (S1-S9)
ann/tests/ab_door_live.py          ใหม่ — A/B กับ numpy oracle (ต้องใช้ ANN_TEST_EXE)
ann/tests/ab_graft_fallback.py     ใหม่ — A/B ของ graft hard fallback
ann/tests/probe_door_exit.py      ใหม่ — พิสูจน์ว่า early exit เข้าถึงได้จริง
cloud_ws/memcore/ann_sync.py      rebuild แล้วต้อง regrow เสมอ (rebuilt flag)
```

---

## 6. TESTS

| ชุด | ผล |
|---|---|
| `run_tests.py` (project suite) | **ALL PASS** |
| `tests/test_stamp_preserve.py` | S1–S9 ALL BEHAVED (9 กลุ่ม) |
| `tests/test_door_path.py` | ALL BEHAVED |
| `tests/test_perm_isolate.py` | ALL BEHAVED |
| `tests/test_incr_fix.py` | ALL BEHAVED |
| `tests/probe_door_exit.py` | ALL BEHAVED (exit ตัดที่ 1 แถวเมื่อ floor ต่ำ) |
| `tests/ab_door_live.py --queries 300` | recall-neutral, 300/300 เท่ากัน |
| `tests/ab_graft_fallback.py` | recall-neutral (0.4167 = 0.4167, 0/12 แย่กว่า) · `mode=graft+full` ยืนยันว่า branch ทำงาน |
| architecture check (skill) | ALL CHECKS BEHAVED |

**S9 (ใหม่)**: ถ้าผลไม้ถูก **สลับลำดับแถว** ป้ายกิ่ง (ซึ่งผูกกับตำแหน่งแถว) จะชี้ผิดแถวแบบเงียบ → append contract ต้องจับได้ (boundary sid ไม่ตรง → ไม่ใช่ incremental → full path วางใหม่ทุกแถว) และผลไม้ต้องไม่ถูกเขียนซ้ำ — ผ่าน

---

## 7. ⚠️ LANDMINES (เจอจริงรอบนี้)

1. **scratch ต้องมีต้นทั้งชุดใน dir เดียว** — index + anchors + order + state + sidecar ปนกันคนละ dir → server ไม่มี anchors → ไม่มี route → brute → **door ไม่มีทางเปิด** และทุกข้อสรุปว่า "ฟีเจอร์พัง" ผิดทั้งหมด (เจอ 1 ครั้ง ใช้เวลา debug นาน)
2. **harness ที่รายงาน recall 0.0000 ทั้งสองข้าง = ผ่านลวง** — 2 สาเหตุเงียบ: oracle ครอบไม่ครบ corpus ที่ server ค้น, หรือสองข้างอ่านคนละ corpus → ต้อง assert recall ไม่เป็นศูนย์ก่อนเชื่อคำว่า "recall-neutral"
3. **`/health` เขียวได้ทั้งที่ route ตาย** — ตาราง+order ถูกแค่พิสูจน์ว่า TABLE สมบูรณ์ ไม่ได้พิสูจน์ว่าแถวมีป้าย → health ต้องรายงาน liveness ของป้ายแยก
4. **fast path ห้ามรายงาน "unchanged" เหนือสถานะที่มันไม่ได้ตรวจ**
5. **path ที่มาแทน path เดิม ต้องได้ผลไม่น้อยกว่าของเดิม** — door ไปแทน full scan แต่เก็บ cap ต่างกัน → เทียบ**work set** ไม่ใช่ label
6. **บอกว่า config ไหน deploy อยู่จริง** — keepalive แก้ไฟล์ = เปลี่ยนตอน start ถัดไป แต่ process ที่รันอยู่ยังใช้ env เก่าจนกว่าจะ restart → เทียบ mtime ของไฟล์ keepalive กับเวลาเริ่มของ process
7. **port 8097 ค้างจากรอบก่อน** → ทุก test ที่ bind 8097 จะ fail ติด ๆ → ฆ่า stray ก่อนรัน (kill by PID หลัง tasklist)
8. `env | grep ^ANN_` แล้ว unset ก่อนขับ test (export ค้างทำให้ test ไป start server ผิดพอร์ตแล้ว poll ไม่จบ)
9. gcc เขียนทับ exe ที่ยังรันอยู่ → Permission denied → build เป็น `*_new.exe` แล้ว swap หลัง kill
10. embed sidecar ตอบ 500 เมื่อข้อความหลุด vocabulary → NFKC + residue map 12 ตัว แล้ว fallback ทีละอัน
11. `make` ไม่อยู่บน PATH → ใช้ `python run_tests.py`; ส่วน python ต้องใช้ `I:/python3.14.4/python.exe` (bare `python` ไม่มี numpy)

---

## 8. กฎที่ได้ (owner-approved architecture กลายเป็นโค้ด)

1. **regrow ไม่เขียนผลไม้** — ป้ายไปที่ช่องทางของต้น (sidecar) jsonl byte-identical
2. **incremental อ่านป้าย ไม่ derive ใหม่** — อ่าน sidecar = การกระทำของต้น; ถ้าไม่มีป้าย → fail loud
3. **มีแค่แถวใหม่เท่านั้นที่ถูก route** — จุดเดียวที่อ่านเวกเตอร์เพื่อวาง
4. **overlay sidecar ครอบทุกแถวที่ครอบ** — ไม่ใช่ต้องขนาด == n*8
5. **fast path ต้องพิสูจน์ทุก artifact ที่งานที่ข้ามจะเขียน**
6. **พังเป็นกล่อง ไม่ใช่ทั้งหมด** — dead route → `route_dead` + full scan ไม่ใช่เสิร์ญไม่กี่แถวแอบเป็น top-k
7. **door แทนที่ fallback ต้องได้ผลไม่น้อยกว่า** — วัดกับ oracle ภายนอก ไม่ใช่ OFF==ON

---

## 9. OPEN ITEMS (owned, ไม่ซ่อน)

### ปิดแล้วในรอบนี้ (2026-10-21 ต่อ)

1. **`ANN_DOOR_EXIT` วัดแล้ว** — เป็น **trade จริง ไม่ใช่ของฟรี**: recall@5 ตกจาก 0.9220 → **0.6570** (p95 ดีขึ้น 76 → 40 ms). เก็บไว้เป็น opt-in (default OFF) พร้อมเอกสารผล — ผู้ใช้ต้องยอมรับการเสีย recall
2. **graft-as-hard-fallback สร้างแล้ว** (env `ANN_GRAFT_FALLBACK=1`, default OFF) — ตอน below-floor fallback ทำงาน กราฟจะเสนอลำดับเดิน (รายการที่ชี้ไฟล์เดียวกันก่อน) แล้วเดินที่เหลือตาม → วัดกับ numpy oracle: **recall-neutral** (0.4167 = 0.4167, 0/12 แย่กว่า) · ต้องแก้ `cap = nent` ก่อน (ครั้งแรก cap=budget ทำให้ recall ตก 0.2833)
3. **scheduled task `\ann-route-watch`** — มีอยู่แล้วและทำงาน (`Last Result: 0`, ทุก 5 นาที) — พบว่า trigger เดิมใช้ `StartBoundary` เป็นวันที่ผ่านมาแล้ว (แสดง "Next Run 21/10/69" = พ.ศ. 2569 ไม่ใช่ปี 2069) → ไม่ต้องแก้
4. **tasks `memory-daemon` / `hermes-mc-sync`** — เปิดอยู่แล้วและทำงาน (`Last Result: 0`)

### ยังค้าง (เปิดหมด)

5. **disk เต็ม** — `I:` เหลือ 0.55 GB จาก 111 GB (`ann_sync` ล้มด้วย disk guard ที่ 600 MB) ต้องจอดของเก่าลง drive อื่นหรือเพิ่มพื้นที่; รอบนี้จอดไฟล์ backup 4 ตัว (1.3 GB) ไปที่ `F:/parked/2026-10-21-ann-tree-side/` แล้ว
6. **keepalive เพิ่มเวลารอ + เช็ค port ก่อนเปิด instance ที่สอง** — index 62k แถวโหลด 31 วินาที (health wait รวม 57 วินาที) ทำให้ budget 60 วินาทีล้มเหลวที่ขอบ → เปิด instance ซ้อน (instance ที่สอง bind ไม่ได้) → แก้: `WAIT_HEALTHY` 60 → **180** + ตรวจ port ก่อน + `STARTUP_GRACE`
7. **`/v1/admin/compact`** (ล้าง jsonl ป้ายเป็น admin action) — deferred (full regrow ครอบคลุมแล้ว และตอนนี้ไม่เขียน jsonl อยู่แล้ว)
8. **S2 (int8 pack + per-bucket scale) / S3 (contiguous reorder)** ยัง parked จากแผน 2026-10-14
9. **ผลไม้ที่ถูกสลับลำดับแถว** → ป้ายผูกกับตำแหน่งแถว → ต้อง re-place ทั้งต้น (full path ทำให้) — ถ้าอยากรันจริงต้อง force regrow

---

## 10. REPRODUCE

```sh
cd I:/tools/ann
I:/python3.14.4/python.exe run_tests.py            # project suite
I:/python3.14.4/python.exe tests/test_stamp_preserve.py
I:/python3.14.4/python.exe tests/test_door_path.py
I:/python3.14.4/python.exe tests/probe_door_exit.py
ANN_TEST_EXE="I:\tools\ann\build\ann_search_batch.exe" \
  I:/python3.14.4/python.exe tests/ab_door_live.py --queries 300 --floor 2.1
```

Architecture check (ฝั่ง skill, ขับ binary ที่ deploy + live + scratch):
```sh
I:/python3.14.4/python.exe I:/Hermes/skills/geometric-compression/ann-tree-side-separation/scripts/verify_ann_tree_side.py
```

---

*Session 2026-10-21 · `I:/tools/ann` (git root `I:/tools`) · tree-side separation + dead-route fail-loud + restamp + door recall contract*
