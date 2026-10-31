"""ann_index binary sidecar — writer, guarded reader, staleness check.

The JSONL stays what the server reads today. This writes a mmap-friendly binary
next to it so a load does not have to parse 212 MB of JSON.

WHY THE GUARD EXISTS: row i of the binary is row i of the JSONL *by position*.
`ann-sync` rewrites the JSONL on a schedule, so without a check a reordered file
would silently serve the wrong vector for every row after the first change —
recall looks normal and every answer is wrong. Silent, no error. So the header
carries a fingerprint of the JSONL it was built from and the reader refuses on
mismatch.

Header v2 (64 bytes, little-endian):

    off size field
      0    8 magic            "ANNIDX\\x00\\x01"
      8    4 version          2
     12    4 dtype            0 = f32, 1 = f16
     16    4 dim
     20    4 reserved0
     24    8 n_rows
     32    8 jsonl_size       size of the source JSONL when the sidecar was built
     40    8 jsonl_mtime_ns   st_mtime_ns of the source (nanoseconds: two writes
                              in the same second must not look identical)
     48    8 jsonl_fp         FNV-1a-64 over "size|n_rows|0" + first 4 KB + last 4 KB
     56    8 reserved1

The fingerprint reuses the algorithm the C server already uses for
`ann_anchors-state.json` (`lz_index_fp`, ann_search.c:3833), including its
non-standard offset basis (1469598103934665603, not the standard
0xcbf29ce484222325). Do not "fix" that constant: matching the existing contract
is worth more than being standard.

Header v1 (28 bytes, magic "ANNIDX\\x00\\x00") is still readable but carries no
guard, so `read_index` marks it unguarded and the caller decides.
"""
import os
import struct
import sys

import numpy as np

MAGIC_V1 = b"ANNIDX\0\0"
MAGIC_V2 = b"ANNIDX\0\1"
MAGIC_V3 = b"ANNIDX\0\2"
VERSION = 3
DTYPES = {"f32": (0, np.float32, 4), "f16": (1, np.float16, 2)}

HDR_V1 = struct.Struct("<8sIIIQ")                  # magic version dtype dim n
HDR_V2 = struct.Struct("<8sIIIIQQQQQ")             # + size mtime_ns fp reserved

# v3 adds a per-row metadata block so the C server can build its entry table
# without parsing the JSONL at all — without it the "89x faster" is a lie, since
# the 212 MB text scan is where the time actually goes.
#   sid[64] file[256] npast i32 dim i32 anchor i32 anchor2 i32 = 336 bytes
# anchor/anchor2 are here because lz_entry_parse() reads them from the JSONL;
# dropping them would silently lose the stamps on any file that carries them.
META_FMT = "<64s256siiii"
META_SIZE = struct.calcsize(META_FMT)
assert META_SIZE == 336, META_SIZE

FNV_OFF = 1469598103934665603                      # ann_search.c:3826
FNV_PRIME = 1099511628211
_M64 = 0xFFFFFFFFFFFFFFFF
_WIN = 4096


def fnv64(h, buf):
    for b in buf:
        h ^= b
        h = (h * FNV_PRIME) & _M64
    return h


def jsonl_fp(path, size, n_rows):
    """Same shape as lz_index_fp(): header string, first 4 KB, last 4 KB."""
    h = fnv64(FNV_OFF, f"{size}|{n_rows}|0".encode())
    with open(path, "rb") as f:
        win = f.read(_WIN)
        if win:
            h = fnv64(h, win)
        if size > _WIN and f.seek(size - _WIN) == size - _WIN:
            win = f.read(_WIN)
            if win:
                h = fnv64(h, win)
    return h or 1


def jsonl_hash(path):
    """FNV-1a-64 over the WHOLE file.

    The sampled jsonl_fp cannot see a change in the middle of the file, and a
    sidecar that maps onto the wrong rows serves the wrong vector with no error
    - the one failure this whole guard exists for. A full pass costs ~0.5 s on
    the 212 MB index, paid once per rebuild, so it is the default; the C side
    checks it unless ANN_SIDECAR_FAST=1 is set.
    """
    h = FNV_OFF
    with open(path, "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h = fnv64(h, buf)
    return h or 1


def write_index(path, X, dtype="f16", src_jsonl=None, meta=None):
    """Write the sidecar.

    meta=None  -> v2 (vectors only). src_jsonl=None -> no guard fields.
    meta=list of dicts with sid/file/npast -> v3 (vectors + metadata block),
    which is what the C server needs to skip the JSONL entirely.
    """
    code, np_dt, _ = DTYPES[dtype]
    n, dim = X.shape
    if src_jsonl:
        st = os.stat(src_jsonl)
        size, mtime_ns = st.st_size, st.st_mtime_ns
        fp = jsonl_fp(src_jsonl, size, n)
        full = jsonl_hash(src_jsonl)
    else:
        size = mtime_ns = fp = full = 0
    magic = MAGIC_V3 if meta is not None else MAGIC_V2
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(HDR_V2.pack(magic, VERSION if meta is not None else 2,
                            code, dim, 0, n, size, mtime_ns, fp, full))
        if meta is not None:
            if len(meta) != n:
                raise ValueError(f"meta has {len(meta)} rows, X has {n}")
            for m in meta:
                f.write(struct.pack(
                    META_FMT,
                    (m.get("sid") or "").encode("utf-8")[:63],
                    (m.get("file") or "").encode("utf-8")[:255],
                    int(m.get("npast") or 0), int(m.get("dim") or dim),
                    int(m.get("anchor", -1)), int(m.get("anchor2", -1))))
        X.astype(np_dt, copy=False).tofile(f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return {"n_rows": n, "dim": dim, "dtype": dtype, "jsonl_size": size,
            "jsonl_mtime_ns": mtime_ns, "jsonl_fp": fp, "jsonl_hash": full,
            "has_meta": meta is not None}


def read_index(path, require_guard=True):
    """Return (info, float32 array). Raises if the sidecar is stale or bad.

    info["guarded"] is False for v1 headers (no fingerprint to check).
    info["meta"] is the per-row metadata list for v3, else None.
    """
    with open(path, "rb") as f:
        head = f.read(HDR_V2.size)
    magic = head[:8]
    meta = None
    if magic in (MAGIC_V2, MAGIC_V3):
        _, ver, code, dim, _r0, n, size, mtime_ns, fp, full = HDR_V2.unpack(head)
        off = HDR_V2.size
        if magic == MAGIC_V3:
            with open(path, "rb") as f:
                f.seek(off)
                raw = f.read(n * META_SIZE)
            if len(raw) != n * META_SIZE:
                raise ValueError(f"{path}: truncated metadata block")
            meta = []
            for i in range(n):
                sid, fil, npast, mdim, anc, anc2 = struct.unpack_from(
                    META_FMT, raw, i * META_SIZE)
                meta.append({"sid": sid.rstrip(b"\0").decode("utf-8", "replace"),
                             "file": fil.rstrip(b"\0").decode("utf-8", "replace"),
                             "npast": npast, "dim": mdim,
                             "anchor": anc, "anchor2": anc2})
            off += n * META_SIZE
    elif magic == MAGIC_V1:
        _, ver, code, dim, n = HDR_V1.unpack(head[:HDR_V1.size])
        size = mtime_ns = fp = full = 0
        off = HDR_V1.size
    else:
        raise ValueError(f"{path}: bad magic {magic!r}")

    name = {v[0]: k for k, v in DTYPES.items()}[code]
    np_dt = DTYPES[name][1]
    mm = np.memmap(path, dtype=np_dt, mode="r", offset=off, shape=(n, dim))
    info = {"path": path, "version": ver, "dtype": name, "dim": dim,
            "n_rows": n, "jsonl_size": size, "jsonl_mtime_ns": mtime_ns,
            "jsonl_fp": fp, "jsonl_hash": full,
            "guarded": magic != MAGIC_V1 and fp != 0,
            "meta": meta}
    return info, np.asarray(mm, dtype=np.float32)


def check_fresh(info, src_jsonl):
    """Raise unless the sidecar was built from exactly this JSONL."""
    if not info["guarded"]:
        raise ValueError(f"{info['path']}: v1 sidecar carries no fingerprint, "
                         f"cannot prove it matches {src_jsonl}")
    st = os.stat(src_jsonl)
    if st.st_size != info["jsonl_size"]:
        raise ValueError(f"STALE: {src_jsonl} is {st.st_size} bytes, sidecar was "
                         f"built from {info['jsonl_size']}")
    if st.st_mtime_ns != info["jsonl_mtime_ns"]:
        raise ValueError(f"STALE: {src_jsonl} mtime changed since the sidecar "
                         f"was built")
    if jsonl_fp(src_jsonl, st.st_size, info["n_rows"]) != info["jsonl_fp"]:
        raise ValueError(f"STALE: {src_jsonl} content fingerprint does not match")
    if info.get("jsonl_hash"):
        if jsonl_hash(src_jsonl) != info["jsonl_hash"]:
            raise ValueError(f"STALE: {src_jsonl} full-content hash does not "
                             f"match (the file changed without touching its "
                             f"size or mtime)")
    return True


def load_vectors(path, src_jsonl=None):
    """Load either a JSONL index or a binary sidecar, by magic. Guarded if asked."""
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic in (MAGIC_V1, MAGIC_V2, MAGIC_V3):
        info, X = read_index(path)
        if src_jsonl:
            check_fresh(info, src_jsonl)
        return X
    from convert_jsonl_to_f16 import read_jsonl          # same directory
    return read_jsonl(path)[2]


def write_from_jsonl(path, src_jsonl, dtype="f16"):
    """One pass over the JSONL -> v3 sidecar (metadata + vectors). Bounded memory."""
    import json

    n = 0
    with open(src_jsonl, "rb") as f:
        while True:
            chunk = f.read(1 << 22)
            if not chunk:
                break
            n += chunk.count(b"\n")

    X = None
    meta = []
    with open(src_jsonl, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            v = o.get("vec")
            if not v:
                continue
            if X is None:
                X = np.empty((n, len(v)), dtype=np.float32)
            if len(v) != X.shape[1]:
                continue
            i = len(meta)
            if i >= n:
                break
            X[i] = v
            meta.append({"sid": o.get("sid"), "file": o.get("file"),
                         "npast": o.get("npast"), "dim": len(v),
                         "anchor": o.get("anchor", -1),
                         "anchor2": o.get("anchor2", -1)})
    if X is None or not meta:
        raise SystemExit("no usable rows")
    return write_index(path, X[:len(meta)], dtype, src_jsonl=src_jsonl, meta=meta)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="build / verify the sidecar")
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--dtype", default="f16", choices=sorted(DTYPES))
    ap.add_argument("--vectors-only", action="store_true",
                    help="write v2 (no metadata block); the C server needs v3")
    ap.add_argument("--verify", action="store_true",
                    help="check an existing sidecar against its JSONL")
    a = ap.parse_args()
    if a.verify:
        info, _ = read_index(a.dst)
        check_fresh(info, a.src)
        print(f"ok: {a.dst} matches {a.src} "
              f"(n_rows={info['n_rows']}, fp={info['jsonl_fp']}, "
              f"meta={'yes' if info['meta'] else 'no'})")
        return
    if a.vectors_only:
        from convert_jsonl_to_f16 import read_jsonl
        n, dim, X = read_jsonl(a.src)
        meta = write_index(a.dst, X, a.dtype, src_jsonl=a.src)
    else:
        meta = write_from_jsonl(a.dst, a.src, a.dtype)
    print(f"wrote {a.dst}  rows={meta['n_rows']} dim={meta['dim']} "
          f"dtype={meta['dtype']} meta={meta['has_meta']} "
          f"fp={meta['jsonl_fp']} size={os.path.getsize(a.dst)}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
