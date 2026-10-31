# DB migrations (memcore)

Scripts that change a live data store. Each one is kept here, in git, because a
migration that only exists on one disk cannot be reproduced — a backup lets you
restore, it does not let you re-run.

## d3_migrate_brainstorm_alias.py — 2026-10-31

Folds the duplicate `brainstorm` tag into `entry_alias` and deletes the
`brainstorm` rows.

**Why the target was changed.** The 2026-10-14 handoff said to delete
`v1-migrate` because `brainstorm` duplicated it. Measured the other way round:

```
brainstorm   9,918 rows, 40.85 MB   all hashes present in v1-migrate
v1-migrate  21,316 rows, 55.03 MB   of which 11,398 rows exist ONLY here
```

`brainstorm` is a subset of `v1-migrate`, not the reverse. Deleting
`v1-migrate` would have destroyed 11,398 rows that exist nowhere else.

**Evidence the subset is real, not a whitespace artefact** (`d3_verify_hash.py`
in the session that ran it; the numbers are repeated here because that script
was not kept):

- sha256 over the **raw bytes** of `content` (`text_factory=bytes`, no decoding,
  no replacement) — trailing newline, whitespace and invalid UTF-8 all count as
  differences;
- 9,918 distinct brainstorm hashes, all 9,918 present in `v1-migrate`, 0 missing;
- multiplicity 1: each hash matches exactly one `v1-migrate` row, so the remap
  is unambiguous;
- shared row ids = 0, so the link can only be content, never id;
- control: hashing after whitespace normalisation gives 9,912 — *fewer* than
  exact. Exact hashing is the stricter test and still yields a full subset.

**Result**

```
entries      63,982 -> 54,064   (-9,918)
embeddings   63,982 -> 54,064
entries_fts  63,982 -> 54,064   (triggers entries_ai/ad/au keep it in sync)
entry_alias  +9,918 rows, alias_tag='brainstorm', all resolving to v1-migrate
orphan embeddings 0 · dangling alias rows 0 · fts integrity-check ok
file size unchanged (466.9 MB), 15,541 free pages, no VACUUM by design
```

**Two hazards the obvious schema misses**

1. `entries.id` is `TEXT`, so `entry_alias.entry_id` must be `TEXT`. An
   `INTEGER` column stores no key that can ever match.
2. `embeddings` has **no foreign key**, so deleting `entries` leaves 9,918
   orphan embeddings (14.53 MB). The script deletes them explicitly.
   (`entries_fts` does have triggers, so it needs no rebuild.)

**Rollback**

```
restore entries + embeddings from brainstorm_backup_20261031.sqlite3
DELETE FROM entry_alias WHERE alias_tag='brainstorm';
```

**Policy (owner, 2026-10-31):** `entry_alias` is **read-only**. Writes must name
the primary tag (`v1-migrate`); the alias is a snapshot and must not diverge.

**Status: dead data.** Nothing in memcore selects by `pattern` at all today, so
no reader uses `entry_alias`. It is a ledger, not a feature. Making a search
honour the alias is NEW work, not a pending task — and the table must not be
deleted as "unused", it is the only record of the merge.

Run:

```
python ann/scripts/d3_migrate_brainstorm_alias.py            # plan only
python ann/scripts/d3_migrate_brainstorm_alias.py --commit   # do it
```

The plan-only mode is safe to run any time: it opens the DB read-only and prints
what it would do.
