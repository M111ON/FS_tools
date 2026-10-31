"""test_index_sidecar.py — the staleness guard must reject, not warn.

A sidecar whose row order drifted serves the wrong vector for every row with no
error, so every case here asserts a RAISE. Run: python tests/test_index_sidecar.py
"""
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))

from index_sidecar import (HDR_V1, MAGIC_V1, check_fresh, load_vectors,  # noqa: E402
                           read_index, write_index)

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
        return False, ""
    except Exception as e:
        return True, str(e)[:70]


def make_jsonl(path, n=200, dim=16, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, dim)).astype(np.float32)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"sid": f"s{i:04d}", "dim": dim,
                                "vec": X[i].tolist()}) + "\n")
    return X


d = tempfile.mkdtemp(prefix="sidecar_")
J = os.path.join(d, "idx.jsonl")
B = os.path.join(d, "idx.f16")
X = make_jsonl(J)
print(f"fixture: {os.path.getsize(J)} bytes, {len(X)} rows")

print("\n1. write + read")
meta = write_index(B, X, "f16", src_jsonl=J)
info, Y = read_index(B)
check("rows match", info["n_rows"] == len(X))
check("guarded", info["guarded"])
check("f16 roundtrip within 1e-3", np.abs(Y - X).max() < 1e-3,
      f"max|dx|={np.abs(Y - X).max():.2e}")

print("\n2. f32 sidecar is bit-exact")
B32 = os.path.join(d, "idx.f32")
write_index(B32, X, "f32", src_jsonl=J)
_, Z = read_index(B32)
check("f32 identical to source", np.array_equal(Z, X))

print("\n3. fresh sidecar passes")
check("check_fresh ok", check_fresh(info, J))

print("\n4. mtime moved (same bytes) -> reject")
os.utime(J, ns=(info["jsonl_mtime_ns"] + 1_000_000_000,
                info["jsonl_mtime_ns"] + 1_000_000_000))
ok, why = raises(check_fresh, info, J)
check("rejected", ok, why)

print("\n5. same size, one byte flipped in the middle -> reject")
raw = open(J, "rb").read()
mid = len(raw) // 2
open(J, "wb").write(raw[:mid] + bytes([raw[mid] ^ 0x01]) + raw[mid + 1:])
ok, why = raises(check_fresh, info, J)
check("rejected", ok, why)
check("size unchanged by the flip", os.path.getsize(J) == len(raw))

print("\n6. reordered rows, same size -> reject")
X2 = make_jsonl(J)
lines = open(J, "rb").readlines()
open(J, "wb").writelines(lines[::-1])
check("size unchanged by the reorder", os.path.getsize(J) == len(raw))
ok, why = raises(check_fresh, info, J)
check("rejected", ok, why)

print("\n7. rebuilt sidecar passes again")
X3 = make_jsonl(J)
write_index(B, X3, "f16", src_jsonl=J)
info2, _ = read_index(B)
check("check_fresh ok after rebuild", check_fresh(info2, J))

print("\n8. v1 header has no fingerprint -> refuses to claim freshness")
B1 = os.path.join(d, "idx.v1")
with open(B1, "wb") as f:
    f.write(HDR_V1.pack(MAGIC_V1, 1, 1, 16, len(X3)))
    X3.astype(np.float16).tofile(f)
i1, _ = read_index(B1)
check("readable", i1["n_rows"] == len(X3))
check("marked unguarded", not i1["guarded"])
ok, why = raises(check_fresh, i1, J)
check("check_fresh refuses v1", ok, why)

print("\n9. load_vectors dispatches by magic")
a = load_vectors(B, src_jsonl=J)
b = load_vectors(J)
check("binary path", np.abs(a - X3).max() < 1e-3)
check("jsonl path identical", np.allclose(b, X3))

print("\n10. guarded load of a stale sidecar raises")
os.utime(J, ns=(info2["jsonl_mtime_ns"] + 2_000_000_000,) * 2)
ok, why = raises(load_vectors, B, J)
check("load refused", ok, why)

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
