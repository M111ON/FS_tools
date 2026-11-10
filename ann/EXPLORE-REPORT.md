# EXPLORE-REPORT.md — ann/ field survey

Snaps the state of `I:/tools/ann` at the time of writing. Counts, paths,
running service state, and the benchmarks/docs that live here — so a later
reader does not have to re-derive the ground from scattered notes.

> This file is a snapshot, not a spec. Numbers are live-as-of timestamps;
> config/env is what the files say, not what the running service may have been
> doing between writes. Treat the running service as the live source of truth
> for "what is serving now", and this doc as the record of what was here.

## Live service, as of this survey

Health returned:

```json
{
  "status": "ok",
  "service": "ann_search",
  "fork_of": "DWGLS 8c85f9f",
  "index": "I:\\tools\\ann\\data\\memcore\\ann_index.jsonl",
  "entries": 54064,
  "routed": 1,
  "stamps_live": 54064,
  "route_dead": 0,
  "maxk": 32,
  "k": 56,
  "embed": "ok",
  "labels": "derive",
  "order_stale": 0,
  "heal_ok": 0,
  "heal_fail": 0,
  "loader": { "mode": "bg", "index_age_s": 1829.3 },
  "tags": { "path": "I:\\tools\\ann\\data\\memcore\\ann_tags.tsv", "kinds": 11, "tagged": 54064, "unlabeled": 0 },
  "stamp": { "live": 54064, "unplaced": 0, "side_rows": 54064, "gate": "ok", "gate_code": 0 },
  "totem": { "replayed": 110, "shut": 64, "free": 10366, "pair": 56 },
  "roster": { "on": 1, "doors": 56, "rows": 108128, "bytes": 432512, "src": "mmap" }
}
```

A few things worth noting from that one response:

- The serving index is `data/memcore/ann_index.jsonl`, **not** the `data/beir/*` copies.
- Embed is `ok` right now; the sidecar on `:18095` is answering.
- `labels` is `derive`, K is `56` — this is the derived (boot) label path, not the
  older fixed-anchor mode.
- `roster.on` is `1` and the roster is memory-mapped from a prebuilt file (`data/memcore/ann_anchors-roster.bin`).
- `toem.pair` = 56 matches `K` = 56, and `toem.shut` = 64 (roster has 56 doors, so the latch has seen a bit more than one full walk since boot/restart).
- `heal_ok`/`heal_fail` are both 0 — no self-heal events recorded in this window.

Regrow state (`GET /v1/state/regrow`) returned `{"error":"not_found",...}` — so there is no
regrow in flight right now.

### What this means in plain terms

- This is the long-lived memcore-derived index, served with the roster and derived labels.
- It is not the BEIR demo index, and it is not a freshly trained index — it is the
  production-ish in-memory index plus the prebuilt roster/labels sidecars.
- A reader who only looks at `data/beir` will get the wrong idea about "the index"; the
  file to point at is `data/memcore/ann_index.jsonl` and its companions.

## Repo layout

Top-level files worth knowing about:

```
Makefile                  build+test driver; make test, make serve, p3-compact-dry, etc.
FORK.md                   provenance: what was cut from DWGLS, what was left behind, how to re-sync
README.md                 runbook: build, run, env, regrow modes, integrations, optional axes
ROSTER.md                 the roster feature and the gate that proved it invisible
GRAPH-ROUTE.md           graph route (train-free candidate proposal) and the magnetic mix
WARM-PATH.md             warm/cold totem axis: what it actually does, where it is inert, what was retracted
STRESS-REPORT.md          pre-fix stress findings and which of them were already closed by 2026-10-08
EMBEDDERS.md             the three embedder packages and why ONNX vs GGUF matters
bench/perf-snapshots/    recorded perf snapshots (e.g. perf-routes-20261020.json)
```

Source and support trees:

```
src/          ann_search.c — the whole server (big single-file C)
include/      vendored headers (anchor_route, geo_wang_latch, latch primitives, route log, addr/capture,
              chain family, anchor_lettercube, etc.)
tests/        C anchor/latch/chain suites + p3 compact/append tests + python tests (roster, tags,
              sidecar, loader, soft zone, route watch, receipts, etc.)
bench/        microbenches (incl. bench_kernel.c for the float-kernel microbench)
scripts/      index build helpers, p3 compact, graft bias, route-sidecar validators, export helpers,
              sidecar builders, tokenizers, seedgen, etc.
experiments/  datasheets and one-off measurement scripts (fiqa/scifact, neighbor purity, embedding
              swaps, LC/LC-frustum overlays, MIRACL-th, SIFT receipts, numeric multi-view fixtures, etc.)
data/         indices, anchors, orders, logs, BEIR subsets, memcore-derived payload, query logs, sample,
              graft tables, sidecar/roster artifacts
build/        built binaries and a lot of scratch/pre- variants from earlier runs
docs/         dated handoffs, architecture notes, route-producer integration notes, dashboard/parked notes
```

### Tests

The repo has a fairly large test surface. Worth distinguishing:

- Anchor/route/latch C tests in `tests/*.c` (the classic set plus chain and LetterCube tests).
- Python tests in `tests/*.py` covering p3 compact, vec format, roster, tags, sidecars,
  loader swap, route watch, query sanitize, soft zone, receipts, seed golden, etc.
- `run_tests.py` exists specifically because `make` is not reliably on PATH on this box; it\n  drives the same suite with an explicit compiler and python interpreter.

A reader who only runs `make test` may miss the python side or the route-producer/sidecar
  tests, depending on environment. If the goal is "does this checkout still hold together,"
  prefer `run_tests.py` or a full local run that includes the python tests.

A couple of things that are easy to misread:

- `build/` is full of `*.bak*` / `*.pre-*` and old scratch exes. Those are run artifacts,
  not first-class deliverables.
- The first-class built binaries are `build/ann_search.exe`, `build/ann_search_batch.exe`,
  and `build/alc_real_gate.exe`. The rest are historical.
- There are two live-ish indices in `data/`: the long-lived `data/memcore` one and the BEIR
  `data/beir/fiqa` one. They are different sizes and different payloads; do not casually
  assume they are interchangeable.

### Service and ops helpers

```
ann_keepalive.py          supervisor: start sidecar + server, one-shot or loop
ann_route_watch.py        cold-path watcher for silent unroute on growth
deploy_and_gate.cmd       Windows deploy+gate wrapper (kill, copy batch exe, keepalive --once, gate_qlog)
ann-keepalive.xml         Task Scheduler registration for ann-keepalive (logon + every 5 min)
systemone_api.txt         unnamed service key/URL artifact (not part of the ANN server itself)
```

A few notes on the helpers:

- `ann_keepalive.py` is the runbook supervisor. It does not only revive the search server;
  it also starts the embed sidecar when `:18095` is down.
- `ann_route_watch.py` exists because `/health` can stay `ok` while the route quietly
  degrades; the watcher is the alert path for that failure mode.
- `deploy_and_gate.cmd` is the Windows-side deploy wrapper: kill existing binaries, copy the
  new `ann_search.exe` over the batch exe, run `ann_keepalive.py --once`, then run a query-log
  gate (`gate_qlog.py`).
- `ann-keepalive.xml` is the Task Scheduler registration for the keepalive task (logon + every
  5 minutes).
- `systemone_api.txt` is an external service credential/URL note. It is not part of the ANN
  server; treat it as an unrelated scratch secret artifact.

## Source shape

`src/ann_search.c` is the server. It is a single large C file (6,226 lines as of this survey).

Selected knobs and hooks that appear in both source and bench code:

- `ANN_MAXK` = 32 (standalone topk bound; upstream served 8).
- `ANN_TOTEM_WARM` — totem warm path (hot/cold axis), off by default.
- `ANN_ROSTER` / `ANN_ROSTER_PATH` — roster on/off and where to load it from.
- `ANN_WALK_PRUNE`, `ANN_WALK_FLOOR` — pruned walk + confidence floor.
- `ANN_WALK_HASH`, `ANN_WALK_DUMP` — walk fingerprint and optional walk dump.
- `ANN_FLOAT_KERNEL` — float-kernel score path (default off; the old double path remains).
- `LZ_ROUTE_BUCKETS` — bucket count for the routed walk.
- `ANN_GRAFT*` — structural graft bias and the totem×graft amplifier.

A reader looking for "how does the server actually decide what to walk" should start from the
regrow/search path in `ann_search.c` and then cross-reference `ROSTER.md`, `WARM-PATH.md`,
`GRAPH-ROUTE.md`, and `STRESS-REPORT.md` for the feature-level story.

## Benchmarks and measurement scripts

These are the repeatable measurement files I'd point a later reader at first:

- `bench_rows_examined.py` — looked-at vs scored, and the roster A/B gate.
- `bench_roster_scaling.py` — when the gather starts to cost real time across bucket counts.
- `bench_query_speed.py` — roster cost + repeat-query cost.
- `bench_kernel.c` — double+div vs float×inv microbench on this CPU.
- `bench_warm_recall.py` — warm-path recall gate (C1/C2 style measurement).
- `bench_latch_ab.py` — budget-limited latch A/B with a consistent snapshot gate.
- `bench_latency_split.py` — embed-vs-scan split on a running server.
- `tests/verify_kernel_equivalence.py` — OFF-vs-ON float-kernel equivalence on a small synthetic index.

There is also `bench.py` and a cluster of older/specialist benches in the root and `bench/`;
some are datasheet-backed (warm, latch, multiview, tag, soft-zone, intent), some are routing/
proof-oriented. The `experiments/` tree has the BEIR-local verify path and the frozen-base and
container experiments with their own result notes.

One concrete snapshot already on disk: `bench/perf-snapshots/perf-routes-20261020.json`. It
records cold/repeat/k1/k32/regrow route numbers at that point in time, including the regrow
cost on a 62,078-entry index.

### Benchmarks are not one thing

- Some benches are **server-side performance** (roster, warm, latch, latency split, kernel).
- Some are **correctness gates** (kernel equivalence, roster A/B, warm recall, latch AB).
- Some are **integration/external** (BEIR local verify, frozen-base, container, MIRACL, SIFT
  receipts).
- Some are **producer-side** (route sidecar export/validate, numeric sidecar fixture, manifest).

If someone says "run the benchmarks", the useful follow-up is "which layer do you mean": the
serving path, the routing correctness, the external recall, or the sidecar/producer contract.

## Data

### The serving index family

- `data/memcore/ann_index.jsonl` — 54,064 lines; the live-serving memcore-derived index.
- `data/memcore/ann_anchors.bin`, `data/memcore/ann_index.order` — anchors + placement order.
- `data/memcore/ann_anchors-roster.bin` — prebuilt roster mmap for the live index.
- `data/memcore/*` also has loader logs, `.f16`, `.seeds`, `.stamp`, tags, totem shadow, route
  log artifacts, receipts, etc.

### BEIR demo family

- `data/beir/fiqa/ann_index.jsonl` — 57,638 lines; a BEIR FIQA-derived index.
- `data/beir/fiqa/corpus.jsonl`, `data/beir/fiqa/queries.jsonl`, `data/beir/fiqa/qrels/{dev,test,train}.tsv`.
- `data/beir/fiqa/ann_anchors.bin`, `data/beir/fiqa/ann_anchors-roster.bin`, `data/beir/fiqa/ann_index.jsonl.loader.log`.
- `data/beir/fiqa/query_log/ann_query-20261101.jsonl` — recorded query log for that index.
- `data/beir/scifact/...` — similar BEIR scifact family, including `scifact/scifact/...` nesting and its own `README.md`.

### Graft / routing support data

- `data/graft/bias.json`, `data/graft/keys.tsv`, `data/graft/entries.jsonl`, plus `files.tsv`, `nb.tsv`, `rel.json`, `tag_lut.json`, `tag_lut_report.json`.

### sidecar and contract artifacts

- Route sidecar schema lives in `route_sidecar_schema.md` and `route_metadata_schema.sql`.
- Producer/export helpers include `export_producer_routes.py`, `export_unknown_routes.py`,
  `build_numeric_sidecar.py`, `validate_route_sidecar.py`, and `route_manifest.py`.
- There is also a numeric multi-view spec (`numeric_multiview_spec.md`) and a LetterCube\n  handoff note (`HANDOFF-2026-10-30-LC-ANN.md`) for the container/identity work.

### Logs and metadata

- `data/query_log/ann_query-*.jsonl` — recent query logs.
- `data/memcore/lz_route.log` and assorted route/totem shadow files.
- `data/memcore/ann_index.jsonl.stamp`, `.f16`, `.seeds`, `.canary.log`, `.loader.log`.

## Build and tests

Build target:

```sh
make                 # -> build/ann_search.exe
```

Test suite (anchored on `make test`):

```sh
make test
```

`make test` covers the anchor/latch C suites plus the python tests that can run here
(`test_p3_compact`, `test_vec_format`). There is also a p3-compact dry-run path and a
`p3-tools` target for the compact/append helpers.

Note: on this box `make` is not on PATH as `make`; the repo's `Makefile` is a GNU make file
and the available mingw make is `mingw32-make` at `/c/mingw64/bin/mingw32-make`. If a reader
expects `make` to Just Work, they should confirm which make is on PATH first.

## Service shape

Endpoints:

- `POST /v1/state/search` — single query.
- `POST /v1/state/search-batch` — batch (N≤32).
- `POST /v1/state/regrow` — regrow with modes.
- `GET /health` — service + index + embed + totem + roster status.

Runtime expectations (from the repo docs and current health):

- Embed sidecar is expected on `127.0.0.1:18095` (llama-server embedding) for the text path.
- The memcore-derived index uses stored ONNX vectors for the vector path; the GGUF sidecar
  matters for the text path.
- This is not a "drop a new .gguf and go" server — sidecar, paths, and index have to line up.

## What I would not assert from this survey

- Whether the running server's current behavior exactly matches `ann_search.c` on disk, since
  the running binary may differ from the built source until next rebuilt.
- Whether any particular old `build/*.bak*` or `build/ann_search_batch.pre-*` artifact is still
  a good reference — treat them as history, not as the current deliverable.
- Precision recall numbers beyond what the existing docs and snapshots say; those are
  measurement-backed in their own files, not something to re-derive by hand here.

## If you revisit this later

Start here:

1. `health` on the running service.
2. `FORK.md` if you care where this came from and how to re-sync.
3. `ROSTER.md`, `WARM-PATH.md`, `GRAPH-ROUTE.md`, `STRESS-REPORT.md` for the feature/quality story.
4. `bench_rows_examined.py` / `bench_roster_scaling.py` / `bench_query_speed.py` if you want to
   reproduce the performance shape instead of trusting prose.
