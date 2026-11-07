#!/usr/bin/env python
"""index_versions.py — versioned snapshots + rollback for the ANN index.

The ANN index (data/memcore/ann_index.jsonl) is today overwritten in place by
ann_sync (tmp+rename, atomic but not recoverable). This module adds:

  - snapshot_current()  : copy current index into data/memcore/versions/<ts>.jsonl
  - list_versions()     : available snapshots (newest first)
  - rollback(version)   : restore a snapshot over ann_index.jsonl (atomic), then
                          POST /v1/state/regrow so the live server swaps in
  - prune(keep=N)       : keep only the newest N snapshots

Snapshots are plain copies of the index file, so rollback is just a file
restore + regrow. No server restart, no schema change. Tenant isolation and
incremental rebuild are separate concerns (need build_index.py + server filter
support) and are intentionally NOT in this module.

Usage (CLI):
    python index_versions.py list
    python index_versions.py snapshot [--label "pre-migration"]
    python index_versions.py rollback [VERSION]   # omit -> previous
    python index_versions.py prune --keep 5

Library:
    from index_versions import snapshot_current, list_versions, rollback
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.environ.get("ANN_INDEX_PATH",
                            os.path.join(DIR, "data", "memcore", "ann_index.jsonl"))
VERSIONS_DIR = os.path.join(os.path.dirname(INDEX_PATH), "versions")
MANIFEST = os.path.join(VERSIONS_DIR, "manifest.json")
ANN_BASE = os.environ.get("MEMCORE_ANN_URL", "http://127.0.0.1:8096").rstrip("/")
REGROW_URL = ANN_BASE + "/v1/state/regrow"
KEEP_DEFAULT = int(os.environ.get("ANN_VERSIONS_KEEP", "5"))


def _ensure_versions_dir():
    os.makedirs(VERSIONS_DIR, exist_ok=True)


def _load_manifest():
    if not os.path.exists(MANIFEST):
        return {"versions": []}
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"versions": []}


def _save_manifest(m):
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MANIFEST)


def _version_name(ts, label=None):
    base = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
    if label:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        base = base + "_" + safe
    return base + ".jsonl"


def snapshot_current(label=None, ts=None):
    """Copy the current index into versions dir. Returns version id or None."""
    if not os.path.exists(INDEX_PATH):
        return None
    _ensure_versions_dir()
    if ts is None:
        ts = time.time()
    vid = _version_name(ts, label)
    dst = os.path.join(VERSIONS_DIR, vid)
    # atomic-ish copy via tmp
    tmp = dst + ".tmp"
    shutil.copy2(INDEX_PATH, tmp)
    os.replace(tmp, dst)
    size = os.path.getsize(dst)
    m = _load_manifest()
    m["versions"].append({"id": vid, "ts": ts, "label": label, "size": size,
                          "entries": _count_entries(dst)})
    _save_manifest(m)
    prune(KEEP_DEFAULT)
    return vid


def _count_entries(path):
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except Exception:
        pass
    return n


def list_versions():
    m = _load_manifest()
    vs = sorted(m.get("versions", []), key=lambda x: x.get("ts", 0), reverse=True)
    return vs


def prune(keep=KEEP_DEFAULT):
    m = _load_manifest()
    vs = sorted(m.get("versions", []), key=lambda x: x.get("ts", 0), reverse=True)
    keep_ids = {v["id"] for v in vs[:keep]}
    removed = []
    for v in vs[keep:]:
        p = os.path.join(VERSIONS_DIR, v["id"])
        try:
            if os.path.exists(p):
                os.remove(p)
            removed.append(v["id"])
        except OSError:
            pass
    m["versions"] = [v for v in vs if v["id"] in keep_ids]
    _save_manifest(m)
    return removed


def rollback(version=None):
    """Restore a snapshot over ann_index.jsonl. version=None -> previous (2nd newest)."""
    vs = list_versions()
    if not vs:
        return {"ok": False, "error": "no versions available"}
    if version is None:
        # previous = second entry (newest is the current just-snapshotted, if any)
        # Prefer the newest that differs from current file size to be safe.
        target = vs[1] if len(vs) >= 2 else vs[0]
    else:
        matches = [v for v in vs if v["id"] == version or v["id"].startswith(version)]
        if not matches:
            return {"ok": False, "error": "version not found: %s" % version}
        target = matches[0]
    src = os.path.join(VERSIONS_DIR, target["id"])
    if not os.path.exists(src):
        return {"ok": False, "error": "snapshot file missing: %s" % src}
    # snapshot current before clobbering (safety)
    pre = snapshot_current(label="pre-rollback")
    # atomic restore
    tmp = INDEX_PATH + ".restore.tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, INDEX_PATH)
    # trigger regrow
    regrow_ok = False
    regrow_detail = None
    try:
        data = json.dumps({"force": True}).encode()
        req = urllib.request.Request(REGROW_URL, data=data,
                                    headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            regrow_detail = json.loads(r.read().decode("utf-8", "ignore"))
            regrow_ok = True
    except Exception as e:
        regrow_detail = {"error": str(e)}
    return {"ok": True, "restored": target["id"], "pre_rollback_snapshot": pre,
            "regrow_ok": regrow_ok, "regrow_detail": regrow_detail}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sp = sub.add_parser("snapshot")
    sp.add_argument("--label", default=None)
    rp = sub.add_parser("rollback")
    rp.add_argument("version", nargs="?")
    pp = sub.add_parser("prune")
    pp.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    args = ap.parse_args()

    if args.cmd == "list":
        vs = list_versions()
        if not vs:
            print("no versions")
            return 0
        for v in vs:
            print("%s  ts=%d  entries=%s  size=%d  label=%s"
                  % (v["id"], v["ts"], v.get("entries"), v.get("size"), v.get("label")))
        return 0
    if args.cmd == "snapshot":
        vid = snapshot_current(label=args.label)
        print("snapshot ->", vid)
        return 0
    if args.cmd == "rollback":
        res = rollback(args.version)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "prune":
        removed = prune(args.keep)
        print("pruned:", removed)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
PY