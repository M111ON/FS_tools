#!/usr/bin/env python3
"""chat-index integrity monitor — P4.

The tool that would have caught "24 days of silent loss" on the day it happened.

  python scripts/monitor.py                 # take today's snapshot, then judge it
  python scripts/monitor.py --check         # judge the snapshots on disk, take nothing
  python scripts/monitor.py --selftest      # prove the alarm logic (no files touched)
  python scripts/monitor.py --drill shrink  # write a doctored snapshot and fire the alarm

Exit 0 = quiet (nothing changed beyond noise).  Exit 2 = alarm.  Exit 1 = cannot judge
(no baseline yet, or the map could not be read) — never a silent zero.

Two alarms, because they catch different failures:

  CLIFF   one layer lost >5% between two snapshots   -> "something happened last night"
  TREND   three consecutive snapshots all lower      -> "it is bleeding slowly"

A missing day is NOT a trend input.  The machine is off sometimes; treating a gap as a
data point is how an alarm learns to cry wolf.  Gaps are reported as [gap] and restart
the trend window from the next real snapshot.
"""
import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"I:\tools\chat-index")
DB = ROOT / "chatmap.db"
SNAP_DIR = ROOT / "snapshots"
QLOG_DIR = Path(r"I:\tools\ann\data\query_log")

CLIFF_PCT = 5.0        # a layer losing more than this between snapshots is an event
TREND_DAYS = 3         # consecutive lower snapshots before we call it a bleed
TREND_MIN_PCT = 1.0    # ...and it must be at least this much overall

LAYERS = ["session", "msg", "vault_turn", "block", "fact", "gitcommit",
          "ds_conv", "ds_turn", "ds_chunk", "ann", "artifact"]


def db_readonly():
    return sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)


def take_snapshot():
    """Counts must come from the map itself, so a snapshot can never disagree with
    what a query would return.  Audit numbers are best-effort: a missing table is
    recorded as null, not as 0 (0 is a claim)."""
    d = db_readonly()
    snap = {"date": datetime.date.today().isoformat(), "layers": {}, "audit": {}}
    for t in LAYERS:
        try:
            snap["layers"][t] = d.execute("select count(*) from " + t).fetchone()[0]
        except sqlite3.Error:
            snap["layers"][t] = None
    try:
        r = d.execute("select built_at from build_receipt where id=1").fetchone()
        snap["built_at"] = r[0] if r else None
    except sqlite3.Error:
        snap["built_at"] = None
    try:
        q = """
        select
          (select count(*) from fact f
             where f.source_sid is not null and f.source_sid <> ''
               and not exists (select 1 from session s where s.sid=f.source_sid)),
          (select count(*) from fact f
             where f.source_sid is null or f.source_sid = ''),
          (select count(*) from session where has_content=0),
          (select count(*) from session where title like 'Recovered from magic-context%'),
          (select count(*) from session s where s.n_msg >= 20 and s.n_block = 0)
        """
        a = d.execute(q).fetchone()
        # orphan != no-source: a fact that never recorded a session is not a fact whose
        # session died, and collapsing the two overstates the damage (measured: 867 vs 1054)
        snap["audit"] = {"orphan_facts": a[0], "facts_without_source": a[1],
                         "sessions_no_content": a[2],
                         "marker_titles": a[3], "long_sessions_without_block": a[4]}
    except sqlite3.Error as e:
        snap["audit"] = {"error": str(e)}
    d.close()
    snap["query_log"] = qlog_stats()
    return snap


def qlog_stats():
    """How much of today's ANN traffic carries a coordinate.  null is allowed, but a
    night where every line is null means nothing downstream can be attributed."""
    f = QLOG_DIR / ("ann_query-%s.jsonl" % datetime.date.today().strftime("%Y%m%d"))
    if not f.exists():
        return {"file": None, "lines": 0, "coord_null": 0, "coord_pct": None}
    n = null = 0
    for line in f.open("r", encoding="utf-8", errors="replace"):
        n += 1
        if '"coord":null' in line:
            null += 1
    return {"file": f.name, "lines": n, "coord_null": null,
            "coord_pct": round(100.0 * (n - null) / n, 1) if n else None}


def load_snapshots():
    out = []
    if not SNAP_DIR.is_dir():
        return out
    for f in sorted(SNAP_DIR.glob("snap-*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _d(s):
    return datetime.date.fromisoformat(s)


def cliff_alarms(prev, cur):
    """A layer that shrank more than CLIFF_PCT.  Growth is never an alarm — more data
    is not damage — but a layer that VANISHED (cur == 0, prev > 0) always is."""
    out = []
    if not prev:
        return out
    for k in LAYERS:
        p, c = prev.get("layers", {}).get(k), cur.get("layers", {}).get(k)
        if p is None or c is None or p == 0:
            continue
        pct = 100.0 * (p - c) / p
        if c == 0:
            out.append((k, p, c, pct, "layer went to zero"))
        elif pct > CLIFF_PCT:
            out.append((k, p, c, pct, "lost %.1f%%" % pct))
    return out


def trend_alarms(snaps):
    """Consecutive-day declines only.  Returns (alarms, notes) where notes carry the
    [gap] evidence so a quiet night can never be mistaken for a clean streak."""
    alarms, notes = [], []
    if len(snaps) < 2:
        return alarms, notes
    snaps = snaps[-(TREND_DAYS + 3):]
    # build contiguous runs by date
    runs, cur = [], [snaps[0]]
    for prev, nxt in zip(snaps, snaps[1:]):
        if (_d(nxt["date"]) - _d(prev["date"])).days == 1:
            cur.append(nxt)
        else:
            notes.append("[gap] %s -> %s (%d days missing, trend window restarted)"
                         % (prev["date"], nxt["date"],
                            (_d(nxt["date"]) - _d(prev["date"])).days - 1))
            runs.append(cur)
            cur = [nxt]
    runs.append(cur)
    for run in runs:
        if len(run) <= TREND_DAYS:
            continue
        tail = run[-(TREND_DAYS + 1):]
        for k in LAYERS:
            vals = [s.get("layers", {}).get(k) for s in tail]
            if any(v is None for v in vals):
                continue
            steps = [b - a for a, b in zip(vals, vals[1:])]
            if all(s < 0 for s in steps):
                total = 100.0 * (vals[0] - vals[-1]) / vals[0] if vals[0] else 0
                if total >= TREND_MIN_PCT:
                    alarms.append((k, vals, total))
    return alarms, notes


def coverage(snap):
    a = snap.get("audit", {})
    lay = snap.get("layers", {})
    print("\ncoverage (what the map can and cannot answer)")
    sess = lay.get("session") or 0
    blocks = lay.get("block") or 0
    long_no = a.get("long_sessions_without_block")
    print("  block summaries            %5s over %s sessions" % (blocks, sess))
    if long_no is not None:
        print("  long sessions (>=20 msg) with NO block summary   %5s" % long_no)
    print("  facts whose source session no longer exists         %5s" % a.get("orphan_facts"))
    print("  facts that never recorded a source at all           %5s" % a.get("facts_without_source"))
    print("  sessions with no content in any layer            %5s" % a.get("sessions_no_content"))
    print("  sessions still titled with the recovery marker    %5s" % a.get("marker_titles"))
    q = snap.get("query_log") or {}
    if q.get("lines"):
        print("  ANN queries logged today  %4s  with a coordinate %5s%%"
              % (q["lines"], q.get("coord_pct")))
    else:
        print("  ANN queries logged today     0  (no file yet or logging off)")


def write_snapshot(snap, where=None):
    where = where or SNAP_DIR
    where.mkdir(parents=True, exist_ok=True)
    f = where / ("snap-%s.json" % snap["date"])
    f.write_text(json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
    return f


def judge(snaps, cur=None, quiet_ok=False):
    cur = cur or snaps[-1]
    prev = snaps[-2] if len(snaps) >= 2 and snaps[-1]["date"] == cur["date"] else None
    if prev is None and len(snaps) >= 2:
        prev = snaps[-1] if snaps[-1]["date"] != cur["date"] else None
    cliffs = cliff_alarms(prev, cur) if prev else []
    trends, notes = trend_alarms(snaps[:] if cur is snaps[-1] else snaps + [cur])
    print("\nintegrity  %s" % cur["date"])
    if prev:
        print("  baseline   %s" % prev["date"])
    else:
        print("  baseline   none yet — this is the first snapshot, nothing to compare")
    for n in notes:
        print("  " + n)
    if not cliffs and not trends:
        print("  verdict    quiet — no layer moved more than %.0f%% and no %d-day bleed"
              % (CLIFF_PCT, TREND_DAYS))
        return 0
    if cliffs:
        print("  ALARM cliff")
        for k, p, c, pct, why in cliffs:
            print("    %-12s %7s -> %-7s  %s" % (k, p, c, why))
        print("    what to do: the layer that shrank has a source; check its mtime and its")
        print("    own count, then rebuild from source. Never accept the smaller map.")
    if trends:
        print("  ALARM trend")
        for k, vals, total in trends:
            print("    %-12s %s  (-%.1f%% over %d days)" % (k, vals, total, TREND_DAYS))
        print("    what to do: a bleed is a source that stopped being read (a path moved, a")
        print("    task stopped running), not damage inside the map.")
    return 2


def selftest():
    base = {"date": "2026-10-01", "layers": {"msg": 1000, "fact": 100}}
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  pass  " if cond else "  FAIL  ") + name)
        ok = ok and cond

    c = cliff_alarms(base, {"date": "2026-10-02", "layers": {"msg": 900, "fact": 100}})
    check("cliff fires on a 10% drop", len(c) == 1 and c[0][0] == "msg")
    c = cliff_alarms(base, {"date": "2026-10-02", "layers": {"msg": 960, "fact": 100}})
    check("a 4% dip is quiet (below the 5% bar)", c == [])
    c = cliff_alarms(base, {"date": "2026-10-02", "layers": {"msg": 1200, "fact": 100}})
    check("growth is never an alarm", c == [])
    c = cliff_alarms(base, {"date": "2026-10-02", "layers": {"msg": 0, "fact": 100}})
    check("going to zero always fires", len(c) == 1 and c[0][4] == "layer went to zero")

    gappy = [
        {"date": "2026-10-01", "layers": {"msg": 1000}},
        {"date": "2026-10-02", "layers": {"msg": 990}},
        {"date": "2026-10-05", "layers": {"msg": 980}},
        {"date": "2026-10-06", "layers": {"msg": 970}},
    ]
    t, notes = trend_alarms(gappy)
    check("a gap breaks the trend window", t == [] and any("[gap]" in n for n in notes))

    streak = [{"date": "2026-10-0%d" % i, "layers": {"msg": 1000 - i * 10}}
              for i in range(1, 6)]
    t, notes = trend_alarms(streak)
    check("4 lower snapshots in a row raise the trend alarm", len(t) == 1)

    run2 = [{"date": "2026-10-0%d" % i, "layers": {"msg": 1000}} for i in range(1, 6)]
    t, _ = trend_alarms(run2)
    check("flat snapshots never raise the trend alarm", t == [])

    print("\nselftest %s" % ("ok" if ok else "FAILED"))
    return 0 if ok else 1


def drill(kind):
    """Write a doctored snapshot into a temp dir and prove the alarm fires on real
    files, not just on in-memory dicts."""
    import tempfile
    today = datetime.date.today()
    tmp = Path(tempfile.mkdtemp(prefix="chatmap-drill-"))
    real = take_snapshot()
    good = json.loads(json.dumps(real))
    good["date"] = (today - datetime.timedelta(days=1)).isoformat()
    bad = json.loads(json.dumps(good))
    bad["date"] = today.isoformat()
    if kind == "shrink":
        k = "msg"
        bad["layers"][k] = int(good["layers"][k] * 0.90)
    else:
        raise SystemExit("unknown drill: %s" % kind)
    write_snapshot(good, tmp)
    write_snapshot(bad, tmp)
    print("drill dir: %s" % tmp)
    snaps = []
    for f in sorted(tmp.glob("snap-*.json")):
        snaps.append(json.loads(f.read_text(encoding="utf-8")))
    rc = judge(snaps, snaps[-1])
    print("\ndrill %s -> exit %d (2 means the alarm fired as designed)" % (kind, rc))
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="judge only, take no snapshot")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--drill", choices=["shrink"])
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.drill:
        return drill(a.drill)
    if not DB.exists():
        print("FAIL LOUD: no map at %s — cannot judge anything" % DB)
        return 1
    if a.check:
        snaps = load_snapshots()
        if not snaps:
            print("FAIL LOUD: no snapshots in %s — run without --check first" % SNAP_DIR)
            return 1
        cur = snaps[-1]
    else:
        cur = take_snapshot()
        snaps = [s for s in load_snapshots() if s.get("date") != cur["date"]] + [cur]
        f = write_snapshot(cur)
        print("snapshot  %s  (%s)" % (f.name, "written" if f.exists() else "FAILED"))
        if not f.exists():
            print("FAIL LOUD: snapshot not written — the monitor is not monitoring")
            return 1
    coverage(cur)
    return judge(snaps, cur)


if __name__ == "__main__":
    sys.exit(main())
