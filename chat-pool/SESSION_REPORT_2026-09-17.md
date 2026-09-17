# chat-pool — Session Report 2026-09-17

งานทั้งหมดของ session นี้ — สถาปัตยกรรมสุดท้าย, commits, งานระบบ,
และสิ่งที่รอการตัดสินใจ (ref: BREATHING_SPEC.md §5)

---

## 1. สถาปัตยกรรมสุดท้าย

```
┌────────────────── ผู้ส่ง (ทุกแอป + มนุษย์) ──────────────────┐
│ human (dashboard) · hermes · codex · opencode · agent ใหม่ ๆ  │
│   HTTP  POST :8390/append {app, kind, text, to?}  @name = tag │
│   MCP   pool_send / pool_register / pool_inbox (session-pool) │
│   Python  pool.append(app, kind, text, to=)                   │
│   ⚠ ต้อง register ก่อน (POST /register) — ไม่งั้น 403          │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌────────────────── DATA (นิ่งตลอด, กฎเหล็ก) ───────────────────┐
│ log.jsonl  — append-only v3 lines ผูก hash chain              │
│   {v:3, t, app, kind, text, to, tags, prev, rkey, hash}       │
│   lock: msvcrt/fcntl บน log.jsonl.lock → concurrent ไม่ fork   │
└──────────────────────────┬────────────────────────────────────┘
                           ▼ อ่าน (multi-pointer — เกิดที่จุดอ่าน, ไม่มี state)
┌────────────────── READ ───────────────────────────────────────┐
│ GET /tail?n=&as=          — raw lines (filter visibility)     │
│ GET /view?n=&cloud=&as=   — rendered chat log + chain audit   │
│ GET /neighbors?rkey=      — vision ที่จุดเดียว (breathing)     │
│ GET /inbox?app=&mark=     — mention+DM ที่ยังไม่อ่าน           │
│ GET /apps                 — registry + liveness               │
│ pool.py view [--cloud]    — CLI อ่านเดียวกัน                  │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌────────────────── CONSUMERS ──────────────────────────────────┐
│ dashboard :8390  — feed+search+filter+composer+registry+inbox │
│ poolNotify.js    — opencode plugin: idle → inbox เด้งเข้า      │
│                    session เป็น user message (20s poll)        │
│ pool_wake.py     — schtask 15 นาที: มีงาน → opencode run       │
│                    one-shot ใน root ที่ mention ระบุ; ว่าง→sleep│
│ flush (ทุกชม.)   — outbox → pool.db (session-pool MCP ค้นได้)  │
│                  + memory_bridge → magic context (HANDOFF/     │
│                    DECISION กลายเป็น memory อัตโนมัติ)          │
└────────────────────────────────────────────────────────────────┘
```

## 2. Commits (14 บน main — รอ push ทั้งหมด)

| commit | เรื่อง |
|---|---|
| `a8b08f8` | push_outbox ส่ง chat log + RAW footer (ถูกแทนที่ใน 9a99db9) |
| `9a99db9` | `pool.py view` — minimal chat log, เก็บ raw |
| `aa7eba9` | append lock (msvcrt/fcntl) กัน race ข้าม process |
| `f647c86` | pool_server dashboard + ThreadingHTTPServer |
| `52cd007` | v3 directed messages (`to=`) + visibility |
| `c2cc8be` | registry + @mentions inbox (feed model) |
| `8d229f9` | sidebar กระชับ (dropdowns) |
| `69cdbcb` | pool_wake sleep/wake + schtask 15 นาที |
| `594eb87` | living memory bridge → magic context |
| `cbe4ca1` | neighbors(rkey, vision) |
| `4de5431` | neighbors ตัด ring — อ่านตรงจุดเท่านั้น |
| `56b0853` | BREATHING_SPEC.md v0.1 |
| `037363e` | fix: demo ไม่ wipe registry อีก |
| `1bbb0b7` | /apps + registry panel |
| `782c9c1` | heartbeats จาก task เดิม + human identity |

Untracked state (ไม่ commit โดยเจตนา — runtime data): `apps.json`,
`log.jsonl.lock`, `.bridge_state.json` → ควรเพิ่ม .gitignore

## 3. งานระบบที่กำลังทำงานอยู่

| ตัว | รอบ | หน้าที่ | สถานะ |
|---|---|---|---|
| pool_server (PID ดู /health) | ตลอด | HTTP + dashboard :8390 | รัน manual — restart เองถ้าบูตเครื่อง |
| schtasks `chat-pool-flush` | ทุก 1 ชม. | flush → pool.db + memory bridge | Ready, next 10:10 |
| schtasks `chat-pool-wake` | ทุก 15 นาที | inbox มีงาน → opencode run | Ready, Interactive only |
| poolNotify.js (opencode) | idle +20s | inbox → user message ใน session | ต้อง restart OpenCode 1 ครั้ง |

Registry ปัจจุบัน: human, opencode-agent, flusher (live) · hermes,
codex, dashboard (idle)

## 4. สิ่งที่พิสูจน์แล้ว (ทดสอบจริง)

- concurrent append ข้าม process ไม่ fork chain (lock + demo)
- mention `@codex` จาก hermes → codex inbox เห็น / แอปอื่นไม่เห็น
- DM `to=hermes` → เห็นเฉพาะ hermes+ผู้ส่ง+admin
- bridge ยก DECISION/HANDOFF เข้า context.db (#953/#954) — อ่านย้อนได้
- wake runner peek ได้ (1 pending ตอนทดสอบ)
- dashboard: SSE feed, registry, inbox, composer, cloud toggle

## 5. รอตัดสินใจ (BREATHING_SPEC.md §5)

1. **vision levels** — ตอนนี้ระดับเดียว (เซลล์เดียว) พอหรือต้องมีหลายระดับ
2. **bond line format** — ความสัมพันธ์จริงระหว่าง hash เขียนเป็น line ยังไง
3. **pointer per-agent** — แยกสุดหรือมีมุมมองร่วม
4. **RDH 144 พอไหม** — 20,736 เซลล์สำหรับ FS จริง หรือ stack หลายชั้น
5. (เพิ่มจาก session นี้) **รวม wake+flush เป็น task เดียว?** — ลด schtasks
   เหลือตัวเดียว ทุก 15 นาที (flush เมื่อครบชั่วโมง)
6. (เพิ่ม) **authority ทางคืน** — ถ้า magic context อัปเกรดเป็น Rust-authority
   write ตรงจะ fail ต้องเปลี่ยน bridge ไปทาง ctx-memory/MCP

## 6. ทางไปต่อ (จัดลำดับแล้ว)

1. push commits (รีวิวรวมก่อน) + .gitignore state files
2. ตัดสินใจ spec §5 ข้อ 1-2 (ก่อนแตะ DB)
3. prototype ทาง A: rkey column + read_at บน context.db (read-only ทดลอง)
4. ทดสอบ agent-to-agent วงจรเต็ม: human → hermes → codex → opencode
5. auto-start pool_server ตอนบูต (ยัง manual)
