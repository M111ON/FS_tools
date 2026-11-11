"""Part-time JEV audit for ANN query logs.

Reads the append-only query log, evaluates each query's returned candidates
with the TypeSafe JEV decision engine (jev-1.13), and appends one audit row
per query. Never touches the ANN index or query log. Dedups by query id.

Context-window friendly: a query with k candidates is split into chunks of
`chunk_size` (default 5) so each JEV call stays small. Only audit results
are written; nothing is fed back into search.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

API_URL = "http://192.168.1.176:20128/v1/systemone"
API_KEY = "sk-a0217d0a6dc1f88b-olr10e-3d11735c"
MODEL = "openrouter/typesafe/jev-1.13"
DEFAULT_CHUNK = 5


def _post(body: dict) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def query_id(row: dict) -> str:
    # input_hash/input_text are natural keys; fall back to ts_ms.
    return str(row.get("input_hash") or row.get("input_text") or row.get("ts_ms") or id(row))


def _state_text(row: dict, cands: list[dict]) -> str:
    top = row.get("top") or []
    lines = [f"Query: (input_hash={row.get('input_hash')}, topk={row.get('topk')})"]
    for i, c in enumerate(cands, 1):
        sid = c.get("sid")
        score = c.get("score")
        lines.append(f"Candidate{i}: sid={sid} ann_score={score}")
    return "\n".join(lines)


def audit_one_query(row: dict, chunk_size: int = DEFAULT_CHUNK,
                    pruned_sids: set | None = None) -> dict:
    top = row.get("top") or []
    pruned_sids = pruned_sids or set()
    # X-ray first: drop cells it pruned before JEV scoring.
    cands = []
    xray_pruned_count = 0
    for r in top:
        sid = r.get("sid")
        if sid in pruned_sids:
            xray_pruned_count += 1
            continue
        cands.append({"sid": sid, "score": r.get("score"),
                      "rank": r.get("rank")})
    # safety net: if X-ray pruned everything, fall back to scoring all
    orig_pruned = xray_pruned_count
    if not cands and top:
        for r in top:
            cands.append({"sid": r.get("sid"), "score": r.get("score"),
                          "rank": r.get("rank")})
        xray_pruned_count = orig_pruned
    k = len(top)
    chunks = [cands[i:i + chunk_size] for i in range(0, k, chunk_size)] or [[]]

    chunk_results = []
    for ci, chunk in enumerate(chunks):
        if not chunk:
            chunk_results.append({"chunk": ci, "results": []})
            continue
        questions = {}
        for j, c in enumerate(chunk, 1):
            questions[f"c{j}_match"] = {
                "type": "noul",
                "instructions": f"Does Candidate{j} (sid={c['sid']}) answer the query best?",
            }
        body = {
            "model": MODEL,
            "state": _state_text(row, chunk),
            "questions": questions,
        }
        resp = _post(body)
        answers = (resp.get("answers") or {})
        out = []
        for j, c in enumerate(chunk, 1):
            a = answers.get(f"c{j}_match", {})
            out.append({
                "sid": c["sid"],
                "rank": c.get("rank"),
                "ann_score": c.get("score"),
                "jev": a.get("noul"),
                "jev_type": a.get("type", "noul"),
            })
        chunk_results.append({"chunk": ci, "results": out})

    flat = [r for cr in chunk_results for r in cr["results"]]
    best = max(flat, key=lambda x: (x.get("jev") or 0.0)) if flat else None
    return {
        "query_id": query_id(row),
        "ts_ms": row.get("ts_ms"),
        "k": k,
        "k_scored": len(cands),
        "xray_pruned": xray_pruned_count,
        "chunk_size": chunk_size,
        "chunks": len(chunks),
        "mode": "part_time_audit",
        "results": flat,
        "best_sid": best["sid"] if best else None,
        "best_jev": best["jev"] if best else None,
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


def _xray_pruned_for(xray_path: Path | None, qid: str) -> set:
    s = set()
    if not xray_path or not xray_path.exists():
        return s
    try:
        for raw in xray_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            r = json.loads(raw)
            if str(r.get("query_id")) != qid:
                continue
            for c in r.get("candidates", []):
                if c.get("xray", {}).get("pruned"):
                    s.add(c.get("sid"))
    except Exception:
        return s
    return s


def process_log(log_path: Path, audit_path: Path, state_path: Path,
                limit: int | None = None, chunk_size: int = DEFAULT_CHUNK,
                follow: bool = False, xray_audit_path: Path | None = None) -> int:
    state = load_state(state_path)
    processed = 0
    while True:
        if not log_path.exists():
            if follow:
                time.sleep(2)
                continue
            break
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for raw in lines:
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
            try:
                pruned_sids = _xray_pruned_for(xray_audit_path, qid)
                audit = audit_one_query(row, chunk_size=chunk_size, pruned_sids=pruned_sids)
            except Exception as e:
                state[qid] = {"error": str(e)}
                save_state(state_path, state)
                continue
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(audit, sort_keys=True) + "\n")
            state[qid] = {"processed": True, "best_sid": audit["best_sid"],
                          "best_jev": audit["best_jev"], "k": audit["k"]}
            save_state(state_path, state)
            processed += 1
            if limit is not None and processed >= limit:
                return processed
        if not follow:
            break
        time.sleep(2)
    return processed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=r"I:\tools\ann\data\query_log\ann_query-20261111.jsonl")
    ap.add_argument("--audit", default=r"I:\tools\ann\data\jev_audit.jsonl")
    ap.add_argument("--state", default=r"I:\tools\ann\data\jev_audit_state.json")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK)
    ap.add_argument("--follow", action="store_true")
    args = ap.parse_args()
    n = process_log(Path(args.log), Path(args.audit), Path(args.state),
                    limit=args.limit, chunk_size=args.chunk_size, follow=args.follow)
    print(f"processed {n} queries")


if __name__ == "__main__":
    main()
