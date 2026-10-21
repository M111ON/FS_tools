#!/usr/bin/env python3
"""ab_graft_fallback.py — does the graft hard-fallback keep recall?

THE FEATURE (ANN_GRAFT_FALLBACK=1, default OFF, added 2026-10-21)
----------------------------------------------------------------
When the below-floor fallback fires, the graph (graft) proposes the walk
order: the structurally related entries first, then the rest. Because the
walk still covers everything, recall cannot drop below the full scan — but
the ORDER changes what a budget-capped scan would reach, and the graph
proposes from a different axis (file structure, not vector geometry).

So the question is empirical: against an independent numpy oracle, does
the graft fallback lose recall, and does it save work?

Arms (scratch servers on port 8097, one copy of the live corpus):
  OFF   ANN_GRAFT=1, ANN_GRAFT_FALLBACK=0
  ON    ANN_GRAFT=1, ANN_GRAFT_FALLBACK=1
Both use the SAME corpus, the SAME queries, and the SAME oracle.

The floor is raised (2.1) so every query takes the below-floor branch —
the regime the fallback exists for. At the production floor a real query
scores ~0.86 and the fallback is never reached.

Usage:
  I:/python3.14.4/python.exe ab_graft_fallback.py [--queries 200]
"""
import argparse, json, os, shutil, subprocess, sys, time
import urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
EXE = os.environ.get("ANN_TEST_EXE") or os.path.join(REPO, "build",
                                                    "ann_search_batch.exe")
PORT = 8097
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-ab-graft")
LIVE = os.path.join(REPO, "data", "memcore")
EMBED = "http://127.0.0.1:18095/embedding"
K = 5


def http(base, path, body=None, timeout=300):
    if body is None:
        return json.loads(urllib.request.urlopen(base + path, timeout=timeout).read())
    r = Request(base + path, json.dumps(body).encode(),
                {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def copy_tree(dst):
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    for fn in ("ann_index.jsonl", "ann_index.order", "ann_anchors.bin",
               "ann_anchors-state.json", "ann_index.jsonl.stamp"):
        s = os.path.join(LIVE, fn)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(dst, fn))
    return dst


def start(idx_dir, work_dir, env_extra):
    os.makedirs(work_dir, exist_ok=True)
    env = {**os.environ,
           "ANN_INDEX_PATH": os.path.join(idx_dir, "ann_index.jsonl"),
           "ANN_ANCH_PATH": os.path.join(idx_dir, "ann_anchors.bin"),
           "ANN_ORDER_PATH": os.path.join(idx_dir, "ann_index.order"),
           "ANN_ROUTE_LOG": os.path.join(work_dir, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOMBSTONE_WARM": "1",
           "ANN_WALK_PRUNE": "1",
           "ANN_FLOAT_KERNEL": "1",
           "ANN_GRAFT": "1",
           "ANN_WALK_FLOOR": "2.1",
           **env_extra}
    err = open(os.path.join(work_dir, "srv_err.log"), "w", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=err)
    url = "http://127.0.0.1:%d" % PORT
    for _ in range(200):
        time.sleep(0.3)
        if p.poll() is not None:
            raise RuntimeError("scratch server exited (rc=%s)" % p.returncode)
        try:
            if http(url, "/health", timeout=4).get("status") == "ok":
                return p, url
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server did not start")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=200)
    a = ap.parse_args()

    try:
        import numpy as np
    except ImportError:
        print("numpy required (I:/python3.14.4/python.exe)")
        return 2

    idx_dir = copy_tree(os.path.join(SCRATCH, "index"))
    sids, vecs = [], []
    with open(os.path.join(idx_dir, "ann_index.jsonl"), encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("vec"):
                sids.append(d.get("sid") or "")
                vecs.append(d["vec"])
    M = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(M, axis=1)
    norms[norms == 0] = 1.0
    print("oracle over %d rows x %d dims" % (len(sids), M.shape[1]))
    if len(sids) < 1000:
        print("corpus too small")
        return 2

    # queries: real graph paths + out-of-corpus (the below-floor regime).
    # The graft graph keys on FILE PATHS that really appear in
    # data/graft/files.tsv (all 4,359 of its entries are present in the
    # live corpus — measured 2026-10-21). A query that names none of
    # them has no structural proposal, so the fallback cannot engage.
    # The paths below are the graph's highest-weight ones.
    texts = [
        "tests/geo_jump_real_test.c implementation",
        "core/geo_hyperbolic_walk.h implementation",
        "core/geo_hyperbolic_store.h implementation",
        "tests/test_geo_hyperbolic.c implementation",
        "tools/kv_dump_turns.c implementation",
        "tools/hexagram_cubes_probe.c implementation",
        "core/kv_dramtile_bridge.h implementation",
        "core/kv_geofs_bridge.h implementation",
        "tools/kv_park_bench.c implementation",
        "core/geofs_multivol.h implementation",
        "how to bake sourdough bread",
        "kubernetes pod eviction budget",
    ]
    try:
        rq = Request(EMBED, json.dumps({"content": texts}).encode(),
                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=120) as resp:
            d = json.loads(resp.read())
        qvecs = [(it["embedding"][0] if it["embedding"] and
                  isinstance(it["embedding"][0], list) else it["embedding"])
                 for it in d]
    except Exception as e:
        print("embed sidecar unavailable (%r)" % e)
        return 2

    results = {}
    for arm, fb in (("off", 0), ("on", 1)):
        p, url = start(idx_dir, os.path.join(SCRATCH, arm),
                       {"ANN_GRAFT_FALLBACK": str(fb)})
        try:
            rec = []
            for qi, qv in enumerate(qvecs):
                q = np.asarray(qv, dtype=np.float32)
                qn = float(np.linalg.norm(q))
                if qn == 0:
                    continue
                scores = (M @ q) / (norms * qn)
                idx = np.argpartition(-scores, K)[:K]
                truth = set(sids[j] for j in idx)
                # send BOTH the text and the vector: the text carries the
                # FILE KEYS the graft graph needs (graft_query_keys reads the
                # "query" string), the vector pins the ranking (no embedder
                # drift between the two arms).
                body = json.dumps({"query": texts[qi],
                                   "vec": [float(x) for x in qv],
                                   "topk": K}).encode()
                rq = Request(url + "/v1/state/search", body,
                             {"Content-Type": "application/json"})
                for _ in range(2):
                    with urllib.request.urlopen(rq, timeout=180) as resp:
                        rr = json.loads(resp.read())
                got = [h["sid"] for h in rr.get("hits", [])]
                rec.append((len(set(got) & truth) / float(K),
                            rr["route"]["mode"],
                            int(rr["route"]["items_scored"]),
                            int(rr["route"].get("fallback", 0))))
            results[arm] = rec
        finally:
            p.poll() or p.kill()
            time.sleep(1)

    off, on = results["off"], results["on"]
    ro = sum(x[0] for x in off) / len(off)
    rn = sum(x[0] for x in on) / len(on)
    worse = sum(1 for a, b in zip(off, on) if b[0] < a[0] - 1e-9)
    better = sum(1 for a, b in zip(off, on) if b[0] > a[0] + 1e-9)
    print()
    print("== A/B: graft fallback OFF vs ON (floor 2.1, %d queries) ==" % len(off))
    print("   recall@5  OFF=%.4f  ON=%.4f  (delta %+.4f)" % (ro, rn, rn - ro))
    print("   paired    better=%d same=%d worse=%d"
          % (better, len(off) - better - worse, worse))
    print("   modes OFF: %s" % sorted(set(x[1] for x in off)))
    print("   modes ON : %s" % sorted(set(x[1] for x in on)))
    print("   items_scored OFF=%.0f ON=%.0f"
          % (sum(x[2] for x in off) / len(off), sum(x[2] for x in on) / len(on)))
    print("   fallback count OFF=%d ON=%d"
          % (sum(x[3] for x in off), sum(x[3] for x in on)))
    print()
    if rn < ro - 0.005:
        print("VERDICT: the graft fallback LOSES RECALL -> keep it OFF")
        return 1
    print("VERDICT: the graft fallback is recall-neutral -> it may be enabled "
          "if the work saving is wanted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
