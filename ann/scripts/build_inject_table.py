#!/usr/bin/env python3
"""build_inject_table.py — manifold-axis anchor tables (no training).

Rows borrow DIRECTION from the data (axes, not values), then normalize:
  Jmean: row = normalize(mean + eps*u_k), u_k = unit tangentialized seed row
  Jpc  : row = normalize(mean + eps*PC_k), pure manifold axes, zero seed content
eps default 0.17 = trained tangential size (measured 2026-10-10).

Writes ann's exact ann_anchors.bin shape (byte-exact FNV-1a checksum).
Reproducible: same index + same args = same bytes (verified sha-stable).

Usage: build_inject_table.py MODE OUT [NTR] [EPS] [INDEX] [SEEDROWS]
  MODE: mean | pc
  mean needs SEEDROWS: an 8x384 .bin whose rows supply tangential directions
  (measured 2026-10-10 with trap-P rows; any 8-row table works).
"""
import json
import struct
import sys

import numpy as np

DIM = 384


def load_index(path):
    rows = []
    with open(path, "rb") as f:
        for line in f:
            rows.append(json.loads(line)["vec"])
    return np.asarray(rows, dtype=np.float64)


def fnv32(b):
    h = 2166136261
    for byte in b:
        h ^= byte
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pc"
    out = sys.argv[2] if len(sys.argv) > 2 else "data/inject_anchors.bin"
    ntr = int(sys.argv[3]) if len(sys.argv) > 3 else 59756
    eps = float(sys.argv[4]) if len(sys.argv) > 4 else 0.17
    index = sys.argv[5] if len(sys.argv) > 5 else "data/memcore/ann_index.jsonl"

    print("loading index...", flush=True)
    X = load_index(index)
    n = len(X)
    print("n =", n, flush=True)
    m = X.mean(axis=0)
    m = m / np.linalg.norm(m)
    Xc = X - X.mean(axis=0)
    C = (Xc.T @ Xc) / n
    evals, evecs = np.linalg.eigh(C)
    PC8 = evecs[:, np.argsort(evals)[::-1][:8]]

    if mode == "mean":
        sp = sys.argv[6] if len(sys.argv) > 6 else None
        if not sp:
            raise SystemExit("mean mode needs SEEDROWS table path")
        d = open(sp, "rb").read()
        magic, K, dim, _ = struct.unpack("<4i", d[:16])
        assert (magic, K, dim) == (0x414E4352, 8, DIM)
        S = np.array(struct.unpack("<%df" % (K * dim), d[20:20 + K * dim * 4]),
                     dtype=np.float64).reshape(K, dim)
        U = [unit(r - (r @ m) * m) for r in S]
        T = np.array([unit(m + eps * u) for u in U])
    elif mode == "pc":
        T = np.array([unit(m + eps * PC8[:, k]) for k in range(8)])
    else:
        raise SystemExit("MODE mean|pc")

    T = np.asarray(T, dtype=np.float32)
    blob = T.tobytes()
    hdr = struct.pack("<5I", 0x414E4352, 8, DIM, ntr, fnv32(blob))
    with open(out, "wb") as f:
        f.write(hdr + blob)
    print("wrote", out, "mode", mode, "eps", eps, flush=True)


if __name__ == "__main__":
    main()
