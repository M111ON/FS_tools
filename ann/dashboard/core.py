"""Pure, read-only derivations for the ANN dashboard.

Invariants
----------
* No network and no filesystem access here. The server fetches bytes; every
  function below is a pure function of its arguments.
* Deterministic: same input -> same output. No clock, no randomness, no
  dict-ordering surprises (all output is explicitly sorted).
* Read-only: nothing is derived by mutating search or index state.
* Honest: a missing candidate receipt is reported as missing. It is never
  inferred from vector values, bucket ids, term_hint, file names or geometry.
"""
from __future__ import annotations

import json
import pathlib
import struct
import sys

MAX_TOPK = 64
MAX_DIM = 4096
MAX_SCOPE = 200
STAMP_BYTES = 8
ORDER_BYTES = 4

ANCHOR_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ANCHOR_ROOT) not in sys.path:
    sys.path.insert(0, str(ANCHOR_ROOT))

from totem_shadow_validate import ValidationContext, validate_receipt  # noqa: E402


class InputError(ValueError):
    """Malformed client input. The server maps this to HTTP 400."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# request parsing (server-side validation, never coercion)
# --------------------------------------------------------------------------

def parse_search_request(body):
    """Validate a dashboard search request. Returns a clean dict.

    `topk` is validated, never coerced: a string or a float is a client bug
    and must fail loudly (STRESS item: topk coercion).
    """
    if not isinstance(body, dict):
        raise InputError("body must be a JSON object")
    vec = body.get("vec")
    if not isinstance(vec, list) or not vec:
        raise InputError("vec must be a non-empty array of numbers")
    if len(vec) > MAX_DIM:
        raise InputError(f"vec longer than {MAX_DIM}")
    clean = []
    for index, value in enumerate(vec):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise InputError(f"vec[{index}] is not a number")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise InputError(f"vec[{index}] is not finite")
        clean.append(number)
    topk = body.get("topk", 5)
    if not _is_int(topk) or not 1 <= topk <= MAX_TOPK:
        raise InputError(f"topk must be an integer in 1..{MAX_TOPK}")
    scope = body.get("scope", "")
    if not isinstance(scope, str) or len(scope) > MAX_SCOPE:
        raise InputError("scope must be a string of at most 200 characters")
    return {"vec": clean, "topk": topk, "scope": scope}


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

_REQUIRED_HEALTH = ("status", "entries", "routed", "stamps_live", "route_dead")


def summarize_health(payload):
    """Normalize `/health` into a view plus explicit warnings.

    Missing keys are reported, never defaulted away: a dashboard that hides a
    missing field is how a dead route looked healthy before.
    """
    if not isinstance(payload, dict):
        raise InputError("health payload is not an object")

    missing = [key for key in _REQUIRED_HEALTH if key not in payload]
    out = {
        "ok": True,
        "reported_status": payload.get("status"),
        "entries": payload.get("entries"),
        "routed": payload.get("routed"),
        "stamps_live": payload.get("stamps_live"),
        "route_dead": payload.get("route_dead"),
        "maxk": payload.get("maxk"),
        "k": payload.get("k"),
        "labels": payload.get("labels"),
        "order_stale": payload.get("order_stale"),
        "fork_of": payload.get("fork_of"),
        "loader": payload.get("loader") if isinstance(payload.get("loader"), dict) else None,
        "tags": payload.get("tags") if isinstance(payload.get("tags"), dict) else None,
        "stamp": payload.get("stamp") if isinstance(payload.get("stamp"), dict) else None,
        "totem": payload.get("totem") if isinstance(payload.get("totem"), dict) else None,
        "roster": payload.get("roster") if isinstance(payload.get("roster"), dict) else None,
    }

    warnings = []
    if missing:
        warnings.append({"code": "missing_health_keys", "keys": missing})
    if payload.get("status") != "ok":
        warnings.append({"code": "health_status_not_ok", "value": payload.get("status")})
    if _is_int(payload.get("route_dead")) and payload["route_dead"] > 0:
        warnings.append({"code": "route_dead_nonzero", "value": payload["route_dead"]})
    if _is_int(payload.get("order_stale")) and payload["order_stale"] > 0:
        warnings.append({"code": "order_stale_nonzero", "value": payload["order_stale"]})
    stamp = out["stamp"]
    if isinstance(stamp, dict) and stamp.get("gate") not in (None, "ok"):
        warnings.append({"code": "stamp_gate_not_ok", "value": stamp.get("gate")})
    if isinstance(stamp, dict) and _is_int(stamp.get("unplaced")) and stamp["unplaced"] > 0:
        warnings.append({"code": "stamp_unplaced_nonzero", "value": stamp["unplaced"]})
    roster = out["roster"]
    if isinstance(roster, dict) and roster.get("on") == 0:
        warnings.append({"code": "roster_off", "value": 0})
    loader = out["loader"]
    if isinstance(loader, dict) and loader.get("mode") not in (None, "bg"):
        warnings.append({"code": "loader_not_background", "value": loader.get("mode")})
    for name in ("loader", "tags", "stamp", "totem", "roster"):
        if payload.get(name) is not None and out[name] is None:
            warnings.append({"code": "malformed_section", "section": name})
    out["warnings"] = warnings
    out["state"] = "degraded" if warnings else "live"
    return out


def unavailable_health(message):
    """The shape returned when :8096 cannot be reached at all."""
    return {
        "ok": False,
        "state": "unavailable",
        "error": {"kind": "upstream_unavailable", "message": str(message)},
        "warnings": [{"code": "upstream_unavailable"}],
    }


# --------------------------------------------------------------------------
# sidecar parsing
# --------------------------------------------------------------------------

def parse_stamps(data):
    """`.stamp` = 8 B per row: two int32 doors (anchor, anchor2)."""
    if len(data) % STAMP_BYTES:
        raise InputError("stamp sidecar length is not a multiple of 8")
    count = len(data) // STAMP_BYTES
    return [struct.unpack_from("<ii", data, offset * STAMP_BYTES) for offset in range(count)]


def parse_order(data):
    """`.order` = 4 B per row: the bucket-major walk permutation."""
    if len(data) % ORDER_BYTES:
        raise InputError("order sidecar length is not a multiple of 4")
    return len(data) // ORDER_BYTES


def bucket_occupancy(stamps, top=16):
    """Per-door membership counts, derived only from the append-time stamps.

    A row is posted under its own door and again under its centroid-pair door;
    both memberships are real work, so both are counted (`own` + `pair`).
    """
    own = {}
    pair = {}
    doors = set()
    negative_rows = 0
    for door, partner in stamps:
        if door < 0 or partner < 0:
            negative_rows += 1
            continue
        own[door] = own.get(door, 0) + 1
        doors.add(door)
        if partner != door:
            pair[partner] = pair.get(partner, 0) + 1
            doors.add(partner)

    rows = []
    for door in sorted(doors):
        own_count = own.get(door, 0)
        pair_count = pair.get(door, 0)
        rows.append({
            "door": door,
            "own": own_count,
            "pair": pair_count,
            "memberships": own_count + pair_count,
        })
    rows.sort(key=lambda row: (-row["memberships"], row["door"]))
    memberships = sum(row["memberships"] for row in rows)
    histogram = {}
    for row in rows:
        bucket = row["memberships"] // 1024 * 1024
        histogram[bucket] = histogram.get(bucket, 0) + 1
    return {
        "rows": len(stamps),
        "doors": len(doors),
        "memberships": memberships,
        "negative_rows": negative_rows,
        "top": rows[:top],
        "histogram": [
            {"floor": floor, "doors": histogram[floor]}
            for floor in sorted(histogram)
        ],
    }


def zone_distribution(tags_payload):
    """Zones from `/v1/state/tags`, with the sidecar's own totals checked."""
    if not isinstance(tags_payload, dict):
        raise InputError("tags payload is not an object")
    zones = tags_payload.get("zones")
    if not isinstance(zones, list):
        raise InputError("tags payload has no zones array")
    cleaned = []
    for zone in zones:
        if not isinstance(zone, dict):
            raise InputError("zone row is not an object")
        if not _is_int(zone.get("zone")) or not isinstance(zone.get("tag"), str):
            raise InputError("zone row is missing zone/tag")
        rows = zone.get("rows")
        if not _is_int(rows) or rows < 0:
            raise InputError("zone row has a non-integer row count")
        cleaned.append({
            "zone": zone["zone"],
            "tag": zone["tag"],
            "rows": rows,
            "retired": bool(zone.get("retired")),
        })
    cleaned.sort(key=lambda row: (row["zone"], row["tag"]))
    summed = sum(row["rows"] for row in cleaned)
    return {
        "zones": cleaned,
        "zones_n": len(cleaned),
        "sum_rows": summed,
        "entries": tags_payload.get("entries"),
        "tagged": tags_payload.get("tagged"),
        "unlabeled": tags_payload.get("unlabeled"),
        "lut_zones": tags_payload.get("lut_zones"),
        "coverage_ok": (
            _is_int(tags_payload.get("entries"))
            and summed == tags_payload.get("entries")
        ),
    }


def epoch_binding(index_stat, anchors_state):
    """Bind the derived sidecars to one index epoch (size + mtime).

    This is the cheap check that catches the 'index frozen for days' class of
    bug without hashing 260 MB on every page load.
    """
    if not isinstance(anchors_state, dict):
        return {"match": False, "reason": "anchors_state_missing"}
    size = index_stat.get("size")
    mtime = index_stat.get("mtime")
    state_size = anchors_state.get("index_size")
    state_mtime = anchors_state.get("index_mtime")
    match = size == state_size and mtime == state_mtime
    return {
        "match": match,
        "reason": "ok" if match else "stale_sidecar",
        "index_size": size,
        "index_mtime": mtime,
        "anchors_index_size": state_size,
        "anchors_index_mtime": state_mtime,
        "anchors_n_prev": anchors_state.get("n_prev"),
        "anchors_k": anchors_state.get("K"),
        "anchors_cksum": anchors_state.get("anchors_cksum"),
    }


# --------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------

def parse_receipt_lines(lines):
    """Parse a sid -> totem_receipt sidecar. Malformed rows are surfaced."""
    import json as _json

    receipts = {}
    malformed = []
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = _json.loads(raw)
        except ValueError as error:
            malformed.append({"line": number, "error": f"invalid json: {error}"})
            continue
        sid = row.get("sid") if isinstance(row, dict) else None
        receipt = row.get("totem_receipt") if isinstance(row, dict) else None
        if not isinstance(sid, str) or not isinstance(receipt, dict):
            malformed.append({"line": number, "error": "row needs sid + totem_receipt"})
            continue
        receipts[sid] = receipt
    return receipts, malformed


def classify_candidates(hits, receipts, context, bound=True, label="live"):
    """Per-hit receipt status: valid | invalid | stale | missing.

    `context` is a ValidationContext from the canonical validator, so the
    dashboard and the shadow probe cannot drift apart. When the sidecar is not
    bound to this index epoch every present receipt is reported `stale`.
    """
    if not isinstance(hits, list):
        raise InputError("hits is not an array")
    if not isinstance(receipts, dict):
        raise InputError("receipts is not an object")
    if not isinstance(context, ValidationContext):
        raise InputError("context is not a ValidationContext")

    findings = []
    counts = {"hits": len(hits), "valid": 0, "invalid": 0, "stale": 0, "missing": 0}
    for hit in hits:
        if not isinstance(hit, dict):
            findings.append({"sid": None, "status": "invalid", "violations": ["malformed_hit"]})
            counts["invalid"] += 1
            continue
        sid = hit.get("sid")
        receipt = receipts.get(sid) if isinstance(sid, str) else None
        if not isinstance(receipt, dict):
            counts["missing"] += 1
            findings.append({
                "sid": sid,
                "file": hit.get("file"),
                "score": hit.get("score"),
                "status": "missing",
                "violations": [],
                "reason": "no producer-owned receipt for this sid",
            })
            continue
        if not bound:
            counts["stale"] += 1
            findings.append({
                "sid": sid,
                "file": hit.get("file"),
                "score": hit.get("score"),
                "status": "stale",
                "violations": ["index_binding_mismatch"],
                "receipt": receipt,
            })
            continue
        violations = validate_receipt(receipt, context)
        if "stale_generation" in violations:
            status = "stale"
        elif violations:
            status = "invalid"
        else:
            status = "valid"
        counts[status] += 1
        findings.append({
            "sid": sid,
            "file": hit.get("file"),
            "score": hit.get("score"),
            "status": status,
            "violations": violations,
            "receipt": receipt,
        })

    coverage = 0.0
    if counts["hits"]:
        coverage = round((counts["valid"] + counts["invalid"] + counts["stale"]) / counts["hits"], 4)
    return {
        "label": label,
        "bound": bound,
        "counts": counts,
        "present_coverage": coverage,
        "findings": findings,
    }


# --------------------------------------------------------------------------
# shadow timeline
# --------------------------------------------------------------------------

_SHADOW_FIELDS = (
    "event_time", "returned_hits", "candidate_receipts", "candidate_receipts_missing",
    "route_receipt", "route_violations", "walk_hash", "items_scored", "items_total",
)


def shadow_summary(lines, max_timeline=50):
    """Summarize the append-only shadow report. Malformed rows stay visible."""
    reports = []
    malformed = []
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as error:
            malformed.append({"line": number, "error": f"invalid json: {error}"})
            continue
        if not isinstance(row, dict):
            malformed.append({"line": number, "error": "report row is not an object"})
            continue
        if "event_time" not in row:
            malformed.append({"line": number, "error": "report row has no event_time"})
            continue
        reports.append(row)

    violations = []
    missing_total = 0
    present_total = 0
    hits_total = 0
    for row in reports:
        row_violations = row.get("route_violations")
        if isinstance(row_violations, list):
            violations.extend(row_violations)
        elif row_violations is not None:
            malformed.append({
                "line": None,
                "event_time": row.get("event_time"),
                "error": "route_violations is not an array",
            })
        if _is_int(row.get("candidate_receipts_missing")):
            missing_total += row["candidate_receipts_missing"]
        if _is_int(row.get("candidate_receipts")):
            present_total += row["candidate_receipts"]
        if _is_int(row.get("returned_hits")):
            hits_total += row["returned_hits"]

    timeline = []
    for row in reports[-max_timeline:]:
        entry = {"event_time": row.get("event_time")}
        for field in _SHADOW_FIELDS[1:]:
            entry[field] = row.get(field)
        timeline.append(entry)

    coverage = 0.0
    if hits_total:
        coverage = round(present_total / hits_total, 4)
    return {
        "reports": len(reports),
        "malformed": malformed,
        "route_violations": sorted(set(str(item) for item in violations)),
        "route_violation_count": len(violations),
        "candidate_receipts": present_total,
        "candidate_receipts_missing": missing_total,
        "returned_hits": hits_total,
        "coverage": coverage,
        "first_event_time": reports[0]["event_time"] if reports else None,
        "last_event_time": reports[-1]["event_time"] if reports else None,
        "timeline": timeline,
    }


def deterministic_json(payload):
    """Stable serialization used by the tests to prove rendering is stable."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))