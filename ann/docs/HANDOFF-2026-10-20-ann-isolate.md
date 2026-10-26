# HANDOFF-2026-10-20-ann-isolate.md — Apple-Tree ANN isolation (A: perm-sidecar, B: totem door)

**Root:** `I:/tools/ann` (git root `I:/tools`, branch `main`, remote `origin=https://github.com/M111ON/FS_tools.git`)
**HEAD:** `d48bc08` — pushed to `origin/main` this session (9 commits: `a63e9b5..d48bc08`)
**Prior handoff chain:** docs/HANDOFF-2026-10-14.md → docs/HANDOFF-2026-10-19-late.md → **this file**
**Live at handoff time:** `:8096` `ann_search_batch.exe` healthy, routed=1, 62,187 entries, e5 embed ok (:18095 one llama-server)

---

## 1. WHAT CHANGED TODAY (2026-10-20)

### Architecture decision (user-approved via clarify)
- **Root = `I:/tools/ann` ONLY.** `DWGLS-native-fs` stays the address-layer library (20736/CAPO/frustum/metatron headers); the live search server is the ann fork. No bidirectional porting; thin include shims only if later needed.
- Adopted the user's Apple-Tree architecture: **apple (payload row) never moves; branch (anchor/perm sidecar) moves; totem = door to its bucket's members; graft = fail-loud fallback.**

### [A] perm-sidecar isolate — commit `00d60c5`
- **Problem:** incremental regrow rewrote `ann_index.jsonl` (~222 MB text, `fprintf %.6g`) on every append → 62–78 s of pure I/O on the hot path, plus the historical 70+ s full regrow the user flagged.
- **Change:** incremental regrow writes a delta **stamp sidecar** `<index>.stamp` (8 bytes/row: two int32 `anchor`,`anchor2`), only for rows `[n_prev..n)`, appended in place; bootstrap covers all n rows one time (~0.5 MB at 62k). `lz_index_load` overlays the sidecar when its size matches `n*8`; a stale/undersized sidecar is ignored (jsonl stamps stay authoritative — same contract as before). **The 222 MB jsonl is NEVER rewritten on the incremental path.**
- Full path unchanged (may rewrite jsonl — declared in the code comment).
- **Measured:** unchanged fast path **1 ms** (from 62–78 s); append regrow mode=`incremental`, jsonl stat delta **0 bytes** on the regrow itself; anchor/ord/perm files unchanged in format (`ORDR`/`RCNA` headers untouched — H2 pair binding intact).

### [B] totem DOOR — commit `d48bc08`
- **Problem:** below-floor fallback (ANN_WALK_PRUNE top score < floor) went straight to a full scan; the shut-totem buckets (already-paid walks) were not used as an entry point.
- **Change (env `ANN_DOOR=1`, default OFF = byte-identical):** when the walked set scores below floor and the query's routed buckets contain a **SHUT** totem, the walk order is rebuilt bucket-major **door-first** (members of the shut bucket, then the other routed buckets, then everything else — recall cannot drop, only ordering moves). Mode label reports **`door`**.
- One-shot: after a door pass, `full2=1` so a repeat below floor falls through to the existing full scan (graft/full2 remains the fail-loud final fallback; graft soft-bias unchanged).
- **Gotcha found and encoded (probed 2026-10-20 with ANN_DOOR_TRACE):** bucket 0 maps to wl-id 0 = `WL_RESERVED_LO`; `wl_traverse` refuses reserved ids, so **bucket 0 can never shut** — the door picks the FIRST routed bucket that is actually shut, not blind `rb[0]`.
- Debug: `ANN_DOOR_TRACE=1` prints up to 8 `[fallback-trace]`/`[door-trace]` lines to stderr.
- **Measured (scratch 400×384):** D1 OFF=`anchor+p` (identical semantics), D3 forced-fallback ON=`door`, hits≥3, top-5 sids identical OFF vs ON (`seed-0013,0026,0052` overlap 3/3), health routed=1.

### Tests added
- `tests/test_perm_isolate.py` — P1 fresh full / P2 append incremental + **jsonl size & mtime untouched by the regrow** / P3 sidecar size `n*8` / P4 unchanged fast path / P5 anchor-mode search + routed health. Port-independent, scratch server on `ANN_PORT=8097` ONLY.
- `tests/test_door_path.py` — D1 OFF=pruned-family label, D3 forced-fallback lands `door` (second call of the same query: first call shuts the totem, second takes the door), top-5 overlap vs OFF, D4 fallback telemetry. Scratch port 8097 only.
- `tests/test_vec_format.py` — **pre-existing failure fixed** (not caused by [A]/[B]; proven by stash-run): pin `abs(o_bin-1.0) < 1e-9` contradicted the file's own docstring ("top-10 overlap vs the store oracle 99.93%"); the strict pin was killed by 5e-7 near-ties inside top-10 sets (stable 0.99967–0.99970). Now pinned `< 1e-3` with an explanatory comment; the exactness contract remains on `float32 ranking == text ranking` (1e-9).

### Fixes kept from earlier today (in this push)
- `1e1e342` — the `routed:0` brute-fallback incident: incremental regrow did not re-save anchors ntr (antr != nent → `lz_route_cached` rejects forever) → re-saves `anch_save_atomic(n)`; plus **fail-loud race guard** (mode=`race`, rc=-2) when ann-sync appends mid-regrow.
- `a2314a4`/`a63e9b5` — float kernel (`ANN_FLOAT_KERNEL=1`) + `/v1/state/search-batch` endpoint; `77fea58` docs; `39f8a08`/`e9c260f` dataflow map + per-route perf snapshot.
- `ef3ab65` — working tests ported OUT of %TEMP% into `tests/` (user rule: evidence does not live in temp).

---

## 2. RUNTIME INTERFACE (unchanged surface + additions)

```
POST /v1/state/search          {"query": str, "k": n}  (or {"vec":[...]} to skip the sidecar embedder)
POST /v1/state/search-batch    {"queries": [...], "k": n}     ← additive, telemetry.took_ms/embed_ms
POST /v1/state/regrow          {}  (unchanged fast) | {"force":1} (full)
GET  /health                   status/embed/entries/routed/totem
```
Env (all additive; defaults = previous behavior):
`ANN_PORT`, `ANN_INDEX_PATH`, `ANN_ANCH_PATH`, `ANN_ORDER_PATH`, `ANN_ROUTE_LOG`,
`ANN_TOTEM_WARM=1`, `ANN_FLOAT_KERNEL=1`, `ANN_WALK_PRUNE=1`,
`**ANN_DOOR=1**` (new, default OFF), `ANN_DOOR_TRACE=1` (stderr debug),
`LZ_SEARCH_BUDGET`/`_FRAC`, `LZ_ROUTE_BUCKETS`, `ANN_GRAFT*`.

Regrow telemetry: `mode ∈ {unchanged, incremental, full, brute, error, race}`; rc: 0 anchor / 1 brute / 2 unchanged / **-2 race (fail-loud, caller retries)**.

Files on disk (data/memcore/):
`ann_index.jsonl` (222 MB, the apple rows), **NEW: `ann_index.jsonl.stamp`** (8B/row stamps), `ann_anchors.bin` (branch), `ann_index.order` (perm, H2-bound), `ann_anchors-state.json` (append contract).

---

## 3. HOW TO OPERATE (cold start)

1. Live server = `I:/python3.14.4/python.exe ann_keepalive.py --once` (scheduled task `ann-keepalive`; do NOT disable it). It spawns `build/ann_search_batch.exe` on :8096 with env above.
2. A scratch server for experiments: `ANN_PORT=8097` + the four path envs; build from source with
   `gcc -O2 -std=c11 -Wall -Wno-unused-parameter -Wno-sign-compare -Wno-format -Wno-unknown-pragmas -Iinclude src/ann_search.c -o build/ann_search_isolate.exe -lws2_32 -lpsapi -lm` (mingw64; NO AVX on this box).
3. Deploy a rebuilt binary: build `*_new.exe`, kill the live PID (verify `tasklist` that you are killing the RIGHT process), swap, then run `ann_keepalive.py --once`.
4. Test suites: `python run_tests.py` (ALL PASS at d48bc08), plus the two new scratch tests (need `I:/python3.14.4/python.exe`, which has numpy; bare `python` does NOT).

## 4. KNOWN LANDMINES (all real, all probed today)

- **Killing the live process by image name kills the WRONG instance when a scratch server from a test run is alive.** (Today's incident ×2 — live :8096 died ~30 s twice; keepalive recovered it both times.) Rule: kill by PID after `tasklist`, and check the port with `netstat -ano | grep 8096`.
- **Leaked shell env poisons child servers:** an exported `ANN_PORT=8099` made `test_incr_fix.py` start its server on 8099 and poll 8097 forever. `env | grep ^ANN_` and unset before driving tests.
- **`test_incr_fix.py` / scratch tests bind 8097** — a stale scratch instance occupies it; Popen boot-wait then fails. Kill stray `ann_search_isolate.exe` first.
- **gcc output file locked by a live exe** → build to `*_new.exe`, swap after kill (ld "Permission denied" otherwise).
- **Windows-native `python` must get Windows paths** (`C:\...`); `cygpath -w` before passing to Popen if the value came from MSYS.
- The verifier script written this turn ends with `RESULT: ALL BEHAVED (ad-hoc verification, not a project suite)` — that phrasing is the project convention; do not restate it as a project suite result.

## 5. DECISIONS MADE (and why)

- Door does NOT hard-require graft. Graft stays a soft score bias + the walk's graph proposals; per the user, graft is the *fail-loud* fallback when door/route both cold — implemented as: door reorders (recall-safe), full2 scan (the existing confidence-fallback machinery) is the last resort; a door that finds no shut bucket leaves the code path so `anchor+full` still fires. NO silent path was added.
- `bucket 0` is structurally unhittable by totems (reserved id) — door skips it by rule, matching the walk's own refusal.
- Full jsonl rewrite still exists only via the FULL regrow path (may later become `/v1/admin/compact`; not built — no caller wanted it yet).

## 6. OPEN ITEMS (not hidden — owned)

1. **`ANN_DOOR=1` is NOT active on :8096** (default OFF by design). Activating = flip env in `ann_keepalive.py` + one live restart, after a live A/B (top-5 sid equality OFF vs ON on 300 real queries) mirrors what the scratch D3 already proved.
2. **memory-daemon + hermes-mc-sync scheduled tasks remain DISABLED** pending the backfilling decision (ann-sync + ann-route-watch re-enabled mid-day).
3. graft-as-hard-fallback (graft proposes the walk list when the route layer rejects) is NOT built — today graft kept its soft-bias contract. If wanted, the door branch is the attachment point.
4. `/v1/admin/compact` (clean rewrite of the jsonl stamps as an admin action) — deferred; full regrow already covers the need.
5. S2 (int8 pack + per-bucket scale) and S3 (contiguous reorder) remain parked from the 2026-10-14 plan.

## 7. VERIFICATION TRAIL (numbers, this session)

- `run_tests.py` ALL PASS at both commit points (including vec_format after the pin fix).
- `test_perm_isolate.py` 9/9, `test_door_path.py` 8/8 (twice each).
- Live smoke: `/health` ok routed=1 entries 62,187; `search-batch` 21 ms; regrow unchanged jsonl delta 0 bytes.
- Final session verifier: V1–V5 ALL BEHAVED (ad-hoc, not a project suite).
- Push: `origin/main` = `d48bc08` (9 commits `a63e9b5..d48bc08`), verified by `git rev-parse` after push.

## 8. FILE MAP (delta this handoff)

```
ann/src/ann_search.c        [A]+[B] landing (stamp sidecar write/read overlay + door branch + race guard commit 1e1e342 merged)
ann/tests/test_perm_isolate.py   new
ann/tests/test_door_path.py      new
ann/tests/test_vec_format.py     pin fix (comment explains)
ann/docs/bench/ANN-DATAFLOW-MAP-2026-10-20.html  from commit e9c260f (unchanged here)
```
Everything else (Makefile, include/, bench/) untouched by [A]/[B].
