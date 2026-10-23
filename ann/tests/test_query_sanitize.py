# -*- coding: utf-8 -*-
"""test_query_sanitize.py — the DSML/fullwidth/NFKC-expand class must not 503.

Measured on the live sidecar (:18095, 2026-10-22): every char whose NFKC
EXPANDS to several chars 500s raw (№ ℀ ½ Ⅰ Ⅻ ⅕ … fullwidth ＠ ｱ), while
single-char symbols pass (→ ✓ ° × ÷ « ′ ‰ † · ♪ emoji CJK boxdraw bengali).
The corpus build (scripts/build_index.py) NFKC's every row before embed, so
the QUERY side must land in the same space — lz_sanitize does the parity.

Runs against the REAL server on :port (default 8097 scratch server):
  python tests/test_query_sanitize.py [--url http://127.0.0.1:8096]
"""
import argparse
import json
import sys
import unicodedata
import urllib.request

# chars that MUST embed (they did NOT expand -> sidecar passes them)
MUST_PASS = "→✓✅°—·†‰×÷«′♪👍中文হ┬Ⅱ≠"
# chars that MUST NOT 503 after sanitize (formerly the 500 class)
MUST_NOT_503 = "＃＄％＆｜＠ｱن№℀℁¼½¾…ⅠⅡⅫⅬⅱ⅕⅗⅑⅟𝕏ℝ𿻻"


def norm_like_corpus(t: str) -> str:
    """What build_index.py did to every corpus row: NFKC + the 12-symbol map."""
    t = unicodedata.normalize("NFKC", t)
    for bad, good in (
        ("∉", " not in "), ("∋", " contains "), ("∖", " minus "),
        ("∝", " proportional to "), ("∤", " does not divide "),
        ("≅", " ~= "), ("⋀", " AND "), ("⋁", " OR "),
        ("⏭", " next "), ("⏸", " pause "), ("⟺", " <=> "),
        ("⬠", " pentagon "),
    ):
        if bad in t:
            t = t.replace(bad, good)
    return t


def search(url, query, to=60):
    body = json.dumps({"query": query, "topk": 1}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/state/search", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=to) as r:
            js = json.loads(r.read().decode("utf-8"))
            return 200 if js.get("status") == "ok" else 1
    except urllib.error.HTTPError as e:
        js = json.loads(e.read().decode("utf-8"))
        if js.get("error") == "embed_unavailable":
            return 503
        return e.code
    except Exception as ex:
        return f"transport {ex}"

def top1_sid(url, query, to=60):
    body = json.dumps({"query": query, "topk": 1}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/state/search", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=to) as r:
        js = json.loads(r.read().decode("utf-8"))
    hits = js.get("hits") or []
    if not hits:
        return None
    return hits[0].get("sid") if "sid" in hits[0] else hits[0].get("id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8096")
    ap.add_argument("--deep", type=int, default=0,
                    help="random-sample N of the full NFKC-delta universe")
    args = ap.parse_args()

    fails = []
    for ch in MUST_PASS:
        rc = search(args.url, f"probe {ch} alpha")
        if rc != 200:
            fails.append(f"MUST_PASS '{ch}' -> {rc}")
    for ch in MUST_NOT_503:
        rc = search(args.url, f"probe {ch} beta")
        if rc == 503 or rc == 500:
            fails.append(f"MUST_NOT_503 '{ch}' -> {rc}")

    # parity: for every corpus-side NFKC expansion, the query must land where
    # its NFKC'd corpus twin lives (NOT a fresh 503 at least)
    for ch in MUST_NOT_503:
        nfc = norm_like_corpus(ch)
        if nfc == ch:
            continue  # not an expansion on this builder's map — skip parity
        rc = search(args.url, f"parity {ch} gamma")
        if rc == 503:
            fails.append(f"parity '{ch}' (nfkc -> '{nfc}') -> 503")

    # sid-parity: raw delta query must return the SAME top doc as its NFKC'd
    # twin (both sanitize to the identical string -> identical embed vector).
    try:
        for ch in MUST_NOT_503:
            nfc = norm_like_corpus(ch)
            if nfc == ch:
                continue
            a = top1_sid(args.url, f"parity {ch} gamma")
            b = top1_sid(args.url, f"parity {nfc} gamma")
            if a is not None and b is not None and a != b:
                fails.append(f"sid-parity '{ch}' raw top={a} nfkc top={b}")
    except Exception as ex:
        fails.append(f"sid-parity harness {ex}")

    # random-sample stress over the FULL NFKC-delta universe (server must not
    # 503 any of them — the generated table is supposed to cover all of them)
    if args.deep:
        import random
        deltas = [c for c in range(0x20, 0x10000)
                  if unicodedata.normalize("NFKC", chr(c)) != chr(c)]
        random.seed(22726)
        for c in random.sample(deltas, min(args.deep, len(deltas))):
            ch = chr(c)
            rc = search(args.url, f"deep {ch} delta")
            if rc == 503 or rc == 500:
                fails.append(f"deep-delta U+{c:04X} -> {rc}")

    n_pass = len(MUST_PASS.encode("utf-16-le").decode("utf-16-le"))
    if fails:
        print(f"test_query_sanitize FAIL {len(fails)}/{len(MUST_PASS)+len(MUST_NOT_503)}:")
        for f in fails:
            print(" ", f)
        return 1
    print(f"test_query_sanitize OK ({len(MUST_PASS)} pass-class and "
          f"{len(MUST_NOT_503)} no-503 chars all clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
