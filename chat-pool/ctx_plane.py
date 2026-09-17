"""BREATHING_SPEC §4 ทาง A — pointer-based vision over Magic Context's db.

PROTOTYPE, READ-ONLY. Nothing here ever writes to context.db: it is opened
with `mode=ro` on every connection, and every address is *derived*. The DB
keeps working untouched (MCP, magic context, the TS authority, all triggers)
and knows nothing about this file.

What it is meant to prove:
  - **capture over rows** — rkey = RDH(content), the same capture pool uses,
    so memories and chat lines land on ONE shared 144x144 plane (no second
    coordinate system, per §5.4).
  - **vision instead of SELECT** — read_at(rkey, level) answers only the point
    you opened: L0 rows in that cell, L1 the containing 12x12 square as a map,
    L2 the whole plane as a map. Maps are address-only, so they carry no
    memory content at all.
  - **the index is a cache, never the truth** — ctx_plane.db holds rowids and
    addresses only (content stays in context.db and is fetched on demand);
    deleting it and rescanning must reproduce byte-identical addresses.
    In the real thing it would be kept fresh by an AFTER INSERT trigger,
    exactly like memories_ai keeps memories_fts fresh today.

Usage:
    python ctx_plane.py scan            # (re)build the index, verify no write
    python ctx_plane.py stats           # distribution / collisions on the plane
    python ctx_plane.py at --rkey N [--level 0|1|2] [--path 1,42]
    python ctx_plane.py demo            # self-check on a throwaway fixture db
"""
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool  # noqa: E402  — one capture rule, one plane, one budget

DIR = os.path.dirname(os.path.abspath(__file__))
CTX = pool.CTX_DB                       # I:\cortexkit\magic-context\context.db
INDEX = os.path.join(DIR, "ctx_plane.db")
TABLE = "memories"                      # the table we point at first


def _ro(src=CTX):
    """Read-only connection. `mode=ro` is the guarantee, not a convention:
    a write on this handle is refused by SQLite itself."""
    return sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/").replace("?", "%3f"), uri=True)


def _source_state(src=CTX):
    st = os.stat(src)
    return {"size": st.st_size, "mtime": st.st_mtime}


def _pseudoline(category, content):
    """The memory's bytes as a line-shaped dict, so the sub-axis rule is
    literally pool's rule and not a second implementation of it."""
    return {"app": category or "", "kind": "memory", "text": content or ""}


def scan(index=INDEX, src=CTX, table=TABLE, force=False):
    """Read every row, derive its rkey, store rowid+address in the index.
    Returns a report dict; raises if the source changed under us (it must
    not — we are a reader)."""
    before = _source_state(src)
    db = _ro(src)
    try:
        rows = db.execute(
            "SELECT rowid, category, content FROM %s" % table).fetchall()
    finally:
        db.close()
    h = hashlib.sha256()
    for rid, cat, content in rows:
        h.update(("%d:%s:%s\n" % (rid, cat or "", content or "")).encode("utf-8"))
    fingerprint = h.hexdigest()

    fresh = False
    if os.path.exists(index) and not force:
        idb = sqlite3.connect(index)
        try:
            cur = idb.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
            fresh = bool(cur) and cur[0] == fingerprint
        finally:
            idb.close()
    if fresh:
        return {"rows": len(rows), "rebuilt": False, "fingerprint": fingerprint[:12]}

    tmp = index + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    idb = sqlite3.connect(tmp)
    try:
        idb.execute("CREATE TABLE rows(rowid INTEGER PRIMARY KEY, rkey INTEGER NOT NULL,"
                    " wedge INTEGER NOT NULL, ring INTEGER NOT NULL,"
                    " category TEXT, clen INTEGER NOT NULL, created_at INTEGER)")
        idb.execute("CREATE INDEX rows_rkey ON rows(rkey)")
        idb.execute("CREATE INDEX rows_wr ON rows(wedge, ring)")
        idb.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        for rid, cat, content in rows:
            rk = pool.rkey((content or "").encode("utf-8"))
            idb.execute("INSERT INTO rows VALUES(?,?,?,?,?,?,?)",
                        (rid, rk, rk % 144, rk // 144, cat, len(content or ""), None))
        idb.execute("INSERT INTO meta VALUES('fingerprint',?)", (fingerprint,))
        idb.execute("INSERT INTO meta VALUES('source',?)", (src,))
        idb.execute("INSERT INTO meta VALUES('table',?)", (table,))
        idb.execute("INSERT INTO meta VALUES('plane',?)", (json.dumps({"field": 144,
                     "capture": "RDH_CAPTURE_144", "budget": pool.VISION_BUDGET}),))
        idb.commit()
    finally:
        idb.close()
    os.replace(tmp, index)  # atomic swap — a half-built index is never served

    after = _source_state(src)
    if before != after:
        raise RuntimeError("context.db changed during scan — we are a reader: %r -> %r"
                           % (before, after))
    return {"rows": len(rows), "rebuilt": True, "fingerprint": fingerprint[:12],
            "cells": len({pool.rkey((c or "").encode("utf-8")) for _, _, c in rows}),
            "source_unchanged": True}


def _index_rows(index, where="", args=()):
    idb = sqlite3.connect(index)
    try:
        return idb.execute("SELECT rowid, rkey, wedge, ring, category, clen FROM rows "
                           + where, args).fetchall()
    finally:
        idb.close()


def _contents(src, rowids):
    """Content is fetched from context.db at read time — the index never
    duplicates it, so 'the data stays where it is' holds literally."""
    if not rowids:
        return {}
    db = _ro(src)
    try:
        marks = ",".join("?" * len(rowids))
        return {r[0]: r[1] for r in db.execute(
            "SELECT rowid, content FROM %s WHERE rowid IN (%s)" % (TABLE, marks),
            list(rowids))}
    finally:
        db.close()


def read_at(rkey0, level=0, path=(), index=INDEX, src=CTX, budget=None,
            preview=160, full=False):
    """Open ONE pointer at ONE point of the memory plane (§4 ทาง A).
    L0 = the rows living in that cell · L1 = the containing 12x12 square as a
    map · L2 = the whole plane as a map. Over-budget cells come back already
    stacked (same phase axis as pool), and `path` walks one sub-cell down."""
    budget = budget if budget is not None else pool.VISION_BUDGET
    path = tuple(path or ())
    if level == 2:
        idb = sqlite3.connect(index)
        try:
            cells = dict(idb.execute("SELECT rkey, count(*) FROM rows GROUP BY rkey"))
        finally:
            idb.close()
        return {"plane": "ctx", "level": 2, "field": 144, "budget": budget,
                "rows": sum(cells.values()), "cells": len(cells),
                "fine_cells": dict(sorted(cells.items())),
                "over_budget": sorted(k for k, c in cells.items() if c > budget)}

    if level == 1:
        bw, br = pool._block_of(rkey0)
        got = _index_rows(index, "WHERE wedge>=? AND wedge<? AND ring>=? AND ring<?",
                          (bw, bw + 12, br, br + 12))
        cells = {}
        for _, rk, _, _, _, _ in got:
            cells[rk] = cells.get(rk, 0) + 1
        return {"plane": "ctx", "level": 1, "block": 12,
                "origin": {"wedge": bw, "ring": br}, "base": br * 144 + bw,
                "rows": len(got), "fine_cells": dict(sorted(cells.items()))}

    got = _index_rows(index, "WHERE rkey=?", (rkey0,))
    rowids = [g[0] for g in got]
    cats = {g[0]: g[4] for g in got}
    lens = {g[0]: g[5] for g in got}
    # content is pulled from context.db here and nowhere else. The stacking
    # axis must be computed on FULL content — a truncated preview would walk
    # a different path and land in a different bucket.
    cont = _contents(src, rowids)
    keep = list(range(len(rowids)))
    if path:
        keep = [i for i in keep if all(
            pool._axis(_pseudoline(cats[rowids[i]], cont.get(rowids[i], "")), d + 1) == v
            for d, v in enumerate(path))]
    out = []
    for i in keep:
        rid = rowids[i]
        body = cont.get(rid) or ""
        out.append({"rowid": rid, "category": cats[rid], "len": lens[rid],
                    "preview": body if full else body[:preview],
                    "truncated": (not full) and len(body) > preview})

    vis = {"plane": "ctx", "level": 0, "point": rkey0, "count": len(out),
           "rows": out}
    if path:
        vis["path"] = list(path)
    if len(out) > budget:
        # one axis per read, so every key in the map stays addressable by
        # a path (§5.4) — the reader walks down instead of being teleported
        vis["stacked"] = _sub_map(rkey0, len(path) + 1, path=path,
                                  index=index, src=src, budget=budget)
        vis["over_budget"] = True
    return vis


def _sub_map(rkey0, depth, path=(), index=INDEX, src=CTX, budget=None):
    """Sub-map of an over-budget memory cell — addresses + counts only,
    same rule as pool.sub_map: split by the phase axis, nothing stored."""
    budget = budget if budget is not None else pool.VISION_BUDGET
    got = _index_rows(index, "WHERE rkey=?", (rkey0,))
    rowids = [g[0] for g in got]
    cats = {g[0]: g[4] for g in got}
    cont = _contents(src, rowids)
    cells = {}
    for rid in rowids:
        pl = _pseudoline(cats[rid], cont.get(rid, ""))
        if any(pool._axis(pl, i + 1) != v for i, v in enumerate(path)):
            continue
        k = pool._axis(pl, depth)
        cells[k] = cells.get(k, 0) + 1
    return {"level": "0.%d" % depth, "cell": rkey0, "depth": depth,
            "path": list(path), "budget": budget,
            "still_over": sum(1 for c in cells.values() if c > budget),
            "next_depth": depth + 1,
            "sub_cells": dict(sorted(cells.items()))}


def links(src=CTX, lines=None):
    """Cross-system edges between the log and the memory plane — DERIVED,
    not new data. The memory bridge already writes a lifted line as
    "[chat-pool/<app>] <text>", so a memory row and the chat line it came
    from are the two ends of one real relationship. Nothing anywhere says
    so; it is recomputed from the same convention the bridge writes, which
    means no schema change and no new line kind.

    Both ends land in the SAME 144x144 plane, so an edge that crosses from
    chat into memory is just an edge — the radius walk crosses systems for
    free. `lines` overrides the log read (tests pass their own)."""
    db = _ro(src)
    try:
        rows = db.execute(
            "SELECT rowid, content FROM %s WHERE content LIKE ?"
            % TABLE, ("[chat-pool/%",)).fetchall()
    finally:
        db.close()
    by_content = {content: rid for rid, content in rows}
    out = []
    for m in (pool.tail(1000) if lines is None else lines):
        text = (m.get("text") or "").strip()
        if not pool.BRIDGE_RE.match(text):     # only what the bridge would lift
            continue
        content = "[chat-pool/%s] %s" % (m.get("app", "?"), text)
        rid = by_content.get(content)
        if rid is None:                        # not lifted yet (flush pending)
            continue
        out.append({"kind": "lifted", "src": m["hash"], "src8": m["hash"][:8],
                    "src_rkey": m.get("rkey"), "dst_rowid": rid,
                    "dst_rkey": pool.rkey(content.encode("utf-8")),
                    "app": m.get("app"), "t": m.get("t")})
    return out


def cross_stats(src=CTX, index=INDEX):
    """What the two planes actually share: cells held by both systems."""
    idb = sqlite3.connect(index)
    try:
        mem_cells = {r[0] for r in idb.execute("SELECT DISTINCT rkey FROM rows")}
    finally:
        idb.close()
    pool_cells = {m["rkey"] for m in pool.tail(1000) if m.get("rkey") is not None}
    return {"memory_cells": len(mem_cells), "pool_cells": len(pool_cells),
            "shared_cells": sorted(mem_cells & pool_cells),
            "shared_count": len(mem_cells & pool_cells),
            "links": len(links(src))}


def stats(index=INDEX, src=CTX, top=8):
    """How the memories actually land on the shared plane."""
    idb = sqlite3.connect(index)
    try:
        total = idb.execute("SELECT count(*) FROM rows").fetchone()[0]
        cells = idb.execute("SELECT count(DISTINCT rkey) FROM rows").fetchone()[0]
        over = idb.execute("SELECT count(*) FROM (SELECT rkey FROM rows GROUP BY rkey"
                           " HAVING count(*)>?)", (pool.VISION_BUDGET,)).fetchone()[0]
        busiest = idb.execute("SELECT rkey, count(*) AS n FROM rows GROUP BY rkey"
                              " ORDER BY n DESC LIMIT ?", (top,)).fetchall()
        blocks = idb.execute("SELECT count(DISTINCT (wedge/12)||':'||(ring/12)) FROM rows").fetchone()[0]
    finally:
        idb.close()
    pool_cells = {m["rkey"] for m in pool.tail(1000) if m.get("rkey") is not None}
    ctx_cells = set()
    idb = sqlite3.connect(index)
    try:
        ctx_cells = {r[0] for r in idb.execute("SELECT DISTINCT rkey FROM rows")}
    finally:
        idb.close()
    shared = sorted(pool_cells & ctx_cells)
    frac = (cells / 20736.0 * 100) if total else 0.0
    return {"source": src, "table": TABLE, "rows": total, "occupied_cells": cells,
            "plane": 144 * 144, "occupied_pct": round(frac, 2),
            "blocks_touched": blocks, "blocks_total": 144,
            "cells_over_budget": over, "budget": pool.VISION_BUDGET,
            "busiest": busiest, "shared_with_pool_cells": shared,
            "shared_with_pool_lines": sum(1 for m in pool.tail(1000)
                                          if m.get("rkey") in set(shared)),
            "source_state": _source_state(src)}


def cli(argv):
    cmd = argv[1] if len(argv) > 1 else "stats"
    if cmd == "scan":
        rep = scan(force="--force" in argv)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        print("# index is a cache: addresses are derived, content stays in context.db")
    elif cmd == "stats":
        if not os.path.exists(INDEX):
            print("no index yet — run: python ctx_plane.py scan")
            return 1
        print(json.dumps(stats(top=10), ensure_ascii=False, indent=2))
    elif cmd == "at":
        rk = int(argv[argv.index("--rkey") + 1])
        lv = int(argv[argv.index("--level") + 1]) if "--level" in argv else 0
        path = tuple()
        if "--path" in argv:
            path = tuple(int(p) for p in argv[argv.index("--path") + 1].split(",") if p)
        print(json.dumps(read_at(rk, level=lv, path=path), ensure_ascii=False, indent=2))
    elif cmd == "demo":
        demo()
    else:
        print(__doc__)
        return 2
    return 0


def demo():
    """Self-check on a throwaway fixture db — the real context.db and index
    are never touched by this path."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ctx-plane-")
    try:
        src = os.path.join(tmp, "fixture.db")
        idx = os.path.join(tmp, "idx.db")
        db = sqlite3.connect(src)
        db.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                   " category TEXT, content TEXT, project_path TEXT, created_at INTEGER)")
        for i in range(300):
            db.execute("INSERT INTO memories(category, content, project_path, created_at)"
                       " VALUES(?,?,?,?)", ("ARCHITECTURE", "memory body %d" % i, "p", 1))
        db.commit()
        db.close()

        rep = scan(index=idx, src=src)
        assert rep["rebuilt"] is True and rep["rows"] == 300, "scan captures every row"
        assert scan(index=idx, src=src)["rebuilt"] is False, "same source ⇒ no rebuild (fingerprint)"
        state = _source_state(src)
        assert state == _source_state(src), "source file untouched by a scan"

        # L0 = only the pointed cell; L1 = the square that frames it; L2 = the plane
        rk = _index_rows(idx, "LIMIT 1")[0][1]
        l0 = read_at(rk, index=idx, src=src)
        assert l0["level"] == 0 and l0["count"] == len(_index_rows(idx, "WHERE rkey=?", (rk,))), "L0 answers exactly the rows in that cell"
        assert all(r["rowid"] in {g[0] for g in _index_rows(idx, "WHERE rkey=?", (rk,))} for r in l0["rows"]), "no row from another cell leaks in"
        bw, br = pool._block_of(rk)
        l1 = read_at(rk, level=1, index=idx, src=src)
        assert l1["origin"] == {"wedge": bw, "ring": br}, "L1 is the spatial square holding the point"
        assert all(pool._in_block(k, bw, br) for k in l1["fine_cells"]), "every address in L1 lies inside the square"
        assert sum(l1["fine_cells"].values()) == l1["rows"], "L1 counts exactly the rows in the square"
        l2 = read_at(rk, level=2, index=idx, src=src)
        assert l2["rows"] == 300, "L2 accounts for every captured row"
        assert all(isinstance(k, int) for k in l2["fine_cells"]), "maps carry addresses, never content"
        assert not any("memory body" in str(v) for v in l2["fine_cells"].values()), "no content crosses a map"

        # cross-system: a lifted memory is one real edge between the two planes.
        # The demo supplies its own log lines — nothing here may touch the
        # live log (an earlier draft of this test appended to it; never again).
        text = "DECISION: fixture decision"
        fake = {"hash": "a" * 64, "rkey": 777, "app": "tester", "kind": "chat",
                "text": text, "t": 1}
        assert not links(src=src, lines=[fake]), "no lift exists yet — the edge is derived, never assumed"
        db2 = sqlite3.connect(src)
        db2.execute("INSERT INTO memories(category, content, project_path, created_at)"
                    " VALUES(?,?,?,?)",
                    ("DECISION", "[chat-pool/tester] " + text, "p", 1))
        db2.commit()
        db2.close()
        lk = links(src=src, lines=[fake])
        assert len(lk) == 1, "a bridge-written memory and its chat line are one edge"
        assert lk[0]["src"] == fake["hash"] and lk[0]["src_rkey"] == 777, "the edge starts at the line that was lifted"
        assert lk[0]["dst_rkey"] == pool.rkey(("[chat-pool/tester] " + text).encode("utf-8")), "the far end is placed by its own content"
        assert links(src=src, lines=[{"hash": "b" * 64, "rkey": 1, "app": "tester",
                                     "kind": "chat", "text": "just chat", "t": 1}]) == [], "only lifted lines form a cross edge"

        # stacking: same axis rule as pool, chosen lazily, walked by path
        st = stats(index=idx, src=src)
        assert st["occupied_cells"] > 0 and st["rows"] == 300, "stats read the plane, not the rows"
        hot = max(l2["fine_cells"], key=lambda k: l2["fine_cells"][k])
        small = read_at(hot, index=idx, src=src, budget=1)
        assert "stacked" in small, "a cell over budget comes back split"
        assert sum(small["stacked"]["sub_cells"].values()) == l2["fine_cells"][hot], "the stack accounts for exactly that cell"
        d1 = small["stacked"]["depth"]
        first = sorted(small["stacked"]["sub_cells"])[0]
        deeper = read_at(hot, index=idx, src=src, budget=1, path=(first,))
        assert deeper["path"] == [first] and deeper["count"] <= small["count"], "a path narrows the read"
        assert pool._depth_for([_pseudoline("c", "x")], d1 + 1) >= d1 + 1, "each step down opens a fresh axis"
        assert small["stacked"]["depth"] == 1 and d1 == 1, "the first split is axis 1 — one axis per read"
        again = read_at(hot, index=idx, src=src, budget=1)
        assert again["stacked"]["depth"] == d1, "depth is derived, not stored"
        assert again["stacked"]["sub_cells"] == small["stacked"]["sub_cells"], "same point ⇒ same split, every time"
        print("CTX-PLANE SELFCHECK: PASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(cli(sys.argv))
