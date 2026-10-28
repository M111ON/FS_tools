# chat-index integrity monitor (P4) — 2026-10-29

**The failure it exists for:** the vault export stopped on 2026-09-30 and nobody noticed for 25
days; 65 sessions vanished with no journal; 41 sessions lost their real titles in one recovery run
on 2026-10-04. Each was found months later by accident, by asking a question and not liking the
answer. A daily number that cannot go quiet when it should, and cannot stay quiet when it shouldn't,
is cheaper than that.

```
python I:\tools\chat-index\scripts\monitor.py            # snapshots today, then judges it
python I:\tools\chat-index\scripts\monitor.py --check    # judge only
python I:\tools\chat-index\scripts\monitor.py --selftest # prove the alarm logic
python I:\tools\chat-index\scripts\monitor.py --drill shrink
```

Exit `0` quiet · `2` alarm · `1` cannot judge (no map, no baseline) — never a silent zero.

## What is measured

Per day, `snapshots/snap-YYYYMMDD.json`:

- **15 layer counts** read from `chatmap.db` itself (session, msg, vault_turn, block, fact,
  gitcommit, ds_conv, ds_turn, ds_chunk, ann, artifact, metric, doc, project, src) — so a snapshot
  can never disagree with what a query would return
- **audit needles**: orphan facts, facts with no source at all, sessions with no content in any
  layer, sessions still carrying the recovery marker, long sessions (≥20 msg) with no block
- **ANN log health**: lines written today and what share of them carry a `coord`
- **build receipt**: `built_at` plus the map's byte size, so "the numbers did not move" and "the
  map was not rebuilt" can be told apart

## The two alarms

| alarm | rule | catches |
|---|---|---|
| **cliff** | any layer lost **>5%** vs the previous snapshot; a layer going to **0** always fires | "something happened last night" — a path moved, a task failed, a source was rewritten |
| **trend** | **3 consecutive** snapshots each lower, ≥1% overall | "it is bleeding slowly" — a source that stopped being read while nothing looked broken |

Growth never alarms. A 4% dip does not alarm (measured noise from live sessions ending). **A missing
day is not a data point**: the run reports `[gap] … trend window restarted` and starts the trend
window again from the next real snapshot, because a machine that was off is not evidence of decay
and an alarm that cries wolf gets ignored — which is exactly how 25 days passed unnoticed.

## Hardening after the audit

Three holes found by reading the code back, all of the same family — a night that could not be read
looking exactly like a night where nothing happened:

| hole | what it did | fix |
|---|---|---|
| locked / unreadable map | every layer counted `None`, judge printed **quiet**, exit 0 | `take_snapshot` probes the DB first and returns `status: unreadable`; `main` exits 1 and **writes no snapshot** (a null snapshot would also hide the night from the judge) |
| a `None` inside a trend window | that layer was skipped, so one bad night silently froze the streak forever | a null **cuts** the run like a gap; the streak that starts after it is still judged |
| blind layers | `metric`, `doc`, `project`, `src` were never counted | added to `LAYERS` (15 total), and the snapshot now records the map's size + write time, so "numbers did not move" and "the map was never rebuilt" can be told apart |

## Gates, run for real

```
$ monitor.py --selftest
  pass  cliff fires on a 10% drop
  pass  a 4% dip is quiet (below the 5% bar)
  pass  growth is never an alarm
  pass  going to zero always fires
  pass  a gap breaks the trend window
  pass  4 lower snapshots in a row raise the trend alarm
  pass  flat snapshots never raise the trend alarm
  pass  a null night cuts the window instead of freezing it
  pass  judging an all-null snapshot exits 1, never quiet
  pass  an unreadable map refuses to become a snapshot
  selftest ok

$ monitor.py --drill shrink          # real files in a temp dir, msg -10%
  ALARM cliff
    msg            40409 -> 36368    lost 10.0%
  drill shrink -> exit 2

$ (file-level trend drill: 1000, 990, 985, 975 on four consecutive days)
  ALARM trend
    msg          [1000, 990, 985, 975]  (-2.5% over 3 days)
  exit 2

$ (file-level gap drill: 10-01, 10-02, 10-05, 10-06 all declining)
  [gap] 2026-10-02 -> 2026-10-05 (2 days missing, trend window restarted)
  verdict quiet
  exit 0
```

First real snapshot, 2026-10-29, quiet and consistent with `chatmap.py audit`:

```
block summaries              956 over 2237 sessions
long sessions (>=20 msg) with NO block summary     154
facts whose source session no longer exists           867   (= audit: 867)
facts that never recorded a source at all             187   (= audit: 187)
sessions with no content in any layer                2
sessions still titled with the recovery marker       14   (was 67; 53 rebuilt from first message)
numeric cells indexed                         208826
repo docs indexed                                223
ANN queries logged today    83  with a coordinate  61.4%
```

`coord_pct` dropped from 100% because today's file mixes real MCP searches with the bench run that
deliberately sends `coord` on only half its queries to prove null is logged, not dropped.

That agreement is the point: two independent code paths printing the same 867 and 187 means neither
is inventing a number. The first version of this monitor printed **1054** for orphans because it
counted `source_sid IS NULL` as "session died" — a fact that never recorded a source is not a fact
whose source was lost, and confusing the two overstates the damage by 21%. Fixed before shipping.

## Schedule

`chat-index-monitor` — daily **05:40**, after `chat-index-refresh` (05:10, 90–190 s). Logon type
**S4U**, `StartWhenAvailable`, battery allowed: it runs whether or not anyone is logged in and
catches up after a power-off. Verified by running the task for real: `Last Result 0`, output in
`monitor.log`.

## Honest limits

- It compares **counts**, not content. A session whose text was silently truncated keeps its row
  count and passes. Content drift needs a hash of a sample; not built.
- The trend alarm needs four consecutive days of history. Before that it can only report cliffs, and
  it says so instead of guessing.
- The `[gap]` note is printed but not alarmed: a machine off for a week is expected, and paging on
  it would train the reader to ignore the output.
- `coord_pct` measures how many ANN searches were attributable. It will read 100% only while every
  caller sends a coord — MCP now can (`coord=` on `ann_search`), other clients may not.
- Nothing here writes to a source. Snapshot + log only.
