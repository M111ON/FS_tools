"""Part-time X-ray audit for ANN query logs (stateless).

Mirrors core/entropy_xray.c (splitmix64 + popcount variance) so query
history can be scored for entropy without touching the ANN index. Writes one
JSONL row per query with per-candidate cell entropy metrics + prune flags.
"""
from __future__ import annotations

import argparse
import glob
import json
import time
from pathlib import Path

CELL_WORDS = 8
WORD_BITS = 64
LOW = 0
HIGH = 8192
TARGET = 4096
MASK64 = (1 << 64) - 1
MUL = 0x9E3779B97F4A7C15


def splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & MASK64
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
    x = (x ^ (x >> 27)) * 0x94D049BB133111B9 & MASK64
    return (x ^ (x >> 31)) & MASK64


def scan_cell(cell_id: int) -> dict:
    state = (cell_id * MUL + 0xC2B2AE3D27D4EB4F) & MASK64
    total = 0
    set_bits = 0
    var = 0
    for _ in range(CELL_WORDS):
        state = splitmix64(state)
        pc = state.bit_count()
        total += WORD_BITS
        set_bits += pc
        var += pc * (WORD_BITS - pc)
    density = (set_bits * 1000) // total if total else 0
    flags = 0
    if var < LOW:
        flags |= 1  # PRUNE_LOW
    if var > HIGH:
        flags |= 2  # PRUNE_HIGH
    # density-based prune (effective on observed corpus)
    if density < 470:
        flags |= 1
    if density > 530:
        flags |= 2
    return {
        "cell_id": cell_id,
        "total_bits": total,
        "set_bits": set_bits,
        "density": density,
        "variance": var,
        "bit_variance": var,
        "entropy": var,
        "flags": flags,
        "pruned": 1 if flags else 0,
    }


def load_sid_map(index_path: Path) -> dict[str, int]:
    m = {}
    if not index_path.exists():
        return m
    with index_path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            sid = r.get("sid")
            if isinstance(sid, str) and sid not in m:
                m[sid] = i
    return m


def query_id(row: dict) -> str:
    return str(row.get("input_hash") or row.get("input_text") or row.get("ts_ms") or id(row))


def audit_one(row: dict, sid_map: dict[str, int]) -> dict:
    top = row.get("top") or []
    cands = []
    for r in top:
        sid = r.get("sid")
        cell = sid_map.get(sid) if isinstance(sid, str) else None
        if cell is None:
            # fallback: stable hash of sid string
            cell = 0
            if isinstance(sid, str):
                h = 2166136261
                for ch in sid:
                    h ^= ord(ch)
                    h = (h * 16777619) & 0xFFFFFFFF
                cell = h
        xr = scan_cell(cell)
        cands.append({
            "sid": sid,
            "rank": r.get("rank"),
            "ann_score": r.get("score"),
            "cell_id": cell,
            "xray": xr,
        })
    pruned = sum(1 for c in cands if c["xray"]["pruned"])
    avg_var = (sum(c["xray"]["variance"] for c in cands) / len(cands)) if cands else 0.0
    return {
        "query_id": query_id(row),
        "ts_ms": row.get("ts_ms"),
        "k": len(cands),
        "mode": "xray_audit",
        "candidates": cands,
        "avg_variance": avg_var,
        "pruned_count": pruned,
        "processed_at": int(time.time() * 1000),
    }


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def process_log(log_path: Path, audit_path: Path, state_path: Path,
                index_path: Path, limit: int | None = None) -> int:
    sid_map = load_sid_map(index_path)
    state = load_state(state_path)
    processed = 0
    if not log_path.exists():
        return 0
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        qid = query_id(row)
        if qid in state:
            continue
        top = row.get("top") or []
        if not top:
            state[qid] = {"skipped": True, "reason": "no_candidates"}
            save_state(state_path, state)
            continue
        audit = audit_one(row, sid_map)
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit, sort_keys=True) + "\n")
        state[qid] = {"processed": True, "k": audit["k"],
                      "avg_variance": audit["avg_variance"],
                      "pruned_count": audit["pruned_count"]}
        save_state(state_path, state)
        processed += 1
        if limit is not None and processed >= limit:
            break
    return processed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=r"I:\tools\ann\data\query_log\ann_query-20261111.jsonl")
    ap.add_argument("--audit", default=r"I:\tools\ann\data\xray_audit.jsonl")
    ap.add_argument("--state", default=r"I:\tools\ann\data\xray_audit_state.json")
    ap.add_argument("--index", default=r"I:\tools\ann\data\memcore\ann_index.jsonl")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    n = process_log(Path(args.log), Path(args.audit), Path(args.state),
                    Path(args.index), limit=args.limit)
    print(f"processed {n} queries")


if __name__ == "__main__":
    main()
