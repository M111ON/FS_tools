# vault export — agent notes (เปิดเมื่อต้องใช้ ไม่ inject ทุกเทิร์น)

- task **`vault-export`** รันวันละครั้ง **04:30** → `I:\tools\opencode-export\vault_export_daily.cmd`
  (3 ขั้น incremental: `export_sessions.py` → `export_session_md.ps1 -Incremental` → `clean_sessions.py`)
- log: `I:\.vault\vault_export.log` — ไม่มีอะไรใหม่ ~20 วิ · มีของใหม่ไม่กี่นาที
- **คนละทางกับ memcore**: memory-daemon export ลง `memcore\.opencode_export` (สำหรับค้นคืน) — vault คือ **md ที่คนเปิดอ่าน** (`I:\.vault\exported_session`)
- md ตั้งชื่อ `session-<short 11 ตัวแรกของ id>.md`; ถ้า 11 ตัวชนกัน ตัวที่แพ้ได้ชื่อเต็ม `session-<full id>.md`
  **ห้ามแก้กลับเป็น 11 ตัวล้วน** — เคยทับกันจริง 34 session
- `clean_sessions.py` ข้ามไฟล์ที่ md ไม่ใหม่กว่า output; `--force` = แปลงใหม่หมด
- ติด/ถอด task: `I:\tools\opencode-export\install_vault_export_task.cmd [HH:MM]`
