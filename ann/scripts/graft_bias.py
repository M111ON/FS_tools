#!/usr/bin/env python3
"""graft_bias.py — turn graft's wiring graph into a tunable relationship layer,
and bake per-entry affinity keys for the ANN "Confident Guideline" bias.

WHY
  graft (.graph/wiring.json) gives typed, DIRECTED edges {source, target,
  relation, confidence} with NO numeric weight. This module defines the weight
  layer on top, so the same graph can be tried in many forms without touching
  the server: relation strengths, confidence, neighbor mode (direct / 2-hop /
  max / sum), damping — all parameters.

MODEL
  w(A->B) = sum over edges A->B of  rel_weight(relation) * conf_weight(confidence)
  squash(w, k) = w / (w + k)          -> [0,1), so 183 calls ~ 0.97, 1 call ~ 0.17
  neighborhood(f) = {neighbor: score} from direct edges and/or 2-hop paths.

OUTPUT (all under data/graft/)
  rel.json     {"<file>": {"<neighbor>": score, ...}, ...}
  entries.jsonl  {"sid": "...", "keys": ["core/x.h", ...]}   (what each entry mentions)
  bias.json    the exact config used (weights, mode, damp, k) for reproducibility

USAGE
  python scripts/graft_bias.py                       # defaults
  python scripts/graft_bias.py --mode 2hop --damp 0.5 --k 5
  python scripts/graft_bias.py --rel calls=1.0,imports=0.7,extends=0.9,contains=0.4
"""
import argparse, collections, json, os, re, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_WIRING = r"I:\DWGLS-native-fs\graft\.graph\wiring.json"
DEFAULT_DB = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
DEFAULT_INDEX = os.path.join(ROOT, "data", "memcore", "kv_index.jsonl")
OUT_DIR = os.path.join(ROOT, "data", "graft")

PATH_RE = re.compile(
    r'\b(core|tools|docs|tests|bench|src|include|experiments|collection|sid)'
    r'/[\w./-]+\.(h|c|py|md|jsonl)\b')


def _file(p):
    return p.split("#")[0]


def parse_rel(s):
    d = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = float(v)
    return d


def build_rel(edges, rel_w, conf_w, k):
    """directed file->file weights, squashed to [0,1)."""
    raw = collections.Counter()
    for e in edges:
        a, b = _file(e["source"]), _file(e["target"])
        if a == b:
            continue
        raw[(a, b)] += rel_w.get(e.get("relation"), 0.5) * conf_w.get(e.get("confidence"), 0.5)
    out = collections.defaultdict(dict)
    for (a, b), w in raw.items():
        out[a][b] = round(w / (w + k), 6)
    return out, raw


def neighborhood(rel, raw, mode, damp, k):
    """file -> {neighbor: score} for the requested form."""
    out = collections.defaultdict(dict)
    files = set(rel)
    for f in files:
        direct = dict(rel.get(f, {}))
        acc = dict(direct)
        if mode in ("2hop", "max", "sum"):
            two = collections.Counter()
            for x, wx in rel.get(f, {}).items():
                for y, wy in rel.get(x, {}).items():
                    if y == f:
                        continue
                    two[y] += damp * wx * wy
            two = {y: v / (v + k) for y, v in two.items()}
            if mode == "2hop":
                acc = two
            elif mode == "max":
                for y, v in two.items():
                    acc[y] = max(acc.get(y, 0.0), v)
            else:  # sum
                for y, v in two.items():
                    acc[y] = acc.get(y, 0.0) + v
        out[f] = acc
    return out


def entry_keys(db_path, valid_sids):
    keys = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute("select id, content from entries"):
        sid = r["id"]
        if sid not in valid_sids:
            continue
        ks = sorted(set(m.group(0) for m in PATH_RE.finditer(r["content"] or "")))
        if ks:
            keys[sid] = ks
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiring", default=DEFAULT_WIRING)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--rel", default="calls=1.0,imports=0.7,extends=0.9,contains=0.4")
    ap.add_argument("--conf", default="extracted=1.0,inferred=0.5")
    ap.add_argument("--mode", default="direct", choices=["direct", "2hop", "max", "sum"])
    ap.add_argument("--damp", type=float, default=0.5)
    ap.add_argument("--k", type=float, default=5.0)
    a = ap.parse_args()

    rel_w, conf_w = parse_rel(a.rel), parse_rel(a.conf)
    print(f"wiring: {a.wiring}")
    edges = json.load(open(a.wiring, encoding="utf-8"))["edges"]
    rel, raw = build_rel(edges, rel_w, conf_w, a.k)
    print(f"  edges {len(edges)} -> {sum(len(v) for v in rel.values())} directed file pairs, {len(rel)} files")
    nb = neighborhood(rel, raw, a.mode, a.damp, a.k)
    print(f"  mode={a.mode} damp={a.damp} k={a.k}")

    valid = set()
    with open(a.index, encoding="utf-8") as f:
        for line in f:
            valid.add(json.loads(line)["sid"])
    keys = entry_keys(a.db, valid)
    print(f"  entries with file keys: {len(keys)}/{len(valid)}")

    os.makedirs(a.out, exist_ok=True)
    json.dump(rel, open(os.path.join(a.out, "rel.json"), "w", encoding="utf-8"))
    with open(os.path.join(a.out, "entries.jsonl"), "w", encoding="utf-8") as f:
        for sid, ks in keys.items():
            f.write(json.dumps({"sid": sid, "keys": ks}, ensure_ascii=False) + "\n")

    # ── flat TSVs the C server reads (id-based, so the loader is trivial) ──
    files = sorted(set(rel) | {n for v in rel.values() for n in v})
    fid = {n: i for i, n in enumerate(files)}
    with open(os.path.join(a.out, "files.tsv"), "w", encoding="utf-8") as f:
        for i, n in enumerate(files):
            f.write(f"{i}\t{n}\n")
    with open(os.path.join(a.out, "nb.tsv"), "w", encoding="utf-8") as f:
        for src, nbrs in nb.items():
            if not nbrs:
                continue
            row = [str(fid[src])]
            for dst, sc in nbrs.items():
                if dst in fid:
                    row.append(f"{fid[dst]}:{sc}")
            f.write("\t".join(row) + "\n")
    with open(os.path.join(a.out, "keys.tsv"), "w", encoding="utf-8") as f:
        for sid, ks in keys.items():
            ids = [str(fid[k]) for k in ks if k in fid]
            if ids:
                f.write(sid + "\t" + "\t".join(ids) + "\n")

    cfg = {"wiring": a.wiring, "rel_weights": rel_w, "conf_weights": conf_w,
           "mode": a.mode, "damp": a.damp, "k": a.k,
           "files": len(rel), "entries_with_keys": len(keys)}
    json.dump(cfg, open(os.path.join(a.out, "bias.json"), "w", encoding="utf-8"), indent=2)
    print(f"wrote {a.out}/{{rel.json, entries.jsonl, bias.json, files.tsv, nb.tsv, keys.tsv}}")


if __name__ == "__main__":
    main()
