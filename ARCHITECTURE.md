# Architecture

## Pattern Overview

**Overall:** Append-only log with spatial addressing and pointer-based vision

**Key Characteristics:**
- Data is append-only JSONL with a sha256 hash chain for integrity
- Every line receives a spatial address (`rkey`) at capture time via RDH capture — address is derived from content, never stored separately
- Vision (reads) is pointer-based: a pointer opens at one cell and dies when the read ends; no server-side pointer state exists
- Bonds (edges between lines) are ordinary lines with `kind:"bond"` — same chain, same address space, same append rules
- The 144×144 spatial plane (20,736 cells) is the single coordinate system shared by chat lines, bonds, pointers, and cross-system memory links
- Stacking (overflow handling) uses phase axes on the same plane, not larger planes

## Layers

**Pool Core (`pool.py`):**
- Purpose: Append-only log, hash chain, RDH capture, vision reads, bond graph, pointer/sighting system, block system, incremental derived index, curator noise filter + pruning, memory bridge to Magic Context
- Location: `chat-pool/pool.py`
- Contains: `append()`, `tail()`, `derived()`, `all_bonds()`, `open_pointer()`, `sighting()`, `sighting_once()` (idempotent — read that re-renders does not write a second line), `pointers_at()`, `memory_bridge()`, `rkey()`, `app_rkey()`, `app_phase()`, `bond()`, `unbind()`, `bonds_for()`, `neighbors()`, `timeline()`, `stamp_seq()` (each line's append ordinal = its tick), `sub_map()`, `plane_meta()` (per-cell last writer + last tick), `view()`, `pretty()`, `inbox()`, `register_app()`, `block()`, `blocks()`, `block_revs()`, `block_get()`, `verify_block()`, `block_timeline()`, `block_card()`, `parse_block()`, `render_block()`, `session_end()`, `pool_session_guard()`, `motif()`, `reach()`, `explore_cell()`, `pattern_census()`, `outbox_list()`, `index_pieces()` (normalize msgs for embedding: strip envelope, drop machine kinds, split to ~16 KB), `push_outbox()`, `flush_emit()`, `flush_check()`, `_iter_lines()` (streaming reverse reader), `_derived_absorb()` (incremental index update), `curate()`, `curator_verdicts()`, `curator_report()`, `curator_prune_report()`, `curator_hits()`, `curator_protect()`, `plane_prune_stats()`, `stamp_cur()` (the noise filter, display-edge only), `demo()` (self-check, runs in temp dir)
- Depends on: Python stdlib only (hashlib, json, os, re, sys, time)
- Used by: `pool_server.py`, `pool_cloud.py`, `pool_wake.py`, `ctx_plane.py`, `pickup.py`, `tower.py`, `reimport_real.py`

**HTTP Server + Dashboard (`pool_server.py`):**
- Purpose: stdlib HTTP server wrapping pool.py; serves the single-file dashboard (HTML+CSS+JS embedded in `DASHBOARD` string) and all API endpoints
- Location: `chat-pool/pool_server.py`
- Contains: `PoolServer(ThreadingHTTPServer)` (threaded server, classifies closed-client socket errors as normal traffic), `PoolHandler(BaseHTTPRequestHandler)` (request routing), `DASHBOARD` (inline HTML/CSS/JS), SSE streaming, chain audit, QR code generation for LAN auth
- Depends on: `pool.py`, Python stdlib `http.server`
- Used by: Dashboard browser clients, `pool_wake.py`, mobile clients

**Tower Geometry (`tower.py`, `chat_tower.h`):**
- Purpose: Geometric packing of 144B chunks into tesseract towers (1152 slots = 32×36), 18 towers fill the full 20,736 address space. Provides fingerprinting (fp_vec8), ticker (enc), sibling involution, p4m block view
- Location: `chat-pool/tower.py`, `chat-pool/chat_tower.h`
- Contains: `pack()`, `save()`, `load()`, `sibling()`, `fp_vec8()`, `enc()`, `quadrants()`, `block_of()`, `demo()`
- Depends on: Python stdlib only (json, os)
- Used by: `pool.py` (rkey function is ported from RDH), spec verification

**Context Plane (`ctx_plane.py`):**
- Purpose: Pointer-based vision over Magic Context's SQLite `context.db` — same 144×144 plane as pool, read-only, index is a cache (addresses derived from content, content stays in context.db)
- Location: `chat-pool/ctx_plane.py`
- Contains: `scan()`, `read_at()`, `links()`, `stats()`, `cross_stats()`, `demo()`
- Depends on: `pool.py` (rkey, axis, budget, block_of), `context.db` (read-only)
- Used by: `pool_server.py` (`/ctx/*` endpoints)

**Cloud Push (`pool_cloud.py`):**
- Purpose: Best-effort mirror of log.jsonl to a Cloudflare Worker (cloud-memory-worker). Local-first + cloud backup only
- Location: `chat-pool/pool_cloud.py`
- Contains: `push()`, `status()`
- Depends on: `pool.py`, Cloudflare Worker API
- Used by: Hourly flush task

**Wake Runner (`pool_wake.py`):**
- Purpose: Dreamer-style sleep/wake agent — scheduled task peeks pool inbox, spawns `opencode run` one-shot when work is pending
- Location: `chat-pool/pool_wake.py`
- Contains: `main()`, `peek()`, `_parse_target()`
- Depends on: Pool HTTP API (`localhost:8390`), `opencode` CLI
- Used by: Windows scheduled task (every 15 min)

**GPU Kernels:**
- Purpose: CUDA/Vulkan parity implementations of fp_vec8 and transpose — proven byte-identical to CPU
- Location: `chat-pool/gpu_fp_vec8.cu`, `chat-pool/gpu_transpose.cu`, `chat-pool/gpu_chain.cu`, `chat-pool/vulkan_fp.comp`
- Contains: fp_vec8 kernel (1 thread/chunk), transpose kernel (A 32×36 → B 36×32), combined chain kernel, Vulkan compute shader
- Depends on: CUDA runtime / Vulkan SDK
- Used by: Benchmark/parity verification

**Benchmarks and Checks:**
- Purpose: Performance measurement and correctness verification — 9 suites run via `run_all.py`
- Location: `chat-pool/bond_scale.py`, `chat-pool/ui_check.py`, `chat-pool/stream_check.py`, `chat-pool/base23_check.py`, `chat-pool/curator_check.py`, `chat-pool/error_check.py`, `chat-pool/reconnect_check.py`, `chat-pool/prune_check.py`, `chat-pool/latency_check.py`, `chat-pool/run_all.py`
- Contains: `bond_scale.py` — measures bond graph read cost at scale; `ui_check.py` — parses dashboard JS via `node --check` AND cross-checks every `$('#id')` against the markup (a missing element throws at load and silently unbinds every handler after it); `stream_check.py` — live end-to-end check of `/stream`: real server on an ephemeral port over a throwaway log, real SSE socket, real RST mid-stream (no-backfill, live-past-window, survives disconnect without a traceback, reconnects); `base23_check.py` — 55 assertions on the numbers the 144×144 plane is built out of (see "Field Arithmetic" below); `curator_check.py` — verifies every CURATOR_RULES rule catches its intended input and spares real text; `error_check.py` — drives every server endpoint with malformed input, cross-checks live (status, error) pairs against source literals and dashboard Thai translations; `reconnect_check.py` — proves SSE reconnect cycle end-to-end plus verifies the backoff formula (3→6→12→24s cap); `prune_check.py` — builds a sandbox with noise, verifies prune reaches past the map (dangling bonds/sightings exposed with `?raw=1`); `latency_check.py` — proves SSE delivery works under high latency (slow client does not block the server); `run_all.py` — runs all 9 suites, reports combined results (exit 0 = all green)
- Depends on: `pool.py`, `pool_server.py`

**Field Arithmetic (why the plane is 144×144 = 20736):**
- Purpose: separate *forced* structure from coincidence in the base-2/base-3/base-12 story, so the geometry is not taken on faith
- Location: `chat-pool/base23_check.py` (stdlib only, no pool state touched, ~1 s)
- Verified: 12 = lcm(4, 6) is the smallest n whose ring carries both a 4-fold and a 6-fold rotation, and n = 12 is the first n > 4 with `disc(Q(ζ_n)) = n²`; `ζ12 = i(1−ω)` exactly and `Z[i,ω] = Z[ζ12]` with a unimodular change of basis, so the two worlds generate one maximal order; its metric form is `2A₂ ⊥ 2A₂` with determinant 12 per plane, so the box is 12 × 12 = 144 (positive definite, rank 4); `12^{φ(12)} = 12⁴ = 20736` and `20736/(2⁴·3²) = 144 = 12²`
- Also checked: square vs triangular lattices are incommensurable (`Z[i] ∩ Z[ω] = Z`), so no 2D cell arrangement can close this box — the joint closure is rank 4; the rhombille grid is the unit cube projected along (1,1,1) (8 vertices → 7 points, 12 edges → 12 equal segments in exactly 3 directions); FCC's 12-vector shell splits as 3×4 and 4×3
- Used by: Development verification

## Data Flow

**Message Append:**
1. Client sends `POST /append {app, kind, text, to}` — `pool_server.py`
2. `pool.append()` acquires process lock (msvcrt on Windows, fcntl on Unix) — `pool.py`
3. Compute `prev` hash (last line's hash or "GENESIS") — `pool.py`
4. Compute `rkey = rkey(app+kind+text)` via RDH capture — `pool.py`
5. Compute `hash = sha256(prev+app+kind+to+text)` — `pool.py`
6. Atomic `os.write` + `os.fsync` to `log.jsonl` — `pool.py`
7. Feed into incremental derived index (`_derived_absorb`) — `pool.py`

**Hourly Flush + Cloud Push:**
1. Scheduled task runs `python pool.py` (no args) or `POST /flush` — `pool_server.py`
2. `flush_emit()` checks sha256 of log vs `.flush_state.json`; new lines → `outbox/<ts>.json` — `pool.py`
3. Default CLI path also runs `memory_bridge()` (lift handoffs/decisions into Magic Context) — `pool.py`
4. `push_outbox()` calls `hermes_pool_cli.py embed` to push to pool.db — `pool.py`
5. `pool_cloud.py push()` mirrors new lines to Cloudflare Worker (best-effort, never blocks flush) — `pool_cloud.py`

**Memory Bridge (pool → Magic Context):**
1. `memory_bridge()` scans pool lines newer than bridge cursor — `pool.py`
2. Lines starting with DECISION/HANDOFF/SESSION/RECOVERY/VERDICT are lifted — `pool.py`
3. Inserted into `context.db` memories table with `project_path = "git:174753f..."` — `pool.py`
4. Bridge cursor saved to `.bridge_state.json` — `pool.py`

**Live Feed:**
1. Client loads the window with `GET /tail?n=200` — each line already stamped with its tick — and renders it as tick chunks — `pool_server.py`
2. Client opens `GET /stream?n=200` (SSE) — `pool_server.py`
3. Server starts its cursor at the newest line at connect time and sends only lines newer than that cursor (hash-compared, so it survives the capped window) — `pool_server.py`
4. Client refreshes once on open to close any reconnect gap, then appends arriving lines; a chunk boundary never moves because `seq` is append order — dashboard JS

**Pointer Vision Read:**
1. Client sends `GET /neighbors?rkey=N&level=0` — `pool_server.py`
2. `pool.open_pointer(rkey, level)` opens one pointer at one point — `pool.py`
3. L0: returns lines in that cell; L1: 12×12 block map; L2: whole plane map — `pool.py`
4. Over-budget cells return stacked sub-map (phase axis splitting) — `pool.py`
5. `path` parameter walks one sub-cell down per read — `pool.py`

**Cross-System Edge Derivation:**
1. Memory bridge writes `"[chat-pool/<app>] <text>"` into `context.db` — `pool.py`
2. `ctx_plane.links()` matches pool lines to memory rows by content convention — `ctx_plane.py`
3. Both ends land in the same 144×144 plane — cross-system edges are just edges — `ctx_plane.py`

**Session End → Block:**
1. Platform calls `POST /session-end {app, force?}` — `pool_server.py`
2. `session_end(app)` reads all lines from the current session (last `pool_session_guard` boundary) — `pool.py`
3. Checks minimum line count (≥3) and substantive lines (≥1), unless `force=True` — `pool.py`
4. Dedup: if same title+story was written within the hour, bumps the existing block's rev instead of creating a new slug — `pool.py`
5. Auto-writes `kind:"block"` line with title, story, method, evidence, refs — `pool.py`

**Graph Exploration:**
1. Client sends `GET /explore?hash=<64hex>` (walk one line) or `GET /explore?rkey=<int>` (walk one cell) — `pool_server.py`
2. `pool.reach(h, hops)` or `pool.explore_cell(rkey)` walks outbound bonds up to `hops` deep (cap 14 per hop) — `pool.py`
3. Client sends `GET /patterns` for the whole-plane census (islands, bridges, hubs) — `pool_server.py` → `pool.pattern_census()`

## Key Abstractions

**RDH Capture (rkey):**
- Purpose: Derive a spatial address [0, 20736) from content bytes — ADDRESS only, never integrity
- Location: `chat-pool/pool.py` (`rkey()`)
- Pattern: Port of `core/rdh_capture.h` (RDH_CAPTURE_144, S=1). Walks a 144×144 grid using low nibbles of input bytes. `phase` parameter creates independent axes on the same plane for stacking

**Hash Chain:**
- Purpose: Integrity — every line's hash covers `prev+app+kind+to+text`, creating an immutable linked list
- Location: `chat-pool/pool.py` (`_prev()`, `append()`)
- Pattern: SHA-256 hash chain, "GENESIS" as genesis block. Chain audit verifies continuity

**Derived Index:**
- Purpose: In-memory fold over the log — cell counts, hash→rkey map, bond list, sighting list, timeline
- Location: `chat-pool/pool.py` (`derived()`, `_derived_absorb()`)
- Pattern: Cached against `(size, mtime)` of log.jsonl. Incremental update on append (absorb guard verifies tail line identity). Invalidated on foreign writes. Never persisted to disk

**Vision Levels:**
- Purpose: Hierarchical reads — L0 (cell content), L1 (12×12 block map), L2 (whole plane map)
- Location: `chat-pool/pool.py` (`open_pointer()`, `_coarse_map()`, `_plane_map()`)
- Pattern: Maps are address-only (no content crosses a map read). Over-budget cells trigger stacking via phase axis

**Bond Graph:**
- Purpose: Edges between lines — `kind:"bond"`, text `"<src64hex> <rel> <dst64hex>"`
- Location: `chat-pool/pool.py` (`bond()`, `unbind()`, `bonds_for()`, `all_bonds()`)
- Pattern: Ordinary lines, append-only. Tombstone via `unbinds` rel. Closed verb set: `uses, produces, validates, decides, follows, contradicts` + `x-<free>`. Blast radius = walk endpoint by endpoint

**Pointer / Sighting:**
- Purpose: Per-read state (no central pointer) + shared awareness via data
- Location: `chat-pool/pool.py` (`open_pointer()`, `sighting()`, `pointers_at()`)
- Pattern: Pointer born at read, dies with it. Sighting = `kind:"pointer"` line declaring a read happened. Awareness derived from lines, never server state

**Twinview (B-twinview):**- Purpose: Pair tesseract (i, i+9) with transpose link — same data, two views (A=32×36, B=36×32)
- Location: `chat-pool/tower.py` (`sibling()`), `chat-tower.h` (`ct_sibling()`)
- Pattern: Involution proven 20,736/20,736, fixed 0. Canonical view per writer, readers lock-free

**Feed Tick Layer (dashboard):**
- Purpose: Read a long shared log as scannable chunks instead of a wall of full messages
- Location: `chat-pool/pool.py` (`_seq_of()`, `stamp_seq()`), `chat-pool/pool_server.py` (`/tail`, `/stream`, dashboard JS)
- Pattern: Every line rides out with `seq` — its append ordinal, the same tick `timeline()` counts — and the client groups lines by `seq // CHUNK` (CHUNK = 8), so a chunk keeps its number however much the log grows after it. Older chunks render collapsed to one header row (tick, line span, time span, count, writer chips); a card shows at most CLAMP (260) characters behind an `อ่านต่อ` toggle; the app-tab bar isolates one writer's lines. The ordinal map is derived, lazily built once per index rebuild, and dropped when the index rebuilds — a stale ordinal would number a chunk wrongly
- Depends on: Derived Index (`seq_of` is a fold over the cached timeline, which carries the full hash at index 6)
- Used by: `pool_server.py` `/tail` + `/stream` (stamps), dashboard feed (chunks, ticks, clamp, tabs)

**Block System:**
- Purpose: Append-only knowledge blocks with revisions — the story of a unit of work, including how it changed
- Location: `chat-pool/pool.py` (`block()`, `blocks()`, `block_revs()`, `block_get()`, `block_timeline()`, `block_card()`, `parse_block()`, `render_block()`)
- Pattern: Each block is a slug (stable address) + sequence of revisions (rev 1, 2, …). A revision is an ordinary `kind:"block"` line. `session_end()` auto-writes a block when a platform session closes — dedup bumps the same slug if title+story match within the hour. Blocks carry title, story, method, evidence, and refs (verifiable anchors). `block_card()` renders a compact summary; `block_timeline()` returns all blocks ordered by last revision tick. Status set: `open`, `closed`, `parked`

**Graph Exploration:**
- Purpose: Walk the bond graph from any line or cell — understand what relates to what and the shape of the whole plane
- Location: `chat-pool/pool.py` (`motif()`, `reach()`, `explore_cell()`, `pattern_census()`)
- Pattern: `motif(h)` returns one node's edge list (the 1-hop panel). `reach(h, hops)` walks outbound edges up to `hops` deep (cap = `REACH_CAP` = 14 per hop). `explore_cell(rkey)` walks from an address instead of a line — collects all lines in the cell then their motifs. `pattern_census()` reads the whole bond graph once and classifies every node as island / bridge / hub, plus the top few lines everything hangs off. All read-only, derived from the same cached bond list as `all_bonds()`

**Curator + Pruning:**
- Purpose: Noise filter for the feed (display-edge only) and address reclamation for the plane (structural)
- Location: `chat-pool/pool.py` (`curate()`, `curator_verdicts()`, `curator_report()`, `curator_prune_report()`, `curator_hits()`, `curator_protect()`, `plane_prune_stats()`, `stamp_cur()`)
- Pattern: `CURATOR_RULES` is a tuple of (name, tag, weight, test_fn) — each rule scores a line. Feed hiding uses `CURATOR_HIDE` threshold; plane pruning uses `CURATOR_PRUNE` (higher, configurable via `POOL_CURATOR_PRUNE`). A hidden line still happened (display-edge only); a pruned line stops occupying its address. `plane_prune_stats()` reports what the last derived-index build actually dropped (cells, bonds, sightings — not just lines). `curator_prune_report()` walks the whole log (opt-in via `?prune=1`) to show what each rule caught

## Entry Points

**Dashboard (browser):**
- Location: `chat-pool/pool_server.py` → `GET /`
- Triggers: Browser navigates to `http://localhost:8390`
- Responsibilities: Single-file HTML+CSS+JS app: tick-chunked feed with a tick rail and per-writer isolate tabs, full-width composer pinned to the bottom, panels as a drawer below 900px, inbox, interactive 144×144 plane heatmap with bond drawing, radius mask, time scrubbing, click-to-popup detail panels, blocks mode, SSE live updates

**HTTP API:**
- Location: `chat-pool/pool_server.py` → `PoolHandler`
- Triggers: HTTP requests on port 8390
- Responsibilities: `/append`, `/tail` (`?clean=1` = curator-filtered), `/view` (`?clean=1`), `/stream` (SSE), `/neighbors`, `/pointers`, `/bonds`, `/explore` (graph walk by hash or rkey), `/patterns` (plane-wide link census), `/plane/timeline`, `/plane/meta`, `/curator` (`?prune=1` for plane stats), `/ctx/*`, `/inbox`, `/apps`, `/blocks`, `/blocks/timeline`, `/block` (POST), `/session-end` (POST), `/register`, `/flush`, `/health`, `/qr`

**CLI self-check:**
- Location: `chat-pool/pool.py` → `python pool.py demo`
- Triggers: Manual invocation
- Responsibilities: Runs all assertions in a temp-dir-isolated environment (count is dynamic, verified at runtime via ast parse). NEVER runs against live log

**CLI view:**
- Location: `chat-pool/pool.py` → `python pool.py view [N] [--cloud]`
- Triggers: Manual invocation
- Responsibilities: Print last N messages in human-readable format

**CLI blocks:**
- Location: `chat-pool/pool.py` → `python pool.py blocks [--status S] [--q Q] [--json]`
- Triggers: Manual invocation
- Responsibilities: List or search knowledge blocks; `block-show <slug> [--revs] [--json]` for one block + ref verification + revision history; `block <slug> --title T [options]` to write or revise a block

**CLI session-end:**
- Location: `chat-pool/pool.py` → `python pool.py session-end <app> [--force] [--json]`
- Triggers: Manual invocation
- Responsibilities: End a session and auto-write a block (same as `POST /session-end`)

**CLI bridge:**
- Location: `chat-pool/pool.py` → `python pool.py bridge [--dry]`
- Triggers: Manual invocation
- Responsibilities: Run the memory bridge once (lift decisions/handoffs into Magic Context); `--dry` previews without writing

**CLI curate:**
- Location: `chat-pool/pool.py` → `python pool.py curate [N] [--clean] [--json]`
- Triggers: Manual invocation
- Responsibilities: Report what the noise filter would hide in the last N lines; `--clean` prints the filtered feed; `--json` for machine-readable output

**CLI prune:**
- Location: `chat-pool/pool.py` → `python pool.py prune [--reveal] [--json]`
- Triggers: Manual invocation
- Responsibilities: Report what plane pruning would reclaim (read-only); `--reveal` lists every pruned line

**Scheduled flush:**
- Location: Windows schtasks → `chat-pool-flush`
- Triggers: Hourly scheduled task
- Responsibilities: `flush_emit()` + `memory_bridge()` (lift decisions/handoffs into Magic Context) + register flusher heartbeat + `push_outbox()` + cloud push via `pool_cloud.py`

**Wake runner:**
- Location: `chat-pool/pool_wake.py`
- Triggers: Windows scheduled task (every 15 min)
- Responsibilities: Peek inbox, spawn `opencode run` one-shot if work pending

## Error Handling

**Strategy:** Fail-safe with isolation. The log is never corrupted by a failed operation:
- `append()` uses `os.write` + `os.fsync` under process lock — crash mid-write leaves a partial line that `_prev()` can skip past (JSON parse on the last line)
- `demo()` runs entirely in a temp directory — a crashed demo cannot touch the live log, apps registry, or outbox
- `pool_cloud.py` push is best-effort: offline/worker-down/key-missing = skip quietly, retried next flush
- `derived()` cache guard: if the tail line is not ours, drop cache and rebuild (accept slowness over lies)
- `_last_line_is()` verifies by hash, not byte arithmetic — Windows text-mode fd adds `\r` making byte counts unreliable

## Cross-Cutting Concerns

**Concurrency:** Process-level file locking (msvcrt on Windows, fcntl on Unix) on `log.jsonl.lock`. Thread-level `threading.Lock` in pool_server.py. Single-writer / many-reader model — dashboard SSE holds connections but pool reads are lock-free after the append lock is released.

**Storage:** `log.jsonl` (append-only JSONL, UTF-8, CRLF on Windows due to text-mode fd). `apps.json` (identity registry). `.flush_state.json` (sha256 + offset cursor). `.bridge_state.json` (last bridged timestamp). `ctx_plane.db` (SQLite index cache, addresses only). `outbox/` (pending flush payloads). `towers/` (tower geometry on disk).

**Authentication:** Bearer token from `.pool_token` (auto-generated on first boot). Loopback always trusted. LAN clients present token via header or `?token=` query param (for EventSource which cannot set headers). QR code on `/qr` embeds token for mobile setup.

**Disk Layout:** `pool.py` is the single source of truth for all pool operations. `pool_server.py` adds HTTP + dashboard. `tower.py` + `chat_tower.h` are the geometry layer. GPU kernels (`gpu_*.cu`, `vulkan_fp.comp`) are parity-verified alternatives. `SPEC.md` and `BREATHING_SPEC.md` are the authoritative design documents. `bond_scale.py`, `ui_check.py`, `stream_check.py`, `base23_check.py`, `curator_check.py`, `error_check.py`, `reconnect_check.py`, `prune_check.py`, `latency_check.py` are verification suites; `run_all.py` runs all 9. `reimport_real.py` is a one-shot data tool that wipes and re-imports the real corpus.
