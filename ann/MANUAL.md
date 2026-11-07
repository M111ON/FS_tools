# ANN Memory Layer — คู่มือติดตั้งและใช้งาน

> ระบบ semantic search บน memcore-derived index (~54k entries) สำหรับ memory layer ของ LLM
> (zero-copy model store + streaming). MAP not COMPRESS — ไม่อ่านค่า bytes, ใช้ address/structure เท่านั้น.

---

## 1. Prerequisites

| รายการ | ค่า |
|---|---|
| OS | Windows 10/11 (WDDM) |
| Python | 3.14.x (I:\python3.14.4) |
| CUDA toolkit | 12.6 ที่ I:\cuda_temp (optional — สำหรับ GPU opt-in) |
| Embedder | llama-server + multilingual-e5-small-q8_0-fixed.gguf (port 18095) |
| ANN server | build/ann_search.exe หรือ ann_search_batch.exe (port 8096) |
| Dashboard | dashboard/server.py (port 8391) |

Python deps (ติดตั้งแล้ว):
```
numpy, cupy-cuda12x, onnxruntime, onnx, ml_dtypes, torch (CPU build)
```

---

## 2. ติดตั้ง (ครั้งแรก)

```powershell
# 1) Clone / ไปที่ repo
cd I:\tools

# 2) ตรวจ Python
I:\python3.14.4\python.exe --version

# 3) ตรวจ CUDA (optional, สำหรับ GPU opt-in)
$env:CUDA_PATH = "I:\cuda_temp"
I:\python3.14.4\python.exe -c "import cupy; print(cupy.__version__, cupy.cuda.runtime.getDeviceCount())"

# 4) ตรวจ embedder model
Test-Path I:\model\multilingual-e5-small-q8_0-fixed.gguf
Test-Path "I:\llama\llama-b10830-win-vulkan-x64\llama-server.exe"
```

---

## 3. เริ่มระบบ (ลำดับสำคัญ)

### Step A — Embedder sidecar (port 18095)
```powershell
cd I:\llama\llama-b10830-win-vulkan-x64
.\llama-server.exe -m I:\model\multilingual-e5-small-q8_0-fixed.gguf --embedding --port 18095 -c 512
```
หรือใช้ keepalive:
```powershell
cd I:\tools\ann
I:\python3.14.4\python.exe ann_keepalive.py --once
```

### Step B — ANN server (port 8096)
```powershell
cd I:\tools\ann
$env:ANN_PORT="8096"
$env:LZ_EMBED_HOST="127.0.0.1"; $env:LZ_EMBED_PORT="18095"
$env:ANN_INDEX_PATH="I:\tools\ann\data\memcore\ann_index.jsonl"
$env:ANN_ANCH_PATH="I:\tools\ann\data\memcore\ann_anchors.bin"
$env:ANN_ORDER_PATH="I:\tools\ann\data\memcore\ann_index.order"
$env:ANN_ROUTE_LOG="I:\tools\ann\data\memcore\lz_route.log"
.\build\ann_search_batch.exe --serve 8096
```
หรือ supervisor:
```powershell
I:\python3.14.4\python.exe ann_keepalive.py --once   # auto-start ทั้ง embedder + ANN
```

### Step C — Dashboard (port 8391)
```powershell
cd I:\tools\ann
I:\python3.14.4\python.exe dashboard\server.py --port 8391
# เปิด http://127.0.0.1:8391
```

### Step D — First-run check
```powershell
cd I:\tools\ann
I:\python3.14.4\python.exe first_run.py
# ต้องได้ VERDICT: READY
```

---

## 4. ใช้งาน

### 4.1 Health
```powershell
curl http://127.0.0.1:8096/health
curl http://127.0.0.1:8391/api/health
```

### 4.2 Search (HTTP)
```powershell
$body = @{ query = "DWGLS field contract"; topk = 5 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8096/v1/state/search" -Method POST -Body $body -ContentType "application/json"
```

### 4.3 Search (Python / MCP)
```python
import sys; sys.path.insert(0, r"I:\tools\ann")
import mcp_ann_search as m
s = m._get_gpu_search(); s._ensure_loaded()
r = s.search("DWGLS field contract", topk=5)
print(r["hits"], r["device"], r["footer"])
```

### 4.4 MCP tool (OpenCode)
- `ann_search` — C server path (~54ms/query)
- `ann_search_fast` — resident Python index (CPU ~8ms, GPU opt-in)
- `ann_search_stats` — per-turn closing log
- `ann_health` — health check

### 4.5 Dashboard views
| view | URL |
|---|---|
| Health | http://127.0.0.1:8391 |
| Index | tab Index |
| Candidate | tab Candidate |
| Shadow | tab Shadow |

---

## 5. การตั้งค่า (env vars)

| var | default | ความหมาย |
|---|---|---|
| `ANN_PORT` | 8096 | port ของ ANN server |
| `LZ_EMBED_HOST` / `LZ_EMBED_PORT` | 127.0.0.1 / 18095 | embedder sidecar |
| `ANN_INDEX_PATH` | data/memcore/ann_index.jsonl | path ของ index |
| `ANN_ANCH_PATH` | data/memcore/ann_anchors.bin | anchors |
| `ANN_ORDER_PATH` | data/memcore/ann_index.order | order file |
| `ANN_ROUTE_LOG` | data/memcore/lz_route.log | route log |
| `ANN_USE_GPU` | 0 | ตั้ง 1 เพื่อใช้ cupy (GPU) — ช้ากว่า CPU บน WDDM box |
| `ANN_EMBED_CACHE_MAX` | 512 | embed cache size |
| `ANN_VERSIONS_KEEP` | 5 | จำนวน snapshot ที่เก็บ |
| `MEMCORE_STORE_DB` | fact_store.v2.sqlite3 | source store (per-tenant) |

---

## 6. Index lifecycle

### Sync (store → index)
```powershell
cd I:\tools\cloud_ws\memcore
python ann_sync.py                # sync if store moved
python ann_sync.py --force       # rebuild แม้ signature ตรง
python ann_sync.py --dry-run     # ดู plan ไม่แตะ
python ann_sync.py --no-regrow    # base index only
```

### Versioned snapshots
```powershell
cd I:\tools\ann
python index_versions.py list
python index_versions.py snapshot --label "pre-migration"
python index_versions.py rollback                 # กลับเป็น version ก่อนหน้า
python index_versions.py rollback <VERSION_ID>     # กลับเป็น version ที่ระบุ
python index_versions.py prune --keep 5
```

### Rollback เมื่อมีปัญหา
```powershell
python I:\tools\ann\index_versions.py rollback 20261107T164659_baseline-2026-11-07.jsonl
```

---

## 7. Regression / health gate

```powershell
cd I:\tools\ann
python first_run.py                      # READY/NOT READY
python golden_query_test.py              # 5/5 PASS (exit 0/1)
python golden_query_test.py --budget 100 --json
python dashboard\tests\test_dashboard_core.py   # 42 tests
```

---

## 8. Troubleshooting

| อาการ | สาเหตุ | แก้ |
|---|---|---|
| embedder unreachable | llama-server ไม่รัน | `ann_keepalive.py --once` |
| ANN server unreachable | ann_search ไม่รัน | `ann_keepalive.py --once` |
| 0 entries indexed | index ไม่มี | `ann_sync.py --force` |
| embed latency >50ms | cold conn / sidecar โหลด | ใช้ persistent conn (ทำแล้ว), รอ warm-up |
| GPU ช้ากว่า CPU | WDDM overhead 3.6x | ใช้ default CPU (ANN_USE_GPU=0) |
| index พัง | build/regrow ผิด | `index_versions.py rollback` |
| dashboard 502 | ANN server down | start ANN server ก่อน |

---

## 9. Performance (วัดจริง 2026-11-07)

| path | avg | min | max |
|---|---|---|---|
| CPU (numpy) single query | 8ms | 4 | 9.5 |
| GPU (cupy) single query | 29.6ms | 12.8 | 41.2 |
| embed (persistent conn) | 9.9ms | 6.1 | 238 (cold) |
| total p95 | 40ms | — | — |

**สรุป:** CPU default (เร็วกว่า 3.6x บน WDDM box). GPU opt-in ผ่าน `ANN_USE_GPU=1` (สำหรับ batch/resident multi-pass).

---

## 10. ไฟล์สำคัญ

```
I:\tools\ann\
├── ann_search_gpu.py        # Python resident index (CPU/GPU)
├── mcp_ann_search.py        # MCP facade (stdio)
├── ann_keepalive.py         # supervisor (embedder + ANN)
├── first_run.py             # health contract
├── golden_query_test.py     # regression gate
├── index_versions.py        # snapshots + rollback
├── dashboard\server.py      # dashboard (port 8391)
├── build\ann_search_batch.exe  # C server binary
├── data\memcore\            # index + sidecars
├── versions\                # index snapshots
└── MANUAL.md                # ไฟล์นี้
```

---

*MAP not COMPRESS. Compute-over-storage. Value-blindness preserved.*
PY