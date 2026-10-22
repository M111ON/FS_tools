# Codebase Structure

## Directory Layout

```
chat-pool/                   # Cross-app group-chat pool + DWGLS geometry
├── pool.py                  # Core: append-only log, hash chain, RDH capture, vision, bonds, pointers, blocks, curator
├── pool_server.py           # HTTP server + single-file dashboard (HTML/CSS/JS embedded)
├── tower.py                 # Tower geometry: pack, save/load, sibling, fp_vec8, p4m block view
├── chat_tower.h             # C parity header (tower.py semantics byte-for-byte)
├── ctx_plane.py             # Pointer-based vision over Magic Context's context.db (read-only)
├── pool_cloud.py            # Cloud backup push to Cloudflare Worker
├── pool_wake.py             # Dreamer-style sleep/wake agent runner
├── taskboard.py             # Task layer over blocks: status/owner/parent/holds + open/folded (demo = self-check)
├── tasks_ui.html            # The board page — writable (new/edit/status/tag), filters, folded drawer
├── inbox_bridge.py          # Bridge, both ways: inbox board ↔ pool tasks (plan/apply/pull)
├── taskboard_check.py       # HTTP end-to-end: /task, /task/update, /tasks, /tasks/sync, /tasks/pull, /holds
├── pickup.py                # Session-start handoff reader
├── bond_scale.py            # Bond graph read-cost benchmark
├── bond_real.py             # Derive real bonds from source data relations
├── ui_check.py              # Dashboard JS parse check (node --check) + markup cross-check
├── stream_check.py          # Live SSE end-to-end check (RST, reconnect, backfill)
├── base23_check.py          # 144×144 plane arithmetic assertions
├── curator_check.py         # Verifies every CURATOR_RULES rule catches its intended input
├── error_check.py           # Server error endpoints × dashboard Thai translation cross-check
├── reconnect_check.py       # SSE reconnect cycle + backoff formula verification
├── prune_check.py           # Prune reaches past the map over HTTP
├── latency_check.py         # SSE delivery under high latency (no deadlock)
├── run_all.py               # Runs all 9 test suites, reports combined results
├── reimport_real.py         # Wipe log + re-import real corpus from pool.db / facts / dict sources
├── gpu_fp_vec8.cu           # CUDA fp_vec8 kernel (1 thread/chunk, parity 0/11520 vs CPU)
├── gpu_transpose.cu         # CUDA B-twinview reorder kernel (parity 0/1,658,880 vs CPU)
├── gpu_chain.cu             # CUDA combined fp+transpose (resident, one transfer pair)
├── vulkan_fp.comp           # Vulkan compute shader fp_vec8 (parity 0/92160 vs CPU)
├── SPEC.md                  # Locked specification (tower geometry, proven numbers)
├── BREATHING_SPEC.md        # Breathing FS design spec + vision/pointer/bond decisions
├── USER_GUIDE.md            # End-user guide (Thai)
├── TEST_REPORT.md           # 9-suite test report (Thai)
├── SESSION_REPORT_2026-09-17.md  # Session report
├── log.jsonl                # [runtime] Append-only message log (gitignored)
├── log.jsonl.lock           # [runtime] Process lock file (gitignored)
├── apps.json                # [runtime] Dynamic identity registry (gitignored)
├── .pool_token              # [runtime] Bearer token for LAN auth (gitignored)
├── .flush_state.json        # [runtime] Flush cursor: sha256 + offset (gitignored)
├── .bridge_state.json       # [runtime] Memory bridge cursor (gitignored)
├── .cloud_push_state.json   # [runtime] Cloud push cursor (gitignored)
├── ctx_plane.db             # [runtime] SQLite index cache for memory plane (gitignored)
├── ctx_plane.db.tmp         # [runtime] Temporary index during scan (gitignored)
├── .gitignore               # Runtime data and build artifacts excluded
├── outbox/                  # [runtime] Pending flush payloads (gitignored)
├── towers/                  # [runtime] Tower geometry on disk (gitignored)
└── __pycache__/             # [runtime] Python bytecode cache (gitignored)
```

## Directory Purposes

**chat-pool/:**
- Purpose: Self-contained cross-app group-chat pool with geometric addressing
- Contains: Core pool logic, HTTP server, geometry, GPU kernels, specs, benchmarks
- Key files: `pool.py`, `pool_server.py`, `tower.py`, `SPEC.md`, `BREATHING_SPEC.md`

## Key File Locations

**Entry Points:** `chat-pool/pool_server.py`: HTTP server + dashboard (port 8390); `chat-pool/pool.py`: CLI (view, demo, blocks, block-show, block, session-end, bridge, curate, prune, flush)
**Configuration:** `chat-pool/.pool_token`: LAN bearer token (auto-generated); `chat-pool/apps.json`: identity registry (runtime)
**Core Logic:** `chat-pool/pool.py`: All pool operations (append, tail, derived, bonds, pointers, vision, blocks, curator, prune, bridge)
**Geometry:** `chat-pool/tower.py`: Tower packing and addressing; `chat-pool/chat_tower.h`: C parity header
**Vision over Memory:** `chat-pool/ctx_plane.py`: Pointer reads over context.db (read-only)
**Server API:** `chat-pool/pool_server.py`: HTTP endpoints + embedded dashboard HTML/CSS/JS
**Cloud:** `chat-pool/pool_cloud.py`: Cloudflare Worker backup push
**Agents:** `chat-pool/pool_wake.py`: Scheduled wake runner for opencode agent
**GPU:** `chat-pool/gpu_fp_vec8.cu`, `chat-pool/gpu_transpose.cu`, `chat-pool/gpu_chain.cu`, `chat-pool/vulkan_fp.comp`: Parity-verified GPU kernels
**Benchmarks:** `chat-pool/bond_scale.py`: Bond graph read cost at scale; `chat-pool/bond_real.py`: Derive real bonds from source data relations
**Utilities:** `chat-pool/reimport_real.py`: Wipe log + re-import real corpus (pool.db, facts, dictionary sources) via `pool.append()`
**Checks (12 suites via `run_all.py`):** `chat-pool/ui_check.py`: Dashboard JS + markup cross-check; `chat-pool/stream_check.py`: Live SSE end-to-end; `chat-pool/base23_check.py`: 144×144 plane arithmetic; `chat-pool/curator_check.py`: CURATOR_RULES coverage; `chat-pool/error_check.py`: Server error × dashboard translation; `chat-pool/reconnect_check.py`: SSE reconnect + backoff; `chat-pool/prune_check.py`: Prune past the map; `chat-pool/latency_check.py`: SSE under high latency; `chat-pool/taskboard.py` demo + `chat-pool/taskboard_check.py`: task board (editable, open vocabulary, folded view), card holds, and the two-way inbox bridge over HTTP
**Specs:** `chat-pool/SPEC.md`: Locked tower geometry spec; `chat-pool/BREATHING_SPEC.md`: Breathing FS design + vision decisions

## Naming Conventions

**Files:** `snake_case.py` for Python modules; `snake_case.cu` for CUDA kernels; `snake_case.comp` for Vulkan shaders; `UPPER_CASE.md` for specs and guides
**Directories:** `snake_case/` for runtime output (`outbox/`, `towers/`)
**Functions:** `snake_case()` for Python; `ct_` prefix for C tower functions; `fp_` prefix for fingerprint functions
**Constants:** `UPPER_CASE` for Python module-level constants (LOG, STATE, APPS, DIR, CHUNK, ROW, COL, TSLOT, TESS, FULL)
**Line Kinds:** `chat`, `code`, `bond`, `pointer`, `block` — values of the `kind` field in log.jsonl
**Bond Relations:** `uses`, `produces`, `validates`, `decides`, `follows`, `contradicts`, `unbinds`, `x-<free>`

## Where to Add New Code

**New pool feature:** `chat-pool/pool.py` — add function, add to `demo()` self-check, add HTTP endpoint in `pool_server.py`
**New HTTP endpoint:** `chat-pool/pool_server.py` → `PoolHandler.do_GET` or `do_POST`
**New bond relation:** `chat-pool/pool.py` → `BOND_RELS` tuple (closed set) or `x-<free>` prefix (open-ended)
**New vision level:** `chat-pool/pool.py` → `open_pointer()` — add case for the new level
**New block field:** `chat-pool/pool.py` → `parse_block()` / `render_block()` — update both sides
**New GPU kernel:** `chat-pool/gpu_*.cu` — must prove parity vs CPU in self-check
**New check suite:** `chat-pool/<name>_check.py` — add to `run_all.py` SUITES list
**New task endpoint:** task logic in `chat-pool/taskboard.py`, HTTP in `pool_server.py` (GET /tasks*, /holds; POST /task, /task/update, /tasks/sync, /tasks/pull), the page itself in `chat-pool/tasks_ui.html`; bridge rules live ONLY in `chat-pool/inbox_bridge.py` (plan/apply/pull/holds/status/tasks) and the endpoints call it — never re-implement the sync in the server
**New task status vocabulary:** the pool accepts any bareword `[a-z0-9][a-z0-9_-]{0,23}` in the `[status]` header. The inbox board has only five columns, so `inbox_bridge.POOL_KNOWN` is the boundary: a task parked under a custom status is skipped by BOTH sync directions and reported, never reset to pending. Add a new status there only if the board grows a column for it
**New task UI behaviour:** CSS + JS in `chat-pool/tasks_ui.html` only — `isOpen()` there MUST mirror `taskboard.task_open()`, or `?view=` and the screen will disagree about what is on top
**New benchmark:** `chat-pool/bond_scale.py` — add to `main()` rows or create new script
**Data reimport:** `chat-pool/reimport_real.py` — add new source in `SOURCES` list, each yields `(app, kind, text)` tuples; the script wipes + re-imports via `pool.append()` so rkeys derive naturally
**New spec/decision:** `chat-pool/BREATHING_SPEC.md` or `chat-pool/SPEC.md` — locked decisions only after proof
**New agent integration:** `chat-pool/pool_wake.py` — modify `_parse_target()` or add new app identity
**Tests:** Co-located in each file's `demo()` function — self-check pattern, temp-dir isolated, never touches live data. Cross-file checks go in `*_check.py` files run by `run_all.py`
