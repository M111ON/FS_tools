"""ann_router_eval_cqadupstack.py — put standard data through the real router.

CQADupStack embeddings arrive as .npy + id lists (Colab). The ANN machine reads
its own JSONL index, so this does both halves:

  1. convert  <pkg>/<sub>/{corpus_ids.json,corpus_embeddings.npy}  ->  ann_index.jsonl
  2. start a SCRATCH ann_search instance (its own port and its own
     anchors/order/log paths, never the live data/ files), feed each query
     vector through POST /v1/state/search with a caller-supplied "vec" (the
     handler prefers it over embedding the text, so no embedder mismatch), and
     compare the router's top-10 against brute force over the same vectors.

Why the "vec" path matters: the served embedder is e5-small, these vectors are
all-MiniLM. Embedding the query text would silently mix epochs; sending the
vector keeps the ranking self-consistent and makes any BEIR package measurable
on this box without re-embedding.

Reported: recall@10 of the router vs brute force (qrels in the loop), plus
items_scored vs corpus size - the design question is whether the geometric route
finds the true neighbours while touching a fraction of the rows.

Run: python experiments/ann_router_eval_cqadupstack.py --sub mathematica
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "data" / "beir_embeddings"
WORK = ROOT / "data" / "beir_embeddings_eval"
EXE = ROOT / "build" / "ann_search.exe"
TOPK = 10
PORT = 8097
MAX_CHARS_PER_LINE = None      # streamed, no cap issues


def to_jsonl(sub):
    base = PKG / sub
    ids = json.load(open(base / "corpus_ids.json", encoding="utf-8"))
    C = np.load(base / "corpus_embeddings.npy")
    assert len(ids) == len(C), "%s: %d ids for %d rows" % (sub, len(ids), len(C))
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / ("%s.jsonl" % sub)
    t0 = time.time()
    with open(out, "w", encoding="utf-8") as f:
        for sid, v in zip(ids, C):
            f.write('{"sid": %s, "file": %s, "npast": 0, "dim": %d, "vec": [%s]}\n'
                    % (json.dumps(str(sid)), json.dumps(str(sid)), C.shape[1],
                       ",".join(repr(float(x)) for x in v)))
    print("index: %s  %d rows  %.1f MB  %.0fs"
          % (out, len(ids), out.stat().st_size / 2**20, time.time() - t0), flush=True)
    return out


def train_anchors(idx, out_bin, K=56, iters=25):
    """Same anchor training as experiments/train_anchors_fast.py (evenly-spaced
    init, 25 Lloyd iterations, magic 0x414E4352 header), but reading the index we
    just wrote and writing into the scratch dir, so a new dataset needs no manual
    step. Without this file the router answers mode="brute" - measured, not
    assumed."""
    import struct
    if out_bin.exists() and out_bin.stat().st_size > 0:
        print("anchors: reusing %s" % out_bin, flush=True)
        return
    X = json.loads
    rows = []
    with open(idx, encoding="utf-8") as f:
        for line in f:
            rows.append(X(line)["vec"])
    X64 = np.asarray(rows, dtype=np.float32).astype(np.float64)
    n, dim = X64.shape
    t0 = time.time()
    C = X64[[k * n // K for k in range(K)]].copy()
    xnorm = np.sum(X64 ** 2, axis=1)
    for it in range(iters):
        dists = xnorm[:, None] + np.sum(C ** 2, axis=1)[None, :] - 2 * (X64 @ C.T)
        lab = np.argmin(dists, axis=1)
        C = np.zeros((K, dim), dtype=np.float64)
        cnt = np.zeros(K, dtype=np.int64)
        np.add.at(C, lab, X64)
        np.add.at(cnt, lab, 1)
        for k in range(K):
            C[k] = C[k] / cnt[k] if cnt[k] else X64[k * n // K]
    Cf = C.astype(np.float32)
    h = 2166136261
    for b in Cf.tobytes():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    with open(out_bin, "wb") as f:
        f.write(struct.pack("<5I", 0x414E4352, K, dim, n, h))
        f.write(Cf.tobytes())
    print("anchors: %s  K=%d dim=%d n=%d  %.0fs" % (out_bin, K, dim, n, time.time() - t0), flush=True)


def wait_health(port, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as r:
                body = json.loads(r.read())
                if "entries" in body:
                    return body
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("scratch ann_search never answered /health on %d" % port)


def search(port, vec, topk):
    body = json.dumps({"vec": [float(x) for x in vec], "topk": topk}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/state/search" % port, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def load_qrels(sub):
    import glob
    cands = sorted(glob.glob(str(PKG / sub / "test*.tsv")))
    if not cands:
        raise FileNotFoundError("no test*.tsv in %s" % (PKG / sub))
    qrels = {}
    with open(cands[0], encoding="utf-8") as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and int(parts[2]) > 0:
                qrels.setdefault(parts[0], set()).add(parts[1])
    return qrels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="mathematica")
    ap.add_argument("--limit-queries", type=int, default=0, help="0 = all")
    ap.add_argument("--topk", type=int, default=TOPK)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--k", type=int, default=56, help="anchor count for the trained routing file")
    a = ap.parse_args()

    idx = to_jsonl(a.sub)
    Work = WORK / a.sub
    Work.mkdir(parents=True, exist_ok=True)
    train_anchors(idx, Work / "ann_anchors.bin", K=a.k)
    env = dict(os.environ)
    # every sidecar (<index>.seeds, <index>.f16, .order, roster, route log)
    # derives from these, and they all point into the scratch dir - the live
    # data/ files of the :8096 service are never touched.
    env.update({
        "ANN_INDEX_PATH": str(idx),
        "ANN_ANCH_PATH": str(Work / "ann_anchors.bin"),
        "ANN_ORDER_PATH": str(Work / "ann_index.order"),
        "ANN_ROUTE_LOG": str(Work / "lz_route.log"),
        "ANN_ROSTER_PATH": str(Work / "ann_roster.bin"),
    })
    log = open(Work / "stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(EXE), "--serve", str(a.port)], env=env, cwd=str(ROOT),
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        health = wait_health(a.port)
        print("health:", json.dumps(health)[:200], flush=True)

        ids = json.load(open(PKG / a.sub / "corpus_ids.json", encoding="utf-8"))
        C = np.load(PKG / a.sub / "corpus_embeddings.npy")
        Q = np.load(PKG / a.sub / "query_embeddings.npy")
        qids = json.load(open(PKG / a.sub / "query_ids.json", encoding="utf-8"))
        qrels = load_qrels(a.sub)
        Cn = C / np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
        Qn = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-9)

        order = list(range(len(qids)))
        if a.limit_queries:
            order = order[:a.limit_queries]

        hits_router = hits_brute = 0.0
        total = 0
        scored_fracs, fallbacks, modes, lat = [], [], {}, []
        for qi in order:
            qid = qids[qi]
            if qid not in qrels:
                continue
            truth = qrels[qid]
            got = search(a.port, Q[qi], a.topk)
            if got.get("status") != "ok":
                raise RuntimeError("query %s: %s" % (qid, json.dumps(got)[:300]))
            rtop = {h["sid"] for h in got["hits"]}
            sims = Cn @ Qn[qi]
            btop = {ids[i] for i in np.argsort(-sims)[:a.topk]}
            k = min(a.topk, len(truth))
            hits_router += len(rtop & truth) / k
            hits_brute += len(btop & truth) / k
            total += 1
            route = got.get("route", {})
            n_tot = route.get("items_total") or len(ids)
            scored_fracs.append((route.get("items_scored") or 0) / max(n_tot, 1))
            fallbacks.append(1 if route.get("fallback") else 0)
            modes[route.get("mode", "?")] = modes.get(route.get("mode", "?"), 0) + 1

        out = {
            "sub": a.sub,
            "corpus": len(ids),
            "queries_scored": total,
            "topk": a.topk,
            "router_recall_at_k": round(hits_router / total, 4),
            "brute_recall_at_k": round(hits_brute / total, 4),
            "router_equals_brute": round(abs(hits_router - hits_brute) < 1e-9, 4),
            "items_scored_frac_mean": round(float(np.mean(scored_fracs)), 4),
            "items_scored_frac_max": round(float(np.max(scored_fracs)), 4),
            "fallback_frac": round(float(np.mean(fallbacks)), 4),
            "route_modes": modes,
            "index_path": str(idx),
            "note": "router fed caller-supplied query vectors; the live e5 embedder was never asked to embed these (different epoch)",
        }
        print(json.dumps(out, indent=2))
        rec = WORK / ("receipt_%s.json" % a.sub)
        rec.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("receipt:", rec)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
