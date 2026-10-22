# HANDOFF-2026-10-22-ann-scope-loader.md

> ANN fork (live root `I:/tools/ann`), session 2026-10-21 evening → 2026-10-22 morning.
> Upstream pointer: `DWGLS-native-fs/docs/HANDOFF-2026-10-22-ann-scope-loader-upstream.md`
> Prior: `docs/HANDOFF-2026-10-21-ann-tree-side.md` (tree/fruit separation), `docs/HANDOFF-2026-10-20-ann-isolate.md` (A: perm-sidecar, B: door).

## 0. Start here

- **Live root** `I:/tools/ann` · **server** `ann_search_batch.exe --serve 8096` · **supervisor** `ann_keepalive.py --once` · **index** `data/memcore/ann_index.jsonl` (258 MB / 62,683 rows) · **embed sidecar** `:18095` (e5-small, ONE slot).
- **Commits (I:/tools main):** `c33664e` scope + tag zone chain · `3ac597c` keepalive starts without a console window · `1cb2252` the index reload leaves the request path. **I:/tools/cloud_ws:** `44c3535` ann_sync re-bakes the tag sidecar.
- **One line:** the ANN search now takes a **SCOPE** (a zone), and the service **no longer stalls when the index is rebuilt**.
- **Board (chat-pool :8390/task/ui):** `task-t4407b8f5` done (the loader) · `task-t75359d36` sanitize the query before embed (503s) · `task-t87ad8462` H1 watcher · `task-t4fbea5cf` per-zone mini vector DB · `task-t5ba6ae63` `GET /v1/state/tags` · `task-tabbd47c5` soft zone bias vs hard filter · `task-t795733ad` one parse per rebuild.

## 1. Why this session existed — the owner's tag idea

The owner brought up a long-held idea: **a tag system embedded in the vector DB**, with the five rules he had worked out before (recalled from the memcore entry "tag เป็นพิกัด"): tag accrual, bake = freeze the LUT, forward tagging, backward double-check, clean cycle.

Restated as rules, before any code:

1. **Accrual** — a row earns a tag from what it is (a number → NUMBER, an icosphere → GEOMETRY).
2. **Bake** — the tag → coordinate LUT is **frozen** when the tag set stabilises; zones are never renumbered.
3. **Forward** — new rows join their zone by the frozen LUT.
4. **Backward** — a new/changed tag re-checks the old rows and reports the ones that sit in the wrong zone.
5. **Clean cycle** — export → filter by the LUT → a clean store.

What already existed upstream: memcore's `entries.pattern` (12 values: v1-migrate, chatpool, hermes, brainstorm, opencode, architecture, config_values, constraints, freebuff, project_rules, naming, memo) and `entries.source_file` (the session/app a row came from). The ANN index carried `file` but **the route never used it**.

The measurement that decided the shape (bench, 300 queries, self-excluded oracle):

| arm | recall@5 vs the global oracle | items_scored | note |
|---|---|---|---|
| live route (cosine, budget 24k) | **0.910** | 24,000 | the incumbent |
| kind scope | 0.698 | ~19,376 | no work saved |
| file scope | 0.442 | 136 (p50) | 94× less work |
| tag scope | 0.34 | 59–334 | 72×–407× less work |

and the answer quality barely moves: the cosine gap (global best − in-zone best) is **mean 0.0110, median 0.0000**.

**Verdict that shaped everything: a tag is a SCOPE SELECTOR, not a speedup for global search.** It answers "find inside this zone" and gives up the global answer on purpose — and it gives it up cheaply.

## 2. Phase 1 — `scope` on the request side (live)

`POST /v1/state/search` accepts a **scope**, in four forms:

- `file` group — `exchange/tray_window.bat หน้าต่าง…`
- `kind` — `exchange`
- `tag:<name>` — the zone name
- `zone:<id>` — the frozen zone id

Rules (deliberate, and each one cost a bug to learn):

- A scoped query **bypasses the route layer entirely** — no buckets, no tombstone, no order sidecar. The candidate set is derived from the rows themselves, so it **cannot drift** from the index. There is no positional sidecar for the scope path at all.
- `file` scope widens to `kind` when the group is too thin to fill top-k (`mode:"scope+kind"`).
- `tag:` / `zone:` are a **closed group** — never widened; too thin → HTTP 400 `scope_too_narrow` / `scope_not_found`.

Measured on the live index (62,632 rows, same server, same moment):

| | unscoped | scoped |
|---|---|---|
| items_scored (p50) | 24,000 | **608** (41× less work) |
| latency p50 | 15.9 ms | **11.6 ms** (groups ≤1,000 rows: 9.9 ms) |
| server hits == numpy exact-in-group | — | **80/80** score-exact (78 same sid, 2 exact cosine ties) |

**Two bugs found by measuring, both fixed:**

1. The scope string was not JSON-unescaped → a Thai tag sent as `\uXXXX` matched no row and **silently fell back to its kind**.
2. The pattern buffer was 64 bytes and a Thai tag is 127 → the walk got an empty array but the bound was the index length → an **out-of-bounds read** (mode "anchor", items 1, one hit outside the scope).

## 3. Phase 2 — the tag chain (the data side, live)

`scripts/tag_lut.py` bakes the frozen LUT and writes a **sid-keyed** sidecar `ann_tags.tsv` (`sid\ttag\tzone`) — keyed by sid, not by position, so a stale sidecar cannot mislabel a row.

- All five rules are implemented: accrual (tag = memcore `pattern`), bake (zones are frozen forever; a new tag appends a new zone; a tag that loses all rows is **retired, not deleted**), forward, backward (rows whose tag changed are reported), clean (orphans + retired reported).
- **The freeze is proven:** bake b0004 keeps b0001's zone ids for all 12 tags.
- **Automation:** `ann_sync.py` re-bakes the sidecar right after every index rebuild — proven live at 22:04→22:06 (index 62,634 → sidecar 62,634 → regrow ok).
- **`/health` tells the truth about it:** `tags{path, kinds, tagged, unlabeled}` — a stale sidecar is visible instead of silently narrowing a scope.

Measured on the live index: **220/220** server hits == numpy exact-in-tag; work **71.9×–406.8×** less on the small tags (naming 59 rows, project_rules 136, constraints 246, config_values 268, architecture 334).

**The negative result worth keeping:** the semantic tag is **orthogonal** to the geometric anchor buckets — a tag's rows spread over all 8 buckets (top-bucket share 0.22–0.50), the baked tag→bucket accuracy is **0.479**, and it conflicts with the row's own stamp **79.1%** of the time. So a tag LUT must **not** claim an anchor bucket as its zone: the tag stays a separate coordinate that a query selects, never a replacement for the geometry.

## 4. The loader — the index reload leaves the request path (the availability work)

**The problem.** The index is 258 MB of JSONL; parsing it takes ~50 s on this box. That parse happened **inside a request**, holding the request lock the whole time, so every request — `/health` included — queued behind it. Measured live 2026-10-21: `/health` did not answer within 60 s while `ann_sync` rebuilt the index; an earlier bench caught a 55,562 ms query.

**What changed** (`ann/src/ann_search.c`, commit `1cb2252`):

1. **A background loader owns the reload.** It parses the new file into its **own** buffer (no lock held) and swaps the pointer under the lock; the old buffer is freed there. A query always sees a complete index, and a rebuild costs the service nothing. It runs **BELOW_NORMAL** priority (2-core box) and skips while a regrow is in flight.
2. **The loader watches the stamp sidecar on its own.** The incremental regrow writes only the sidecar (the fruit is never rewritten), so the routing table can change while the index file does not — before this, such a change was invisible until the next index rebuild.
3. **THE GATE.** The sidecar is *positional*, so its labels are applied only when the regrow state **proves** the buffer's prefix is the same rows — the regrow's own append contract (`state.n_prev` + `state.last_sid`). A sidecar from another index version is **refused**, not laid over the wrong rows. This is the S9 lesson made enforceable.
4. **The regrow releases the request lock** around its heavy work (it parses the index, trains the anchors, rewrites the tree's channel) and takes it back for the answer; it also runs below normal priority.
5. **Bounded waits.** The embed call had **no socket timeout** and a **blocking `connect()`**. The sidecar runs **ONE slot**, so while it is busy its accept backlog is full and `connect()` waits for the SYN to be retried — **measured 53 s**, and it happened **while the request lock was held**, so every other request queued behind a health probe. Now: a non-blocking connect + `select()`, plus `SO_RCVTIMEO`/`SO_SNDTIMEO`; `/health` lowers the ceiling to 2 s and then answers honestly (`"embed":"down"`).
6. **`/health` gained the loader's truth:** `loader{mode, index_age_s}` — the age of the **served** buffer. A rebuild in flight shows up as a growing age, never as a stalled request.
7. **`scripts/build_index.py` retries the atomic swap.** On Windows the destination can be held open by a scanner (Defender walks the freshly written 258 MB file) and `os.replace` fails with `WinError 5` — measured live: the rebuild aborted at the swap and left a 258 MB `.tmp` behind (deleted; the disk was at 2.66 GB free).

**Measured, live, `ann_sync --force` with `/health` polled every 3 s:**

| | before | after |
|---|---|---|
| probes that timed out (30 s) | 3 of 77 | **1 of 61** (and the cause is CPU contention, not the lock) |
| `/health` unanswered | > 60 s | the lock is provably free during the regrow now |

**The trap that cost the most time to find (do not repeat):** the first hypothesis was "lock contention", and it was wrong — the lock was free; the thread was simply not scheduled, because the box (2 cores) was running the loader's parse + the regrow's kmeans + the sidecar at once. The diag had to be written to a **file** (`<index>.loader.log`) because the stderr of a detached server is not captured by the supervisor — the missing log line was the thing that made this look like a lock bug for two iterations.

## 5. What is left, in the order it should be done

| # | board | what |
|---|---|---|
| 1 | `task-t795733ad` | **one parse per rebuild.** The full regrow path still rewrites the fruit (the jsonl), so the loader parses it a second time; and the regrow parses the same 258 MB itself. Make the full path sidecar-only like the incremental one (that also makes the fruit immutable — the architecture's own rule), and let the regrow reuse the buffer the loader just parsed when mtime+size match. |
| 2 | `task-t75359d36` | **sanitize the query before embed.** 1 of 120 corpus queries (0.8%) returns `503 embed_unavailable`: the sidecar answers 500 for DSML-style tokens (`<｜｜DSML｜｜tool_calls>`); the class reproduces 100%. The corpus side is sanitized (`build_index.py`), the **query path is not**. |
| 3 | `task-t87ad8462` | **H1 watcher**: own the rebuild+retrain+swap, and a gate that ALERTS when `stamps_live != entries` or `route_dead` is set. `ann_sync` already does the healing every 15 min; what is missing is the alerting truth and the watcher owning the swap (now that the loader owns the reload, the two must agree). |
| 4 | `task-t4fbea5cf` | **per-zone mini vector DB** — a zone query still walks every row of the zone (`tag:v1-migrate` = 21,316 rows, only 1.1× less than unscoped). |
| 5 | `task-t5ba6ae63` | **`GET /v1/state/tags`** — a client cannot discover the zone vocabulary today. |
| 6 | `task-tabbd47c5` | **soft zone bias vs the hard filter** — the codebase already has the machinery (`ANN_GRAFT` + `ANN_AMP_BOOST`): boost the zone instead of restricting to it, and keep the global answer. Measure recall@5, cosine gap, items_scored, latency for unscoped / hard filter / soft bias (2–3 lambdas) / hard filter + fallback. |

## 6. How to verify (never against the live corpus)

```powershell
cd I:\tools\ann
I:\tools\obsidian-memory\.venv\Scripts\python.exe run_tests.py      # ALL PASS (includes tests/test_loader_swap.py)
# the loader, isolated: a reload never blocks a query, /health keeps answering,
# an unproven sidecar is refused, a proven one is picked up on its own
I:\tools\obsidian-memory\.venv\Scripts\python.exe tests\test_loader_swap.py
# live truth
curl http://127.0.0.1:8096/health
# scope, live
curl -X POST http://127.0.0.1:8096/v1/state/search -H 'Content-Type: application/json' `
  -d '{"query":"project rules","topk":3,"scope":"tag:project_rules"}'
```

## 7. Traps

- **Do not put the tag into the vector dims** — the vector is a MAP, not semantics (#949); it also breaks the space against memcore's own vectors and re-opens the model-swap fragility (#7563 H3). The tag is a coordinate the query selects.
- **Do not widen a `tag:`/`zone:` scope** to make it work — it answers a different question; the fallback belongs to `file` → `kind` only.
- **A positional sidecar needs a proof** that the buffer it lands on is the same rows (the gate). "The sizes match" is not a proof; the prefix contract is.
- **`stderr` of the detached server is not captured.** Diagnostics that must survive go to a file (`<index>.loader.log`).
- **The sidecar is the tree's channel; the fruit is never rewritten.** The full regrow path still breaks this rule — see task #1 above.
- `make` is not on PATH here: use `python run_tests.py`. Bare `python` has no numpy — use `I:\tools\obsidian-memory\.venv\Scripts\python.exe`.
- The scratch servers can linger: `Get-Process ann_search*` should show exactly one live instance.
