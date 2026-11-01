#!/usr/bin/env python3
"""build_index.py — memcore (fact_store.v2) -> ann base index (ann_index.jsonl).

Reads the memcore SQLite store (entries + embeddings) and writes the ANN base
index in the format ann_search's parser expects:

    {"sid":"...","file":"...","npast":N,"dim":384,"vec":[...]}

No re-embedding: memcore already stores multilingual-e5-small vectors at dim
384, and the ANN sidecar serves the SAME model, so the spaces match (measured
cosine 0.955 — close enough for routing; re-embed only if exactness matters).

anchor/anchor2 are NOT written here, and the server does NOT rewrite this file
(it did in an older design; anchors now live in ann_anchors.bin, and
POST /v1/state/regrow keeps its own state file). So this script is the only
writer of the live index, and it also writes the binary sidecar
(<out>.f16) in the same run, right after the swap, so the two can never drift.

Usage:
    python build_index.py [--db PATH] [--out PATH] [--limit N] [--max-chars 400]
        [--route-db PATH] [--routes-out PATH]
"""
import argparse, json, os, sqlite3, struct, sys, time

import numpy as np

DEFAULT_DB = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(_ROOT, "data", "memcore", "ann_index.jsonl")
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
    # The sidecar is filled from the SAME rounded values that go into the JSONL,
    # so sidecar row i is byte-for-byte the JSONL row i after a float32 cast.
    first_dim = (len(rows[0]["vec"]) // 4) if rows else 0
    X = np.empty((len(rows), first_dim), dtype=np.float32)
    meta_rows = []
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
            # anchor/anchor2 are NOT known here (the server's regrow adds them
            # to the .stamp overlay), so they stay -1 in the sidecar exactly as
            # they are absent from the JSONL.
            meta_rows.append({"sid": sid, "file": file, "npast": npast,
                              "dim": dim, "anchor": -1, "anchor2": -1})
            if dim == first_dim:
                X[n] = rec["vec"]
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

    # ── Seed generation (chain identity layer) ──────────────────
    # Generate 64-bit seeds for each row using chain_seedgen algorithm.
    # Space ID = model + version + dim (prevents cross-model collisions).
    import struct as _struct
    space_hash = 0
    for c in "multilingual-e5-small":
        space_hash = (space_hash * 31 + ord(c)) & 0xFFFFFFFF
    space_hash = (space_hash ^ (1 * 0x9E3779B9)) & 0xFFFFFFFF
    space_hash = (space_hash ^ (384 * 0x517CC1B7)) & 0xFFFFFFFF

    FNV_OFF = 14695981039346656037
    FNV_PRIME = 1099511628211
    M64 = 0xFFFFFFFFFFFFFFFF

    def _fnv1a(data_bytes):
        h = FNV_OFF
        for b in data_bytes:
            h ^= b
            h = (h * FNV_PRIME) & M64
        return h

    def _mix64(x):
        x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
        x = ((x ^ (x >> 27)) * 0x94D049BB133111B9) & M64
        return (x ^ (x >> 31)) & M64

    seeds = []
    for r in rows:
        vec = struct.unpack("<%df" % (len(r["vec"]) // 4), r["vec"])
        vec_bytes = struct.pack("<%df" % len(vec), *vec)
        h = _fnv1a(vec_bytes)
        h ^= (space_hash * 0x9E3779B97F4A7C15) & M64
        h ^= (1 * 0x517CC1B727220A95) & M64
        h = _mix64(h)
        major_a = h % 24
        cube_route = (h >> 8) % 48
        inner_ori = (h >> 11) & 0xFFF
        outer_ori = (h >> 23) & 0xFFF
        checksum = (h >> 35) & 0x1FFF
        seed = 0
        seed |= (major_a & 0x1F) << 59
        seed |= (1 & 0xFFFF) << 43
        seed |= (cube_route & 0x3F) << 37
        seed |= (inner_ori & 0xFFF) << 25
        seed |= (outer_ori & 0xFFF) << 13
        seed |= (checksum & 0x1FFF)
        seeds.append(seed)

    seed_path = a.out + ".seeds"
    seed_tmp = seed_path + ".tmp"
    with open(seed_tmp, "wb") as f:
        f.write(b"ANNSEED\x00")  # magic (8 bytes)
        f.write(_struct.pack("<I", 2))  # version 2 (fixed FNV + bias)
        f.write(_struct.pack("<Q", len(seeds)))  # n_rows
        f.write(_struct.pack("<I", space_hash))  # space_hash
        for seed in seeds:
            f.write(_struct.pack("<Q", seed))
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(60):
        try:
            os.replace(seed_tmp, seed_path)
            break
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(1.0)
    print(f"seeds: {len(seeds)} written to {seed_path} (space_hash=0x{space_hash:08x}, v2)")

    # The binary sidecar is written HERE, in the same run, and never by a
    # separate job: two writers would drift and the guard would only tell us
    # after the fact. src_jsonl is the file just swapped in, so the size,
    # mtime_ns and fingerprint all describe the bytes the reader will see.
    sidecar = a.out + ".f16"
    if X.shape[0] != n:
        print(f"sidecar: SKIPPED - {n - X.shape[0]} rows have a different dim")
    else:
        from index_sidecar import write_index
        meta = write_index(sidecar, X, "f16", src_jsonl=a.out, meta=meta_rows)
        print(f"wrote {meta['n_rows']}x{meta['dim']} f16 -> {sidecar} "
              f"fp={meta['jsonl_fp']}")

    if a.route_db:
        sys.path.insert(0, _ROOT)
        from export_producer_routes import export as export_routes
        route_n, route_digest = export_routes(a.out, a.route_db, a.routes_out)
        print(f"wrote {route_n} route records -> {a.routes_out} sha256={route_digest}")
    else:
        print("route sidecar: disabled (no --route-db; baseline index behavior preserved)")


if __name__ == "__main__":
    main()
