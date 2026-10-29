# Session handoff — Gen 9 LetterCube / ANN (2026-10-30)

## Goal of next session
Continue Gen 9 work with the corrected separation: container/field, fingerprint/seed, payload/semantic ANN, and Totem are distinct chains. Define and test per-shape connector/path identity before any ANN optimization.

## State of play
- This session audited the ANN/LetterCube blueprint and confirmed the served core has anchor routing and bounded roster/bucket search, but no row-level HNSW/efSearch.
- `include/anchor_lettercube.h` was added as a deterministic anchor-bucket → LetterCube bridge. It is currently identity/telemetry, not semantic routing.
- `src/ann_search.c` reports `route.lc{on,bucket,face,name_level}` and builds the LC cache at table load; it does not filter or reorder the vector walk.
- Tests added: `tests/test_anchor_lettercube.c`, `tests/alc_real_gate.c`; `Makefile` and `run_tests.py` include the unit test. Full suite passed; real memcore gate passed: 63,915/63,915 rows, 191,745 checks, zero mismatch, all 26 L2 faces used.
- Existing identity receipts remain separate: row stamp, roster/order bindings, walk_hash, index_fp, anchor checksum, and Totem epoch. No single combined epoch exists.
- Corrected interpretation from the owner: letters may repeat because major roots run vertically and minor roots connect laterally through multiple connectors. Pools are local, not global. Each shape has its own 52-symbol pool: triangle(3), square(4), hexagon(6), octagon(8) side-count metadata. A symbol alone is not a collision; collision requires the full connector/path context.
- Numeric position suffixes are not needed per letter. A number may encode shape phase/side count, but decoder must not confuse it with LetterCube level/face count.
- Existing `chain_addr.h`/`chain_headtail.h` describe letter paths and reserved marker 0, but do not yet model major/minor root, connector, shape pool, epoch, or composite collision identity.

## Open decisions
- Define the authoritative composite identity, likely including epoch, major root, minor root, entry/exit connector, shape phase, level, local path, and offset. Do not add a global used-set or reject repeated letters.
- Decide whether the shape number is side count (3/4/6/8) or a separate compact phase code; keep level metadata distinct.
- Design a measurement-only allocator audit before changing generation: record pool/shape/root/connector/path reservations and classify true duplicate, intended merge, residual, and closed-node cases.
- Keep LC/container correctness gates separate from semantic ANN recall gates. Dataset quality (memcore/SIFTsmall/SIFT1M) evaluates ANN payload search, not fingerprint identity.
- Do not call the current LC telemetry a complete fingerprint mechanism until it is bound to the relevant container/connector records.

## Skills to use
- `brainstorming` before behavior changes; `graft` before source exploration; `focused-fix` only for implementation; `verification-before-completion` before completion claims; `git-commit` for commits; `ann-memcore-taskboard` for taskboard updates.

## Artifacts
- Repo: `I:\tools\ann`
- Core files: `ann/include/anchor_lettercube.h`, `ann/src/ann_search.c`, `ann/lettercube_rhombi.h`, `ann/include/chain_addr.h`, `ann/include/chain_headtail.h`
- Tests: `ann/tests/test_anchor_lettercube.c`, `ann/tests/alc_real_gate.c`, `ann/tests/test_chain_headtail.c`
- Experiment evidence: `ann/experiments/container-2026-10-30/REPORT.md`
- Taskboard: `task-t70e52fbf` and `task-t42563c1d` done; `task-t78c67779` pending. Explore task found no served HNSW; parked FAISS numbers are targets, not reproduced results.
