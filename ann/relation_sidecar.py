"""Offline consumer for the v1 relation route sidecar.

This module deliberately consumes only ``relation_edges``.  Relation queries
must not fall back to, or union with, value/shape/junction routes.
"""
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from validate_route_sidecar import validate


@dataclass(frozen=True, order=True)
class RelationEdge:
    """A producer-owned typed relation edge."""

    source: str
    target: str
    kind: str
    direction: str


class RelationGraph:
    """Validated relation graph with deterministic, bounded traversal."""

    def __init__(self, rows):
        self._adj = defaultdict(list)
        for row in rows:
            source = row["sid"]
            for edge in row.get("relation_edges", []):
                relation = RelationEdge(source, edge["to"], edge["kind"], edge["dir"])
                self._adj[source].append(relation)
        for source in self._adj:
            self._adj[source].sort(key=lambda e: (e.direction, e.kind, e.target))

    def walk(self, start, *, depth=2, cap=64, kind=None, direction="out"):
        """Return SIDs reached through typed relation edges only.

        ``cap`` includes ``start`` and is enforced in sorted edge order.
        Reverse traversal is materialized without changing the sidecar.
        """
        if depth < 0 or cap < 1:
            raise ValueError("depth must be non-negative and cap must be positive")
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be out, in, or both")
        edges = []
        for source, outgoing in self._adj.items():
            for edge in outgoing:
                if kind is None or edge.kind == kind:
                    edges.append(edge)
        if direction in {"in", "both"}:
            edges.extend(RelationEdge(e.target, e.source, e.kind, "in") for e in edges)
        if direction == "out":
            edges = [e for e in edges if e.direction == "out"]
        elif direction == "in":
            edges = [e for e in edges if e.direction == "in"]
        adjacency = defaultdict(list)
        for edge in edges:
            adjacency[edge.source].append(edge.target)
        for values in adjacency.values():
            values.sort()
        seen, queue = {start}, deque([(start, 0)])
        while queue and len(seen) < cap:
            node, distance = queue.popleft()
            if distance >= depth:
                continue
            for target in adjacency[node]:
                if target not in seen:
                    seen.add(target)
                    queue.append((target, distance + 1))
                    if len(seen) >= cap:
                        break
        return seen


def load(sidecar, manifest):
    """Verify the manifest and return ``(RelationGraph, digest)``.

    Validation failures are intentionally raised: callers can select their
    safe baseline rather than enabling partially trusted routing.
    """
    sidecar = Path(sidecar)
    manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
    rows, digest = validate(sidecar)
    if manifest.get("v") != 1 or manifest.get("rows") != rows:
        raise ValueError("manifest row count or version mismatch")
    if manifest.get("sha256") != digest:
        raise ValueError("sidecar sha256 mismatch")
    parsed = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    return RelationGraph(parsed), digest
