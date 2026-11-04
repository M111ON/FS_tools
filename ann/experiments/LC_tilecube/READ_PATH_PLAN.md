# LC_tilecube Read Path — Plan

Write Path (ingest_fingerprint.py) and Persistence (gen_manifest.py, commit_evidence.py) are frozen.
Read Path is a NEW read-only layer on top. It must not modify Geometry v1.0 or Persistence.

## Scope (memory #8223)
Support, with fresh-process exact equality:
1. read(address)
2. search(branch, query)
3. reconstruct(item_id)
4. list_addresses(branch)

## Tasks
- T1. Define Address + ReadPath skeleton (load storage read-only).
- T2. Implement list_addresses(branch).
- T3. Implement read(address) with letters + reverse item mapping.
- T4. Implement reconstruct(item_id) including vectors.
- T5. Implement search(branch, query) vector ranking.
- T6. Fresh-process exact equality tests.
- T7. Run full suite (existing tests still pass).

## Non-goals
- No mutation of branches/relations/fruit_data.
- No new geometry.
- No new persistence backend.
