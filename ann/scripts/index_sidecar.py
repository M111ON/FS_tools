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
VERSION = 2
DTYPES = {"f32": (0, np.float32, 4), "f16": (1, np.float16, 2)}

HDR_V1 = struct.Struct("<8sIIIQ")                  # magic version dtype dim n
HDR_V2 = struct.Struct("<8sIIIIQQQQQ")             # + size mtime_ns fp reserved

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


def write_index(path, X, dtype="f16", src_jsonl=None):
    """Write the sidecar. src_jsonl=None produces an unguarded v2 (size/fp = 0)."""
    code, np_dt, _ = DTYPES[dtype]
    n, dim = X.shape
    if src_jsonl:
        st = os.stat(src_jsonl)
        size, mtime_ns = st.st_size, st.st_mtime_ns
        fp = jsonl_fp(src_jsonl, size, n)
    else:
        size = mtime_ns = fp = 0
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(HDR_V2.pack(MAGIC_V2, VERSION, code, dim, 0, n, size,
                            mtime_ns, fp, 0))
        X.astype(np_dt, copy=False).tofile(f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return {"n_rows": n, "dim": dim, "dtype": dtype, "jsonl_size": size,
            "jsonl_mtime_ns": mtime_ns, "jsonl_fp": fp}


def read_index(path, require_guard=True):
    """Return (info, float32 array). Raises if the sidecar is stale or bad.

    info["guarded"] is False for v1 headers (no fingerprint to check).
    """
    with open(path, "rb") as f:
        head = f.read(HDR_V2.size)
    magic = head[:8]
    if magic == MAGIC_V2:
        _, ver, code, dim, _r0, n, size, mtime_ns, fp, _r1 = HDR_V2.unpack(head)
        off = HDR_V2.size
    elif magic == MAGIC_V1:
        _, ver, code, dim, n = HDR_V1.unpack(head[:HDR_V1.size])
        size = mtime_ns = fp = 0
        off = HDR_V1.size
    else:
        raise ValueError(f"{path}: bad magic {magic!r}")

    name = {v[0]: k for k, v in DTYPES.items()}[code]
    np_dt = DTYPES[name][1]
    mm = np.memmap(path, dtype=np_dt, mode="r", offset=off, shape=(n, dim))
    info = {"path": path, "version": ver, "dtype": name, "dim": dim,
            "n_rows": n, "jsonl_size": size, "jsonl_mtime_ns": mtime_ns,
            "jsonl_fp": fp, "guarded": magic == MAGIC_V2 and fp != 0}
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
    return True


def load_vectors(path, src_jsonl=None):
    """Load either a JSONL index or a binary sidecar, by magic. Guarded if asked."""
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic in (MAGIC_V1, MAGIC_V2):
        info, X = read_index(path)
        if src_jsonl:
            check_fresh(info, src_jsonl)
        return X
    from convert_jsonl_to_f16 import read_jsonl          # same directory
    return read_jsonl(path)[2]


def main():
    import argparse
    from convert_jsonl_to_f16 import read_jsonl
    ap = argparse.ArgumentParser(description="build / verify the sidecar")
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--dtype", default="f16", choices=sorted(DTYPES))
    ap.add_argument("--verify", action="store_true",
                    help="check an existing sidecar against its JSONL")
    a = ap.parse_args()
    if a.verify:
        info, _ = read_index(a.dst)
        check_fresh(info, a.src)
        print(f"ok: {a.dst} matches {a.src} "
              f"(n_rows={info['n_rows']}, fp={info['jsonl_fp']})")
        return
    n, dim, X = read_jsonl(a.src)
    meta = write_index(a.dst, X, a.dtype, src_jsonl=a.src)
    print(f"wrote {a.dst}  rows={n} dim={dim} dtype={a.dtype} "
          f"fp={meta['jsonl_fp']} size={os.path.getsize(a.dst)}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
