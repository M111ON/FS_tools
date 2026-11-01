"""make_slice.py — cut the first N rows of the ANN index into a standalone bundle.

Row i of the f16 sidecar is row i of the sliced jsonl, so the bundle needs no
changes on the read path: point ANN_INDEX_PATH/ANN_ANCH_PATH at the new dir.

Writes into --out: ann_index.jsonl · ann_index.f16 · ann_tags.tsv (sids present
in the slice only) · README.txt. Anchors/order are NOT sliced — the server
rebuilds them when it loads a new index.

Run: python make_slice.py --n 20000 --out I:\\tools\\ann\\zip\\ann_20k
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_jsonl_to_f16 import read_jsonl, write_binary  # noqa: E402

SRC = r"I:\tools\ann\data\memcore"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    out_jsonl = os.path.join(a.out, "ann_index.jsonl")
    if os.path.exists(out_jsonl) and not a.force:
        sys.exit(f"exists, use --force: {a.out}")

    lines, sids, dim = [], set(), None
    with open(os.path.join(a.src, "ann_index.jsonl"), "r", encoding="utf-8",
              errors="replace") as f:
        for line in f:
            if len(lines) >= a.n:
                break
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
            dim = dim or len(v)
            if len(v) != dim:
                continue
            lines.append(line)
            sids.add(o["sid"])

    with open(out_jsonl, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    n, dim2, X = read_jsonl(out_jsonl, a.n)
    assert (n, dim2) == (len(lines), dim), "slice rows disagree with parsed vectors"
    write_binary(os.path.join(a.out, "ann_index.f16"), X, "f16")

    kept = 0
    with open(os.path.join(a.src, "ann_tags.tsv"), "r", encoding="utf-8",
              errors="replace") as f, \
         open(os.path.join(a.out, "ann_tags.tsv"), "w", encoding="utf-8") as g:
        for line in f:
            if line.split("\t", 1)[0] in sids:
                g.write(line)
                kept += 1

    tags = sorted({ln.split("\t")[1] for ln in
                   open(os.path.join(a.out, "ann_tags.tsv"), encoding="utf-8")})
    with open(os.path.join(a.out, "README.txt"), "w", encoding="utf-8") as f:
        f.write(
            f"ANN slice: {n} rows x {dim} dim, f16, taken from the FIRST {n} rows\n"
            f"of data/memcore/ann_index.jsonl (file order, no shuffle).\n\n"
            f"ann_index.jsonl  {os.path.getsize(out_jsonl)/1048576:.1f} MB source of truth\n"
            f"ann_index.f16    {os.path.getsize(os.path.join(a.out,'ann_index.f16'))/1048576:.1f} MB"
            f" sidecar, row i == jsonl row i\n"
            f"ann_tags.tsv     {kept} rows, tags: {', '.join(tags)}\n\n"
            f"No ann_anchors.bin / ann_index.order here: the server rebuilds them on\n"
            f"load (ANN_INDEX_PATH=<this dir>/ann_index.jsonl, ANN_ANCH_PATH=<new file>).\n")

    print(f"rows {n} dim {dim} tags {kept} -> {a.out}")


if __name__ == "__main__":
    main()
