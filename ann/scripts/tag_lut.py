#!/usr/bin/env python3
"""tag_lut.py — the TAG chain: bake a frozen tag->zone LUT over the ANN index.

The ANN index rows carry a `file` tag (kind/term_hint) but nothing semantic.
The memcore store carries one: entries.pattern (12 values: v1-migrate,
chatpool, hermes, brainstorm, opencode, architecture, config_values,
constraints, freebuff, project_rules, naming, memo) — a tag that SPANS
sessions, so it groups material no file name can.

This script is that chain (per the chain rule: parts talk through
input/output only). It reads:
    fact_store.v2.sqlite3   entries(id, pattern, kind, term_hint)  (read-only)
    ann_index.jsonl          the rows the server serves
and writes:
    ann_tags.tsv             sid \t tag       (the LABEL sidecar, tree-side)
    tag_lut.json             the frozen LUT: tag -> zone id, counts, lineage
    tag_lut_report.json      forward / backward / clean deltas vs last bake

THE FIVE RULES, as implemented here
  accrual : the tag vocabulary is whatever the store says (pattern)
  bake    : freeze. A tag keeps its zone id FOREVER. A new tag appends the
            next free zone; a tag with no rows left is RETIRED (kept, never
            deleted, never renumbered) so old references stay valid.
  forward : a new row gets the zone of its tag automatically (this run)
  backward: a row whose tag CHANGED since the last bake is double-checked
            and reported (the "is it in the wrong zone" list)
  clean   : rows in the index that the store no longer has (orphans) are
            reported, and retired tags are listed

The sidecar is keyed by SID, never by row position: a sidecar that drifts
from the index is a mislabelled corpus, and a positional sidecar cannot be
verified (the S9 lesson from 2026-10-20). The server therefore resolves a
sid-keyed map and can PROVE the two agree (ann_tags_check).

Usage
    python tag_lut.py                  # bake (writes tsv + lut + report)
    python tag_lut.py --check          # verify the sidecar vs index+store
    python tag_lut.py --dry-run        # report only
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.environ.get("MEMCORE_STORE_DB",
                            r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3")
DEFAULT_INDEX = os.path.join(ROOT, "data", "memcore", "ann_index.jsonl")
OUT_DIR = os.path.join(ROOT, "data", "memcore")
TSV = os.path.join(OUT_DIR, "ann_tags.tsv")
LUT = os.path.join(OUT_DIR, "tag_lut.json")
REPORT = os.path.join(OUT_DIR, "tag_lut_report.json")


def read_index(path):
    sids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sids.append(json.loads(line)["sid"])
    return sids


def read_store(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute("SELECT id, pattern, kind FROM entries").fetchall()
    finally:
        con.close()
    return {r[0]: ((r[1] or "?").strip() or "?", r[2] or "?") for r in rows}


def load_lut(path=None):
    """The LUT of THIS bake. The path matters: reading the module constant
    would make any bake (even one into a test directory) inherit the live
    zones and write tags the live LUT never saw."""
    path = path or LUT
    if not os.path.exists(path):
        return {"zones": {}, "tags": {}, "bakes": []}
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return {"zones": {}, "tags": {}, "bakes": []}


def read_sidecar(path=TSV):
    """sid -> (tag, zone) — the third column is the frozen zone id."""
    if not os.path.exists(path):
        return {}
    m = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or "\t" not in line:
                continue
            parts = line.split("\t")
            m[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "-1")
    return m


def bake(db, index, out_tsv, out_lut, out_report, dry=False):
    sids = read_index(index)
    store = read_store(db)
    lut = load_lut(out_lut)
    prev_tags = lut.get("tags", {})          # tag -> {zone, count, ...}
    # the previous sidecar lives NEXT TO the one being written, so a bake
    # never touches another path's state (the constant PREV would have moved
    # the live sidecar during a test bake)
    prev_path = out_tsv + ".prev"
    prev_side = read_sidecar(prev_path) or read_sidecar(out_tsv)

    # ── accrual: the tag of a row is the store's pattern ───────────────
    rows = []
    orphans = []
    unknown = []
    for sid in sids:
        if sid not in store:
            orphans.append(sid)
            continue
        tag = store[sid][0]
        if tag == "?":
            unknown.append(sid)
        rows.append((sid, tag))

    counts = Counter(t for _, t in rows)

    # ── bake: freeze. existing tag -> its zone, forever ─────────────────
    zones = {}
    tags = dict(prev_tags)
    for t, e in prev_tags.items():
        zones[e.get("zone", -1)] = t
    next_zone = (max([z for z in zones] + [-1]) + 1)
    new_tags = []
    for t in sorted(counts):
        if t in tags:
            continue
        z = next_zone
        next_zone += 1
        tags[t] = {"zone": z, "first_bake": None, "last_bake": None,
                   "count": 0, "retired_at": None}
        new_tags.append((t, z))

    # ── forward: the current row set gets the zone of its tag ──────────
    for t, n in counts.items():
        e = tags[t]
        e["count"] = n
        e["last_bake"] = None
        e["retired_at"] = None
    # a tag with no rows left counts ZERO (not its last count) — otherwise it
    # would never look empty and could never be retired
    for t, e in tags.items():
        if t not in counts:
            e["count"] = 0

    # ── backward: rows whose tag changed since the last bake ───────────
    changed = []
    for sid, tag in rows:
        old = prev_side.get(sid)
        old_tag = old[0] if isinstance(old, tuple) else old
        if old_tag is not None and old_tag != tag:
            changed.append({"sid": sid, "from": old_tag, "to": tag})

    bake_id = f"b{len(lut.get('bakes', [])) + 1:04d}"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for t, e in tags.items():
        if e.get("first_bake") is None:
            e["first_bake"] = bake_id
        e["last_bake"] = bake_id
        if e["count"] == 0 and e.get("retired_at") is None:
            e["retired_at"] = bake_id
    retired = [t for t, e in tags.items() if e["count"] == 0]

    report = {
        "bake_id": bake_id,
        "baked_at": now,
        "index_rows": len(sids),
        "tagged_rows": len(rows),
        "orphan_rows": len(orphans),
        "untagged_rows": len(unknown),
        "tags": len(counts),
        "new_tags": [{"tag": t, "zone": z, "rows": counts[t]} for t, z in new_tags],
        "retired_tags": retired,
        "backward_changed": changed[:200],
        "backward_changed_total": len(changed),
        "orphan_sample": orphans[:50],
    }

    if dry:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    # ── write: the sidecar (sid-keyed) + the frozen LUT + the report ───
    os.makedirs(os.path.dirname(out_tsv), exist_ok=True)
    if os.path.exists(out_tsv):
        try:
            os.replace(out_tsv, prev_path)
        except OSError:
            pass
    # the sidecar carries the FROZEN zone id as a third column, so the server
    # can serve zone:<id> without reading the LUT json: a zone id never
    # changes once baked, so the sidecar stays valid across re-bakes.
    tmp = out_tsv + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for sid, tag in rows:
            f.write(f"{sid}\t{tag}\t{tags[tag]['zone']}\n")
    os.replace(tmp, out_tsv)

    lut["bake_id"] = bake_id
    lut["baked_at"] = now
    lut["tags"] = tags
    lut["zones"] = {str(e["zone"]): {"tag": t, "rows": e["count"],
                                     "retired": e["count"] == 0}
                    for t, e in tags.items()}
    lut["bakes"] = (lut.get("bakes", []) + [{"id": bake_id, "at": now,
                                             "rows": len(rows),
                                             "tags": len(counts)}])[-50:]
    tmp = out_lut + ".tmp"
    json.dump(lut, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, out_lut)

    tmp = out_report + ".tmp"
    json.dump(report, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, out_report)

    print(f"bake {bake_id}: {len(rows)} rows, {len(counts)} tags, "
          f"{len(new_tags)} new, {len(retired)} retired, "
          f"{len(changed)} changed, {len(orphans)} orphans")
    print(f"  sidecar -> {out_tsv}")
    print(f"  lut     -> {out_lut}")
    return 0


def check(db, index, out_tsv, out_lut=None):
    """the sidecar must cover exactly the index rows, with the store's tags"""
    sids = read_index(index)
    store = read_store(db)
    side = read_sidecar(out_tsv)
    idx_set = set(sids)
    side_set = set(side)
    missing = [s for s in sids if s not in side]
    extra = [s for s in side if s not in idx_set]
    wrong = []
    lut = load_lut(out_lut)
    for s, tv in side.items():
        tag = tv[0] if isinstance(tv, tuple) else tv
        zone = tv[1] if isinstance(tv, tuple) else "-1"
        if s in store and store[s][0] != tag:
            wrong.append((s, tag, store[s][0]))
        exp = lut.get("tags", {}).get(tag, {}).get("zone")
        if exp is not None and str(exp) != str(zone):
            wrong.append((s, "zone:" + str(zone), "zone:" + str(exp)))
    print(f"index rows: {len(sids)}  sidecar rows: {len(side)}")
    print(f"  missing from sidecar: {len(missing)} {missing[:5]}")
    print(f"  in sidecar, not in index: {len(extra)} {extra[:5]}")
    print(f"  tag disagrees with store: {len(wrong)} {wrong[:5]}")
    ok = not missing and not extra and not wrong
    print("RESULT:", "SIDECAR MATCHES INDEX+STORE" if ok else "MISMATCH")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--tsv", default=TSV)
    ap.add_argument("--lut", default=LUT)
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check(a.db, a.index, a.tsv, a.lut)
    return bake(a.db, a.index, a.tsv, a.lut, a.report, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
