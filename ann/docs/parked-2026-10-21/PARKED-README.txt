PARKED 2026-10-21 — disk relief for I:/ (the ANN session)

WHY
  I:/ was at 0.55 GB free (of 111 GB) and ann_sync's disk guard (600 MB)
  refused every rebuild, so the ANN index stopped tracking the memcore
  store. The park moved regenerable/offline artifacts OFF I: (never
  deleted — everything here is still intact).

WHAT IS HERE
  tmp-scratch/            scratch dirs + .o files from earlier ANN
                          benchmark sessions (bench_budget_recall.py,
                          bench_warm_recall.py, bench_anchor_proof.py).
                          REGENERABLE: the bench scripts create their
                          scratch dir themselves
                          (os.makedirs(args.scratch, exist_ok=True)) and
                          rebuild the index from the live corpus.
  ann_index.jsonl.bak-20261020-probes
                          the ANN corpus as it was on 2026-10-20 09:25
                          (before the tree-side work).
  context.db.pre-backfill-20261014-172607
  context.db.pre-hermes-sync-20261009-194443
                          magic-context database backups.
  fact_store.v2.sqlite3.pre-hermesdb-multidb-20261014-173956
                          memcore store backup.

RESULT
  I:/ free: 0.55 GB -> 5.1 GB. ann_sync runs again ("in sync"), live :8096
  serves 62,517 entries with stamps_live == entries.

TO RESTORE
  copy the wanted item back to its original path (see the list above).
