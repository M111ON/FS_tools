"""Observe live ANN responses without changing returned hits."""
import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def load_receipts(path):
    if not path:
        return {}
    receipts = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if raw.strip():
            row = json.loads(raw)
            receipt = row.get("totem_receipt")
            if not isinstance(row.get("sid"), str) or not isinstance(receipt, dict):
                raise ValueError("invalid candidate receipt row")
            receipts[row["sid"]] = receipt
    return receipts


def probe(url, vector, scope="", topk=5, receipts=None):
    body = json.dumps({"vec": vector, "topk": topk, "scope": scope}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.load(response)
    route = payload.get("route", {})
    hits = payload.get("hits", [])
    receipts = receipts or {}
    totem = route.get("totem")
    buckets = route.get("buckets", [])
    route_violations = []
    if isinstance(totem, dict) and isinstance(totem.get("lit"), int):
        if totem["lit"] != len(buckets):
            route_violations.append("totem_lit_bucket_count_mismatch")
    elif totem is None:
        route_violations.append("missing_route_totem")
    result = {
        "event_time": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow",
        "returned_hits": len(hits),
        "hits_unchanged": True,
        "route_receipt": "present" if isinstance(totem, dict) else "missing",
        "route_violations": route_violations,
        "candidate_receipts": 0,
        "candidate_receipts_missing": 0,
        "candidate_receipt_findings": [],
        "walk_hash": route.get("walk_hash"),
        "items_scored": route.get("items_scored"),
        "items_total": route.get("items_total"),
        "totem": totem,
    }
    for hit in hits:
        sid = hit.get("sid")
        receipt = receipts.get(sid)
        if receipt is None:
            result["candidate_receipts_missing"] += 1
            result["candidate_receipt_findings"].append({"sid": sid, "status": "missing"})
        else:
            result["candidate_receipts"] += 1
            result["candidate_receipt_findings"].append({
                "sid": sid, "status": "present", "receipt": receipt})
    return result


def append_report(path, report):
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8096/v1/state/search")
    ap.add_argument("--scope", default="")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--report", default="")
    ap.add_argument("--receipts", default="")
    args = ap.parse_args()
    report = probe(args.url, [0.0] * args.dim, args.scope, args.topk,
                    load_receipts(args.receipts))
    if args.report:
        append_report(args.report, report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
