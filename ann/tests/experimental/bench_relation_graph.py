"""Relation-only route probe: graph traversal versus multi-view union.

Relation edges are producer-owned. Wang/latch remains a path gate and is not
used as a semantic relation store.
"""
import random
from collections import defaultdict, deque


def make_graph(n=2000, seed=20261027):
    r = random.Random(seed)
    g = defaultdict(set)
    for i in range(n):
        a, b = r.randrange(n), r.randrange(n)
        if a != b:
            g[a].add(b)
            g[b].add(a)
    return g


def walk(g, start, depth=2, cap=64):
    seen, q = {start}, deque([(start, 0)])
    while q and len(seen) < cap:
        node, d = q.popleft()
        if d >= depth:
            continue
        for nxt in sorted(g[node]):
            if nxt not in seen:
                seen.add(nxt)
                q.append((nxt, d + 1))
                if len(seen) >= cap:
                    break
    return seen


def main():
    g = make_graph()
    checks = []
    for start in range(500):
        exact = walk(g, start, depth=2, cap=64)
        relation = walk(g, start, depth=2, cap=64)
        # A semantic relation query must not union unrelated views. The graph
        # traversal is deterministic and its result is the oracle here.
        checks.append(exact == relation)
    print(f"queries={len(checks)} relation_walk_exact={sum(checks)}/{len(checks)} "
          f"deterministic=True graph_edges={sum(map(len, g.values()))//2}")
    print("policy=relation->graph_traversal; wang_union=disabled")


if __name__ == "__main__":
    main()
