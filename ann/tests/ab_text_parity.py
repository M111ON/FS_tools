"""Text-path A/B: same queries, same order, from identical state copies.

Compares two ann_search binaries on plain-text queries (no NFKC-delta chars,
no unmappable chars) so any difference would mean the sanitizer change moved
ordinary text routing. Fresh state per binary via two copies of the corpus.

    python tests/ab_text_parity.py --a build/ann_search_v4.exe --b build/ann_search.exe
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

QUERIES = [
    "what is the opencode source path",
    "how does the dreamer lease work",
    "gguf roundtrip failure",
    "kv cache cold start trap",
    "wang latch totem routing",
    "anchor bucket count",
    "why did we choose sqlite",
    "hyper jump orbit length",
    "magic context map verify",
    "shawarma recipe",
    "tensor stride 37 helix",
    "who is the pool keeper",
]


def search(port, q, k=5, to=90):
    body = json.dumps({"query": q, "topk": k}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/state/search" % port, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=to) as r:
        js = json.loads(r.read().decode("utf-8"))
    return [h.get("sid", h.get("id")) for h in (js.get("hits") or [])]


def boot(exe, data, port, log):
    env = dict(os.environ)
    env.update({
        "ANN_PORT": str(port),
        "ANN_INDEX_PATH": os.path.join(data, "ann_index.jsonl"),
        "ANN_ANCH_PATH": os.path.join(data, "ann_anchors.bin"),
        "ANN_ORDER_PATH": os.path.join(data, "ann_index.order"),
        "ANN_TAGS_PATH": os.path.join(data, "ann_tags.tsv"),
        "ANN_ROUTE_LOG": os.path.join(data, "lz_route.log"),
    })
    p = subprocess.Popen([exe, "--serve", str(port)], env=env,
                         stdout=open(log, "wb"), stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=3) as r:
                js = json.loads(r.read().decode("utf-8"))
            if js.get("status") == "ok" and js.get("routed") == 1:
                return p, js
        except Exception:
            pass
        time.sleep(1)
    p.kill()
    sys.exit("server on %d never became healthy" % port)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="build/ann_search_v4.exe")
    ap.add_argument("--b", default="build/ann_search.exe")
    ap.add_argument("--data-a", default="I:/tmp/ab_data_v4")
    ap.add_argument("--data-b", default="I:/tmp/ab_data_new")
    ap.add_argument("--port-a", type=int, default=8101)
    ap.add_argument("--port-b", type=int, default=8102)
    a = ap.parse_args()

    pa, ha = boot(a.a, a.data_a, a.port_a, "I:/tmp/ab_text_a.log")
    pb, hb = boot(a.b, a.data_b, a.port_b, "I:/tmp/ab_text_b.log")
    print("A %s entries=%d k=%d | B %s entries=%d k=%d"
          % (os.path.basename(a.a), ha["entries"], ha.get("k", -1),
             os.path.basename(a.b), hb["entries"], hb.get("k", -1)))
    diff = 0
    try:
        for q in QUERIES:
            ra = search(a.port_a, q)
            rb = search(a.port_b, q)
            same = ra == rb
            diff += 0 if same else 1
            print("  %-32s %s  A=%s B=%s" % (
                q[:32], "SAME" if same else "DIFF",
                ",".join(map(str, ra[:2])), ",".join(map(str, rb[:2]))))
    finally:
        pa.kill()
        pb.kill()
    print("text parity: %d/%d identical" % (len(QUERIES) - diff, len(QUERIES)))
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
