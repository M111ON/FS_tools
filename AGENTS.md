# AGENTS.md — กฎความปลอดภัยก่อนลบไฟล์ (อ่านก่อนรันคำสั่งลบทุกครั้ง)

> สร้าง 2026-10-08 หลังสอบสวนเหตุ "โฟลเดอร์หาย เหลือโฟลเดอร์เปล่า" 3-4 ครั้ง
> หลักฐาน: ทดสอบจริงบนเครื่องนี้ (symlink + junction, MSYS bash, Windows cmd)

---

## 1. กฎเหล็ก: ห้ามลบแบบตามเข้าไปใน target

**โฟลเดอร์ในเครื่องนี้เต็มไปด้วย symlink / junction** (pnpm store, bun cache,
opencode plugins, `.vscode`, `I:/Opencode/opencode.db` → `I:/OpenCode-data/` ฯลฯ)

### ❌ คำสั่งที่ "ดูดเนื้อใน target" — ห้ามใช้กับ path ที่อาจเป็น link

```sh
rm -rf path/      # trailing slash  ← shell resolve ตามเข้าไป แล้วลบเนื้อใน target
rm -rf path/*     # glob            ← ลบไฟล์จริงใน target ทั้งหมด
rm -rf path/**    # recursive glob  ← เหมือนกัน
```

ผลคือ **target จริงถูกดูดเนื้อในหมด เหลือโฟลเดอร์เปล่า** และตัว link ก็หายไป
→ นี่คือสาเหตุของ "folder เปล่า" ที่เจอ (ZGLS, POGLS_Vault, C:\geobit)

### ✅ คำสั่งที่ปลอดภัย (ลบแค่ตัว link)

```sh
rm -rf path            # ไม่มี slash/glob → ลบแค่ reparse point
cmd.exe /c rmdir path  # Windows native → ปลอดภัยที่สุด
```

### ตารางผลทดสอบจริง (symlink dir + junction)

| คำสั่ง | ตัว link | target |
|---|---|---|
| `rm -rf link` | หาย | **อยู่ครบ ✅** |
| `cmd /c rmdir link` | หาย | **อยู่ครบ ✅** |
| `find link -delete` | หาย | **อยู่ครบ ✅** |
| `rm -rf link/` | หาย | **ว่าง ❌** |
| `rm -rf link/*` | หาย | **ว่าง ❌** |
| `rm -rf link/**` | หาย | **ว่าง ❌** |

junction ก็โดนเหมือน symlink ทุกกรณี

---

## 2. ก่อนลบโฟลเดอร์ใด ๆ — เช็คก่อนว่าเป็น link ไหม

```sh
# PowerShell (ชัวร์สุด)
powershell -NoProfile -Command "Get-Item '<path>' -Force | Select Name,LinkType,Target"

# cmd (กวาดทั้งโฟลเดอร์)
cmd.exe /c "dir /AL /S <path> 2>nul | findstr /I \"JUNCTION SYMLINK\""
```

- `LinkType` = `SymbolicLink` หรือ `Junction` → **ห้ามใส่ `/` หรือ `*` ต่อท้าย**
- ถ้าไม่ใช่ link (โฟลเดอร์จริง) → `rm -rf` ปกติได้ แต่ยังต้องเช็คว่ามี link ซ้อนข้างใน

---

## 3. หลักฐานที่ต้องรู้: โฟลเดอร์เปล่า = ร่องรอยการถูกดูด

ถ้าเจอโฟลเดอร์ที่มีแค่ `.` และ `..` (ไม่มีไฟล์เลย) ให้สงสัยทันทีว่า:
**เนื้อในถูกดูดออกไปแล้ว** — และมันเกิดขึ้นได้จาก agent ที่รัน destructive command

เหตุการณ์จริง: `C:\geobit\newextension` (จุดเริ่มต้นของระบบ DWGLS/FGLS ทั้งหมด)
ถูกดูดเนื้อในเมื่อ **2026-08-22** โดย opencode agent — บันทึกไว้ใน
`opencode.db` part [707]: *"Was emptied on 2026-08-22 by an opencode agent
running destructive shell commands. Root cause: opencode shell tool has NO
safety guards."*

---

## 4. กฎเพิ่มเติม

1. **ก่อนรัน `rm -rf`, `del /s`, `rmdir /s`, `Remove-Item -Recurse`** —
   ตรวจ path ก่อนเสมอ ว่าไม่ใช่ link และไม่มี link ซ้อนข้างใน
2. **ห้ามรันคำสั่งลบกวาดทั้งไดรฟ์/ทั้งโฟลเดอร์ใหญ่** โดยไม่ระบุ path ชัดเจน
3. **WSL:** อย่าใช้ `rm` บน path ที่ผ่าน `/mnt/` ถ้าไม่แน่ใจ — 9p mount ทำให้
   symlink/junction ถูก resolve ผิดที่ → ลบผิดฝั่ง ใช้ `cmd.exe /c rmdir` แทน
4. **ลบไฟล์ที่สร้างใหม่ได้ + ใหญ่ + งานจบแล้ว** → ได้ แต่เหลือ generator script ไว้
5. **ของเก่า: เก็บ ไม่ทับ ไม่ rename** (policy ประจำของเจ้าของเครื่อง)

---

## 5. ถ้าทำข้อมูลหายแล้ว — ลำดับกู้

1. หยุดเขียนดิสก์ทันที (ยิ่งเขียน ยิ่งทับข้อมูลเดิม)
2. เช็ค `I:/$Recycle.Bin/`, `C:/$Recycle.Bin/` (ถ้าใช้ `del` ธรรมดา)
3. เช็ค `I:/.vault/` (trash_bak ของเจ้าของเครื่อง) + `F:/backup/`
4. เช็ค `I:/OpenCode-data/opencode.db` — ถ้า agent เคยอ่าน/เขียนไฟล์นั้น
   เนื้อไฟล์อาจอยู่ในตาราง `part` (tool read/write) → กู้คืนได้
5. เช็ค git (ถ้าไฟล์เคย track), เช็ค `*.bak`, `*.corrupt-*`

---

## 6. หมายเหตุสำหรับ agent

- เครื่องนี้: Windows + MSYS2 (bash), `make` ไม่มี, gcc 8.1 ที่ `/c/mingw64/bin`
- **path ของ Python/gcc ต้องเป็น Windows path** (`I:\...`) — ส่ง `/i/...`
  แล้วจะไปสร้างที่ `I:\i\...` (เจอจริง)
- ลบไฟล์ temp ที่ตัวเองสร้างเสมอเมื่อจบงาน — อย่าทิ้งไว้ในโฟลเดอร์ของเจ้าของ
