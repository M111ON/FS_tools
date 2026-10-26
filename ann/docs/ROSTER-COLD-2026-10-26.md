# Roster cold path — warm measured, cold bounded by arithmetic (2026-10-26)

Owner decision: **no reboot, no purge measurement.** The claim ships as
"warm measured, cold bounded by arithmetic", and the roster is made
**cold-safe by design** so there is nothing left to measure.

## Why the old attempt failed

`EmptyWorkingSet` returned `true` but the index pages never left RAM — Windows
moved them to the standby list, and the next read still came from memory. Every
"cold" number produced that way would have been a warm number with a cold label.
That is why the cold column in the handoff was zeros.

## The arithmetic

| quantity | value |
|---|---|
| roster memberships (live) | 127,694 rows |
| payload | `127,694 x 4 B` = **510,776 B = 0.49 MiB** |
| index jsonl (what actually must be read cold) | **258 MB** |
| ratio | 0.19 % |

Even at a 100 MB/s disk the whole roster is ~5 ms, against 100–2500 ms for the
index. The roster **cannot** be the cold cost, at any disk speed, on any boot.
That is the whole claim — and it needs no clock to be true.

What used to be untrue was the *design*: the roster lived only in heap, so a
cold start paid a `2 x n` rebuild (twice every row) and its pages could not be
evicted without losing the work. That is fixed.

## What changed (file-backed roster)

- Written once next to the anchors: `<anchors minus .bin>-roster.bin`
  (override `ANN_ROSTER_PATH`). Layout: 7-word header
  `{magic 'RSTR', ver, K, n, rows, ord_hash, anchors_hash}`, then `K` row
  counts, then the payload (`uint32` row indices, walk order).
- Every later boot **maps** it read-only (`/health` → `roster.src: "mmap"`), so a
  cold fault is a 4 KB page read and nothing is rebuilt. The kernel owns the
  pages and may evict them freely — that is the cold-safety.
- The tie is content, not time: `ord_hash` (FNV over the order perm) and
  `anchors_hash` (FNV over the 12 KB anchor table). A retrained table or a
  different order invalidates the file instead of serving a wrong walk.
- `/health` now carries `roster{on,doors,rows,bytes,src}`; the diag line logs
  `[roster] ready src=mmap|built ... cost=<ms>`, so a rebuild is always visible.

## Gate: `tests/test_roster_cold.py` (9 checks, in `run_tests.py`)

| check | proves |
|---|---|
| C1 | first boot builds and writes the sidecar |
| C2 | sidecar size = 28 + 4·K + 4·rows exactly |
| C3 | restart maps it (`src: mmap`) |
| C4 | mapped roster returns the same hits, `walk_hash`, `items_scored` |
| C5 | a mapped restart does not rewrite the file (mtime unchanged = no rebuild) |
| C6 | a wrong `n` in the header is refused and rebuilt, answer unchanged |
| C7 | a regrow keeps a valid roster (rebuild only when the tie changes) |
| C8 | deleting the sidecar just rebuilds — never an error |

## What is still not measured (and is fine)

A true cold *latency* number (reboot, first query). It is deliberately not
pursued: with the roster file-backed, the remaining cold cost is the index load,
which the roster does not touch — the number would describe the loader, not this
feature.
