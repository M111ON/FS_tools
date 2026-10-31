#!/usr/bin/env python3
"""build_index.py — memcore (fact_store.v2) -> ann base index (ann_index.jsonl).

Reads the memcore SQLite store (entries + embeddings) and writes the ANN base
index in the format ann_search's parser expects:

    {"sid":"...","file":"...","npast":N,"dim":384,"vec":[...]}

No re-embedding: memcore already stores multilingual-e5-small vectors at dim
384, and the ANN sidecar serves the SAME model, so the spaces match (measured
cosine 0.955 — close enough for routing; re-embed only if exactness matters).

anchor/anchor2 are NOT written here — the server's POST /v1/state/regrow
trains the centroids, posts every entry into its top-2 buckets, and rewrites
this file with the anchor fields added (same split as upstream).

Usage:
    python build_index.py [--db PATH] [--out PATH] [--limit N] [--max-chars 400]
        [--route-db PATH] [--routes-out PATH]
"""
import argparse, json, os, sqlite3, struct, sys, time

DEFAULT_DB = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(_ROOT, "data", "ann_index.jsonl")
DEFAULT_ROUTES = os.path.join(_ROOT, "data", "ann_routes.jsonl")


def sanitize(t: str, max_chars: int) -> str:
    """Match upstream reindex_docs.py: NFKC + a few ascii folds, then clip.
    The e5-small tokenizer 500s on some compat chars; NFKC is meaning-preserving."""
    import unicodedata
    t = unicodedata.normalize("NFKC", t)
    for _bad, _good in (
        ("∉", " not in "), ("∋", " contains "), ("∖", " minus "),
        ("∝", " proportional to "), ("∤", " does not divide "),
        ("≅", " ~= "), ("⋀", " AND "), ("⋁", " OR "),
        ("⏭", " next "), ("⏸", " pause "), ("⟺", " <=> "),
        ("⬠", " pentagon "),
    ):
        if _bad in t:
            t = t.replace(_bad, _good)
    return t[:max_chars]


def clean_field(s: str, cap: int) -> str:
    """The regrow hook rewrites the index with raw fprintf (no JSON escaping),
    so a `"` or `\\` in a field corrupts the file. Sanitize before it lands."""
    s = str(s)
    s = s.replace("\\", "/").replace('"', "'").replace("\r", " ").replace("\n", " ")
    s = "".join(c if 32 <= ord(c) < 0x10000 else " " for c in s)
    return s[:cap]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--max-chars", type=int, default=400)
    ap.add_argument("--route-db", default="", help="optional producer route metadata SQLite DB")
    ap.add_argument("--routes-out", default=DEFAULT_ROUTES)
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    q = ("SELECT e.id, e.kind, e.term_hint, e.content, m.vec "
         "FROM entries e JOIN embeddings m ON m.entry_id = e.id")
    if a.limit:
        q += f" LIMIT {int(a.limit)}"
    rows = con.execute(q).fetchall()
    print(f"memcore rows: {len(rows)}")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    n = 0
    dims = {}
    t0 = time.time()
    with open(tmp, "w", encoding="utf-8") as out:
        for r in rows:
            vec = struct.unpack("<%df" % (len(r["vec"]) // 4), r["vec"])
            dim = len(vec)
            dims[dim] = dims.get(dim, 0) + 1
            sid = clean_field(r["id"] or "", 56)
            file = clean_field(r["kind"] or "?", 48)
            if r["term_hint"]:
                file += "/" + clean_field(r["term_hint"], 48)
            npast = len(r["content"] or "")
            rec = {"sid": sid, "file": file, "npast": npast, "dim": dim,
                   "vec": [round(x, 6) for x in vec]}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            if n % 5000 == 0:
                print(f"  {n}/{len(rows)}  ({time.time()-t0:.1f}s)")
    # ATOMIC SWAP, WITH A RETRY (2026-10-22): on Windows the destination can
    # be held open for a moment by a scanner (Defender walks the freshly
    # written 258 MB file) and os.replace then fails with WinError 5
    # "Access is denied" — measured live: the index rebuild aborted at the
    # swap and the .tmp was left behind (258 MB of wasted disk, and the
    # index would never refresh -> the H1 hole). A short retry turns the
    # race into a delay.
    for attempt in range(60):
        try:
            os.replace(tmp, a.out)
            break
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(1.0)
    print(f"wrote {n} entries -> {a.out}")
    print(f"dims: {dims}")
    print(f"elapsed {time.time()-t0:.1f}s")
    if a.route_db:
        sys.path.insert(0, _ROOT)
        from export_producer_routes import export as export_routes
        route_n, route_digest = export_routes(a.out, a.route_db, a.routes_out)
        print(f"wrote {route_n} route records -> {a.routes_out} sha256={route_digest}")
    else:
        print("route sidecar: disabled (no --route-db; baseline index behavior preserved)")


if __name__ == "__main__":
    main()
