"""Shadow validation for ANN candidate receipts.

This module observes candidate topology receipts without changing search output.
Missing receipts are counted, not rejected; hard filtering is a later gate.
"""
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ValidationContext:
    generation: int
    totems: frozenset[int]
    frustum: int
    edges: frozenset[str]
    intent: str = "unknown"


def validate_receipt(receipt: dict, context: ValidationContext) -> list[str]:
    violations = []
    if receipt.get("generation") != context.generation:
        violations.append("stale_generation")
    if not isinstance(receipt.get("totem_id"), int) or receipt["totem_id"] not in context.totems:
        violations.append("totem_mismatch")
    if receipt.get("frustum_id") != context.frustum:
        violations.append("frustum_mismatch")
    if receipt.get("wang_edge") not in context.edges:
        violations.append("wang_edge_mismatch")
    if context.intent == "relation" and receipt.get("relation_valid") is not True:
        violations.append("relation_edge_mismatch")
    return violations


def shadow_validate(candidates: Iterable[dict], context: ValidationContext) -> dict:
    counts = {"checked": 0, "valid": 0, "invalid": 0, "missing_receipt": 0}
    findings = []
    for candidate in candidates:
        counts["checked"] += 1
        sid = candidate.get("sid")
        receipt = candidate.get("totem_receipt")
        if not isinstance(receipt, dict):
            counts["missing_receipt"] += 1
            findings.append({"sid": sid, "status": "missing_receipt", "violations": []})
            continue
        violations = validate_receipt(receipt, context)
        if violations:
            counts["invalid"] += 1
            status = "invalid"
        else:
            counts["valid"] += 1
            status = "valid"
        findings.append({"sid": sid, "status": status, "violations": violations})
    return {"mode": "shadow", "counts": counts, "findings": findings}
