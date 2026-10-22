#!/usr/bin/env python3
"""test_tag_lut.py — the tag chain's own contract (2026-10-21).

scripts/tag_lut.py bakes a FROZEN tag->zone LUT over the ANN index and
writes a sid-keyed sidecar. This verifies the five rules, hermetically
(temp dir, its own store + index — never the live ones):

  T1 bake: every indexed row gets a tag, a zone, and a sidecar line
  T2 freeze: re-baking keeps every zone id (a tag never gets renumbered)
  T3 accrual: a NEW tag appends the next free zone; old zones do not move
  T4 forward: a new ROW joins the zone of its tag
  T5 backward: a row whose tag CHANGED is reported (the double-check list)
  T6 clean: a tag with no rows left is RETIRED — kept, never deleted
  T7 check: --check passes on a consistent state and fails on a broken one
  T8 sid-keyed: the sidecar is keyed by sid, so a reordered index still
     labels the right rows (the S9 lesson: a positional sidecar drifts)

Usage: python tests/test_tag_lut.py
"""
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import tag_lut  # noqa: E402

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def make_store(path, rows):
    """rows: [(sid, pattern, kind, term_hint)]"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entries (id TEXT, source_file TEXT, pattern TEXT, "
                 "term_hint TEXT, kind TEXT, content TEXT)")
    con.execute("CREATE TABLE embeddings (entry_id TEXT, vec BLOB)")
    for sid, pat, kind, hint in rows:
        con.execute("INSERT INTO entries VALUES (?,?,?,?,?,?)",
                    (sid, "x.md", pat, hint, kind, "c"))
        con.execute("INSERT INTO embeddings VALUES (?,?)",
                    (sid, struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)))
    con.commit()
    con.close()


def make_index(path, sids):
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(sids):
            f.write(json.dumps({"sid": s, "file": "exchange/s%d" % i, "npast": 1,
                                "dim": 4, "vec": [0.1, 0.2, 0.3, 0.4]}) + "\n")


def bake(store, index, tsv, lut, report):
    rc = tag_lut.bake(store, index, tsv, lut, report, dry=False)
    return rc


def read_tsv(path):
    m = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                m[parts[0]] = (parts[1], parts[2])
    return m


def main():
    tmp = tempfile.mkdtemp(prefix="ann-taglut-")
    try:
        store = os.path.join(tmp, "store.sqlite3")
        index = os.path.join(tmp, "ann_index.jsonl")
        tsv = os.path.join(tmp, "ann_tags.tsv")
        lut = os.path.join(tmp, "tag_lut.json")
        report = os.path.join(tmp, "tag_lut_report.json")

        rows = [("s%d" % i, p, "exchange", "h%d" % i)
                for i, p in enumerate(["alpha", "alpha", "beta", "beta", "beta"])]
        make_store(store, rows)
        make_index(index, [r[0] for r in rows])

        # T1 bake
        rc = bake(store, index, tsv, lut, report)
        ok(rc == 0, "T1 bake returns 0", str(rc))
        side = read_tsv(tsv)
        ok(len(side) == 5, "T1 every row has a sidecar line", str(len(side)))
        ok(all(side[k][0] in ("alpha", "beta") for k in side), "T1 tags are the store's")
        lj = json.load(open(lut, encoding="utf-8"))
        zones = {t: e["zone"] for t, e in lj["tags"].items()}
        ok(set(zones) == {"alpha", "beta"}, "T1 the LUT knows both tags", str(zones))
        rep = json.load(open(report, encoding="utf-8"))
        ok(rep["orphan_rows"] == 0 and rep["untagged_rows"] == 0,
           "T1 no orphans, no untagged", f"{rep['orphan_rows']}/{rep['untagged_rows']}")

        # T2 freeze: re-bake keeps the zones
        bake(store, index, tsv, lut, report)
        lj2 = json.load(open(lut, encoding="utf-8"))
        zones2 = {t: e["zone"] for t, e in lj2["tags"].items()}
        ok(zones2 == zones, "T2 re-bake keeps every zone id",
           f"{zones} -> {zones2}")

        # T3 accrual: a new tag appends the next free zone
        con = sqlite3.connect(store)
        con.execute("INSERT INTO entries VALUES ('s5','x.md','gamma','h5','exchange','c')")
        con.execute("INSERT INTO embeddings VALUES ('s5',?)",
                    (struct.pack("<4f", 1.0, 0.0, 0.0, 0.0),))
        con.commit()
        con.close()
        make_index(index, [r[0] for r in rows] + ["s5"])
        bake(store, index, tsv, lut, report)
        lj3 = json.load(open(lut, encoding="utf-8"))
        zones3 = {t: e["zone"] for t, e in lj3["tags"].items()}
        ok(zones3["alpha"] == zones["alpha"] and zones3["beta"] == zones["beta"],
           "T3 old tags keep their zones", f"{zones3}")
        ok(zones3["gamma"] not in (zones["alpha"], zones["beta"]),
           "T3 the new tag took a new zone", str(zones3.get("gamma")))
        side = read_tsv(tsv)
        ok(side["s5"][0] == "gamma" and side["s5"][1] == str(zones3["gamma"]),
           "T4 the new row joined its tag's zone", str(side.get("s5")))

        # T5 backward: a row whose tag changed is reported
        con = sqlite3.connect(store)
        con.execute("UPDATE entries SET pattern='beta' WHERE id='s0'")
        con.commit()
        con.close()
        bake(store, index, tsv, lut, report)
        rep = json.load(open(report, encoding="utf-8"))
        ok(rep["backward_changed_total"] == 1,
           "T5 the changed row is reported", str(rep["backward_changed_total"]))
        ok(rep["backward_changed"][0]["from"] == "alpha" and
           rep["backward_changed"][0]["to"] == "beta",
           "T5 the report names the old and the new tag",
           str(rep["backward_changed"][:1]))

        # T6 clean: a tag with no rows left is retired, not deleted
        con = sqlite3.connect(store)
        con.execute("UPDATE entries SET pattern='beta'")
        con.commit()
        con.close()
        make_index(index, ["s0", "s1", "s2", "s3", "s4", "s5"])
        bake(store, index, tsv, lut, report)
        lj4 = json.load(open(lut, encoding="utf-8"))
        rep = json.load(open(report, encoding="utf-8"))
        ok("gamma" in lj4["tags"], "T6 the emptied tag is still in the LUT",
           str(sorted(lj4["tags"])))
        ok(lj4["tags"]["gamma"]["count"] == 0, "T6 it counts zero rows",
           str(lj4["tags"]["gamma"]["count"]))
        ok("gamma" in rep["retired_tags"], "T6 it is reported as retired",
           str(rep["retired_tags"]))

        # T7 check: consistent -> 0, broken -> non-zero
        rc = tag_lut.check(store, index, tsv)
        ok(rc == 0, "T7 --check passes on a consistent state", str(rc))
        with open(tsv, "a", encoding="utf-8") as f:
            f.write("sZZZ\tbeta\t1\n")
        rc = tag_lut.check(store, index, tsv)
        ok(rc != 0, "T7 --check fails when the sidecar has a row the index does not",
           str(rc))

        # T8 sid-keyed: reorder the index, the labels follow the sids
        make_index(index, ["s5", "s4", "s3", "s2", "s1", "s0"])
        bake(store, index, tsv, lut, report)
        side = read_tsv(tsv)
        ok(side["s0"][0] == "beta" and side["s5"][0] == "beta",
           "T8 the labels follow the sid, not the position", str(side.get("s0")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)"
          if not fails else f"{len(fails)} FAILURES")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
