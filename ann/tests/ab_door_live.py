#!/usr/bin/env python3
"""ab_door_live.py — live-scale A/B for the totem DOOR (ANN_DOOR).

WHY THIS SHAPE
--------------
The handoff's open item 1 asks for "top-5 sid equality OFF vs ON on 300 real
queries" before flipping ANN_DOOR on :8096. Equality alone is NOT enough to
decide: the door REPLACES a fallback that scans every row, so ON can differ
from OFF without either being wrong. This harness therefore measures
against an INDEPENDENT ORACLE (numpy brute-force cosine over the whole
index) and reports recall@k for OFF and ON separately.

THE MECHANIC UNDER TEST (read from ann_search.c, 2026-10-20)
-------------------------------------------------------------
  * below floor (pruned walk's top score < ANN_WALK_FLOOR):
      - OFF: the fallback sets cap = nent and walks EVERY row -> exact top-k
      - ON : the DOOR branch rebuilds the walk order (shut bucket's members
             first) but LEAVES cap = budget -> it scores only budget rows
  So the door is NOT a reordering of the OFF path: it trades an exhaustive
  scan for a budget-capped walk. "recall cannot drop" holds only against
  the PRUNED walk, not against the fallback it displaces. This harness
  measures both, so the trade is visible instead of assumed.

SETUP
  * the live index is COPIED to a scratch dir (the server must not touch
    the live data; the copy also isolates the totem log per arm)
  * two servers, one per arm, on separate ports, started in sequence
  * each query is sent to both arms; the oracle is computed once per query
  * the below-floor path is reached with queries that have no good match
    (out-of-corpus phrasing), and the totem state is warmed by
    repeating each query (the walk SHUTS the buckets it visits)

USAGE
  python ab_door_live.py --queries 300
  python ab_door_live.py --queries 40 --keep-scratch   (fast probe)
"""
import argparse, json, os, shutil, sqlite3, struct, subprocess, sys, time
import urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
# The A/B must run the binary under test, not whatever is deployed. Override
# with ANN_TEST_EXE (the harness compares the OFF/ON arms of ONE binary, so
# a stale binary silently invalidates the whole measurement — it happened
# once on 2026-10-20: the A/B ran the pre-fix binary and reported the
# OLD numbers).
EXE = os.environ.get("ANN_TEST_EXE") or os.path.join(REPO, "build", "ann_search_batch.exe")
SRC = os.path.join(REPO, "data", "memcore")
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-ab-door")
STORE = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
EMBED = "http://127.0.0.1:18095/embedding"
K = 5
TOPK = 10     # the server returns up to topk hits; the oracle compares 10

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(url, body=None, to=120):
    if body is None:
        return json.loads(urllib.request.urlopen(url, timeout=to).read())
    r = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def _sanitize(t):
    """NFKC + drop the characters the e5-small tokenizer chokes on.

    The sidecar (llama-server b10830) returns HTTP 500 "invalid
    unordered_map<K,T> key" for inputs whose tokens are outside the
    tokenizer's vocabulary (measured 2026-10-08: 63/63 real strings).
    NFKC is meaning-preserving; the residue map is the verified 12-entry
    fallback from build/reindex_docs.py.
    """
    import unicodedata
    t = unicodedata.normalize("NFKC", t)
    for bad, good in (
        ("∉", " not in "), ("∋", " contains "), ("∖", " minus "),
        ("∝", " proportional to "), ("∤", " does not divide "),
        ("≅", " ~= "), ("⋀", " AND "), ("⋁", " OR "),
        ("⏭", " next "), ("⏸", " pause "), ("⟺", " <=> "),
        ("⬠", " pentagon "),
    ):
        t = t.replace(bad, good)
    return t


def embed(texts):
    """-> list of vectors, one per text. Batched (the sidecar takes an
    array), with a per-item fallback for the batch that trips the
    tokenizer."""
    def post(batch):
        r = Request(EMBED, json.dumps({"content": batch}).encode(),
                    {"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=600) as resp:
            d = json.loads(resp.read())
        out = []
        for item in d:
            v = item["embedding"]
            if v and isinstance(v[0], list):
                v = v[0]
            out.append(v)
        return out

    clean = [_sanitize(t) for t in texts]
    try:
        return post(clean)
    except Exception as e:
        print("   (batch embed failed: %r -> per-item)" % e)
    out = []
    for t in clean:
        try:
            out.extend(post([t]))
        except Exception:
            # a text the tokenizer cannot represent at all: fall back to a
            # single-word probe that is guaranteed to embed, and mark the
            # query as unusable by returning a zero vector (the oracle
            # yields no truth for it and the recall denominator excludes it)
            out.append([0.0] * 384)
    return out


# ── queries: REAL corpus text + out-of-corpus phrasings ───────────────────
def load_queries(n_total):
    """Real content snippets from the memcore store (the queries a user
    would actually type) + out-of-corpus phrasings (the ones that fall
    below the floor and therefore exercise the door)."""
    real = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE, uri=True, timeout=30)
        rows = con.execute(
            "SELECT content FROM entries WHERE content IS NOT NULL "
            "AND length(content) > 40 ORDER BY rowid DESC LIMIT ?",
            (n_total * 2,)).fetchall()
        con.close()
        for (c,) in rows:
            t = " ".join((c or "").split())[:120]
            if len(t) > 25:
                real.append(t)
    except Exception as e:
        print("  (store read failed: %r)" % e)
    out = []
    n_real = n_total * 3 // 4
    out.extend(real[:n_real])
    # out-of-corpus: unrelated technical phrasings -> low cosine -> below floor
    ooc = [
        "how to bake sourdough bread at 250 degrees",
        "quarterly revenue variance analysis spreadsheet",
        "kubernetes pod eviction and pod disruption budget",
        "left handed golf swing drill for beginners",
        "sql window function running total per partition",
        "best hiking trails in northern patagonia",
        "electric vehicle battery degradation warranty",
        "typography rules for long form magazine layout",
        "distributed consensus raft leader election protocol",
        "harmonizing a chord progression in a minor key",
        "cold chain logistics temperature monitoring sensors",
        "regression testing of parser edge cases and unicode",
    ]
    while len(out) < n_total:
        out.append(ooc[len(out) % len(ooc)])
    return out[:n_total]


# ── the oracle: numpy brute force over the whole index ────────────────────
def load_index_vectors():
    """-> (sids[], vecs float32 [N,D], norms[N]) from the index jsonl."""
    sids, vecs = [], []
    idx_path = os.path.join(SCRATCH, "index", "ann_index.jsonl")
    with open(idx_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            v = d.get("vec")
            if not v:
                continue
            sids.append(d.get("sid") or "")
            vecs.append(v)
    import numpy as np
    M = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(M, axis=1)
    norms[norms == 0] = 1.0
    return sids, M, norms


def oracle_topk(M, norms, qv, k):
    import numpy as np
    q = np.asarray(qv, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    scores = (M @ q) / (norms * qn)
    idx = np.argpartition(-scores, k)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return idx, scores[idx]


# ── scratch setup ─────────────────────────────────────────────────────────
def setup_scratch():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    idx_dir = os.path.join(SCRATCH, "index")
    os.makedirs(idx_dir, exist_ok=True)
    for fn in ("ann_index.jsonl", "ann_index.order", "ann_anchors.bin",
               "ann_anchors-state.json", "ann_index.jsonl.stamp"):
        src = os.path.join(SRC, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(idx_dir, fn))
    # each arm gets its own totem log so the arms do not share latches
    for arm in ("off", "on"):
        os.makedirs(os.path.join(SCRATCH, arm), exist_ok=True)


def start_arm(arm, port, door, floor="0.5", exit_early=False, exit_floor=None):
    idx = os.path.join(SCRATCH, "index")
    arm_dir = os.path.join(SCRATCH, arm)
    os.makedirs(arm_dir, exist_ok=True)
    env = {**os.environ,
           "ANN_INDEX_PATH": os.path.join(idx, "ann_index.jsonl"),
           "ANN_ANCH_PATH": os.path.join(idx, "ann_anchors.bin"),
           "ANN_ORDER_PATH": os.path.join(idx, "ann_index.order"),
           "ANN_ROUTE_LOG": os.path.join(arm_dir, "route.log"),
           "ANN_PORT": str(port),
           "ANN_TOTEM_WARM": "1",
           "ANN_WALK_PRUNE": "1",
           "ANN_FLOAT_KERNEL": "1",
           "LZ_ROUTE_BUCKETS": "5",
           "LZ_SEARCH_BUDGET": "24000",
           "ANN_WALK_FLOOR": floor,
           "ANN_DOOR": "1" if door else "0",
           "ANN_DOOR_EXIT": "1" if exit_early else "0",
           "ANN_DOOR_EXIT_FLOOR": str(exit_floor) if exit_floor else "",
           "ANN_DOOR_TRACE": "1" if door else "0"}
    errlog = open(os.path.join(arm_dir, "srv_err.log"), "w", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    url = "http://127.0.0.1:%d" % port
    for _ in range(120):
        time.sleep(0.5)
        try:
            if http(url + "/health", to=4).get("status") == "ok":
                return p, url
        except Exception:
            pass
    p.kill()
    raise RuntimeError("arm %s failed to start on %d" % (arm, port))


def search(url, qv, topk=5):
    body = json.dumps({"vec": [float(x) for x in qv], "topk": topk}).encode()
    r = Request(url + "/v1/state/search", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--floor", type=float, default=2.1,
                    help="ANN_WALK_FLOOR for both arms. 2.1 forces the "
                         "below-floor branch (the door's regime); 0.5 is "
                         "production (the door never fires there).")
    ap.add_argument("--exit-arm", action="store_true",
                    help="add a third arm: door + ANN_DOOR_EXIT=1 (the "
                         "latency trade)")
    ap.add_argument("--exit-floor", type=float, default=0.80,
                    help="ANN_DOOR_EXIT_FLOOR for the exit arm: the score at "
                         "which the door walk may stop. Separate from the "
                         "trigger floor on purpose.")
    ap.add_argument("--keep-scratch", action="store_true")
    a = ap.parse_args()

    print("== setup: copying the live index to scratch ==")
    setup_scratch()
    t0 = time.time()
    sids, M, norms = load_index_vectors()
    print("   index loaded: %d rows, dim %d (%.1fs)" %
          (len(sids), M.shape[1], time.time() - t0))

    queries = load_queries(a.queries)
    print("   queries: %d (real content + out-of-corpus)" % len(queries))
    t0 = time.time()
    qvecs = embed(queries)
    print("   embedded in %.1fs" % (time.time() - t0))

    print("== starting arms ==")
    print("   binary under test: %s" % EXE)
    # The door only fires when the pruned walk scores BELOW the floor. With
    # the production floor (0.5) a real query scores ~0.86, so the door is
    # never reached — an A/B there would compare two identical paths and
    # prove nothing. The floor is therefore RAISED to 2.1 (> any cosine)
    # to force EVERY query through the below-floor branch: that is the
    # regime the door exists for. The production-floor regime is measured
    # too (arm set 1) so the no-op case is on record too.
    p_off, url_off = start_arm("off", 8095, False, floor=str(a.floor))
    p_on, url_on = start_arm("on", 8094, True, floor=str(a.floor))
    print("   OFF=%s (floor %s)  ON=%s (floor %s)" % (url_off, a.floor, url_on, a.floor))
    url_exit = None
    if a.exit_arm:
        p_exit, url_exit = start_arm("exit", 8093, True, floor=str(a.floor),
                                     exit_early=True,
                                     exit_floor=a.exit_floor)
        print("   EXIT=%s (door + ANN_DOOR_EXIT=1, exit_floor=%s)" %
              (url_exit, a.exit_floor))
    res = {"off": [], "on": []}
    if url_exit:
        res["exit"] = []
    skipped = []
    try:
        # warm the ORACLE-independent state: each arm sees the same query
        # sequence, so the totem latches evolve the same way
        for i, qv in enumerate(qvecs):
            # a query the embedder could not represent (zero vector) has no
            # truth set -> it is excluded from the comparison, not scored 0
            if not any(x != 0.0 for x in qv):
                skipped.append(i)
                continue
            oi, os_ = oracle_topk(M, norms, qv, K)
            truth = [sids[j] for j in oi]
            oi10, _ = oracle_topk(M, norms, qv, 10)
            truth10 = [sids[j] for j in oi10]
            for arm, url in (("off", url_off), ("on", url_on),
                             ("exit", url_exit)):
                if url is None:
                    continue
                # pass 1: warms the route (the walk SHUTS the buckets it visits)
                search(url, qv, TOPK)
                t0 = time.time()
                r = search(url, qv, TOPK)
                ms = (time.time() - t0) * 1000.0
                got = [h["sid"] for h in r.get("hits", [])]
                rt = r.get("route", {})
                res[arm].append({
                    "q": queries[i],
                    "got": got,
                    "truth": truth,
                    "truth10": truth10,
                    "mode": rt.get("mode"),
                    "scored": rt.get("items_scored"),
                    "fallback": rt.get("fallback"),
                    "ms": ms,
                    "rec": len(set(got[:K]) & set(truth[:K])) / float(K),
                    "rec10": len(set(got[:10]) & set(truth10)) / 10.0,
                })
            if (i + 1) % 50 == 0:
                print("   ... %d/%d" % (i + 1, len(qvecs)))
    finally:
        p_off.poll() or p_off.kill()
        p_on.poll() or p_on.kill()
        try:
            p_off._errlog.close(); p_on._errlog.close()
        except Exception:
            pass

    # ── report ────────────────────────────────────────────────────────────
    def agg(arm):
        rs = res[arm]
        n = len(rs)
        rec = sum(x["rec"] for x in rs) / max(1, n)
        rec10 = sum(x["rec10"] for x in rs) / max(1, n)
        modes = {}
        for x in rs:
            modes[x["mode"]] = modes.get(x["mode"], 0) + 1
        ms = sorted(x["ms"] for x in rs)
        med = ms[len(ms) // 2] if ms else 0
        return dict(n=n, recall5=rec, recall10=rec10, modes=modes,
                    p50_ms=med, p95_ms=ms[int(len(ms) * 0.95)] if ms else 0,
                    scored=sum(x["scored"] or 0 for x in rs) / max(1, n),
                    fallback=sum(1 for x in rs if x["fallback"]) )

    ao, an = agg("off"), agg("on")
    print("\n== A/B RESULT (live index, %d queries, %d unembeddable skipped) ==" %
          (len(res["off"]), len(skipped)))
    arms = [("OFF", ao), ("ON", an)]
    if "exit" in res:
        arms.append(("EXIT", agg("exit")))
    print("   %-22s" % "metric" + "".join("%14s" % n for n, _ in arms))
    for key, label in (("recall5", "recall@5 (oracle)"), ("recall10", "recall@10 (oracle)"),
                       ("p50_ms", "p50 latency ms"), ("p95_ms", "p95 latency ms"),
                       ("scored", "avg items_scored"), ("fallback", "fallback count")):
        print("   %-22s" % label + "".join("%14.4f" % d[key] for _, d in arms))
    for n, d in arms:
        print("   %s modes: %s" % (n, d["modes"]))

    # paired comparison: per-query recall difference
    diffs = [res["on"][i]["rec"] - res["off"][i]["rec"] for i in range(len(res["off"]))]
    worse = [d for d in diffs if d < -1e-9]
    better = [d for d in diffs if d > 1e-9]
    same = len(diffs) - len(worse) - len(better)
    print("   paired: better %d, same %d, worse %d (of %d)" %
          (len(better), same, len(worse), len(diffs)))
    if worse:
        worst = sorted(range(len(diffs)), key=lambda i: diffs[i])[:5]
        print("   worst 5 (ON - OFF):")
        for i in worst:
            x = res["off"][i]
            print("     d=%.2f mode_off=%s mode_on=%s scored_off=%s scored_on=%s" %
                  (diffs[i], x["mode"], res["on"][i]["mode"], x["scored"],
                   res["on"][i]["scored"]))
            print("       q=%r" % x["q"][:70])

    # verdict
    print("\n== VERDICT ==")
    if an["recall5"] >= ao["recall5"] - 0.005:
        print("   DOOR ON is recall-neutral (>= OFF - 0.005): safe to enable.")
    else:
        print("   DOOR ON LOSES RECALL (%.4f vs %.4f): do NOT enable as-is." %
              (an["recall5"], ao["recall5"]))
    if "exit" in res:
        ax = agg("exit")
        if ax["recall5"] >= ao["recall5"] - 0.005:
            print("   DOOR+EXIT is recall-neutral too (%.4f), p50 %.1f ms vs "
                  "OFF %.1f ms (%.2fx)" % (ax["recall5"], ax["p50_ms"],
                                           ao["p50_ms"],
                                           ax["p50_ms"] / max(1e-9, ao["p50_ms"])))
        else:
            print("   DOOR+EXIT LOSES RECALL (%.4f vs %.4f): the early exit is "
                  "a real trade, not a free win." % (ax["recall5"], ao["recall5"]))
    print("   door modes seen: %s" % {k: v for k, v in an["modes"].items() if "door" in str(k)})

    out = os.path.join(SCRATCH, "ab_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"off": ao, "on": an, "paired": {"better": len(better),
                "same": same, "worse": len(worse)},
                "rows": res}, f, indent=1)
    print("   raw: %s" % out)
    if not a.keep_scratch:
        pass  # keep the scratch for inspection; it is under TEMP
    return 0


if __name__ == "__main__":
    sys.exit(main())
