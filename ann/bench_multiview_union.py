"""Measurement-only fixture for multi-view route unions.

It compares the two owner-approved interpretations without touching the ANN
server or production data:
 A: four totems are pattern/color/words/context;
 B: three triangle views are pattern/color/words and totem four is junction.

The vector scorer is the final authority. Route views only produce candidate
IDs; candidates are always UNIONed, never intersected.
"""
import argparse
import numpy as np


def norm(x):
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-12)


def top_ids(q, x, k):
    return np.argsort(-(x @ q))[:k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=10000)
    ap.add_argument("--queries", type=int, default=400)
    ap.add_argument("--dim", type=int, default=32)
    ap.add_argument("--route-k", type=int, default=256)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7818)
    ap.add_argument("--case", choices=("independent", "correlated", "dominant", "conflicting"), default="independent")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    base = rng.normal(size=(a.items, a.dim)).astype(np.float32)
    if a.case == "correlated":
        views = [norm((base + .20 * rng.normal(size=base.shape)).astype(np.float32)) for _ in range(4)]
    elif a.case == "dominant":
        views = [norm(base), norm((base + .90 * rng.normal(size=base.shape)).astype(np.float32)),
                 norm(rng.normal(size=base.shape).astype(np.float32)),
                 norm(rng.normal(size=base.shape).astype(np.float32))]
    elif a.case == "conflicting":
        # Three independent semantic neighborhoods.  The query combines
        # different targets per view, so a junction must recover a union, not
        # pretend that one fourth semantic axis owns the item.
        views = [norm(rng.normal(size=(a.items, a.dim)).astype(np.float32)) for _ in range(4)]
    else:
        views = [norm(rng.normal(size=(a.items, a.dim)).astype(np.float32)) for _ in range(4)]
    # Queries have one intended signal per view; final scoring uses the same
    # combined query against the combined item vector.
    qi = rng.choice(a.items, size=a.queries, replace=False)
    qv = [norm((views[v][qi] + .08 * rng.normal(size=(a.queries, a.dim))).astype(np.float32))
          for v in range(4)]
    if a.case == "dominant":
        qv[1] = norm((views[1][qi] + .08 * rng.normal(size=(a.queries, a.dim))).astype(np.float32))
        qv[2] = norm(rng.normal(size=(a.queries, a.dim)).astype(np.float32))
        qv[3] = norm(rng.normal(size=(a.queries, a.dim)).astype(np.float32))
    elif a.case == "conflicting":
        targets = [rng.choice(a.items, size=a.queries, replace=False) for _ in range(3)]
        qv = [norm((views[v][targets[v]] + .08 * rng.normal(size=(a.queries, a.dim))).astype(np.float32))
              for v in range(3)]
        qv.append(norm(rng.normal(size=(a.queries, a.dim)).astype(np.float32))
        )
    item_final = norm(sum(views).astype(np.float32))
    query_final = norm(sum(qv).astype(np.float32))
    truth = [set(top_ids(query_final[i], item_final, a.topk)) for i in range(a.queries)]

    def run(mode):
        hits = scored = exact = 0
        orders = []
        for i in range(a.queries):
            if mode == "A":
                routes = range(4)
            else:
                # Junction is a derived centroid route, not the fourth semantic
                # view: it sees the shared intersection of the three triangle
                # views and therefore must be measured separately.
                junction_x = norm((views[0] + views[1] + views[2]).astype(np.float32))
                junction_q = norm((qv[0] + qv[1] + qv[2]).astype(np.float32))
                parts = [top_ids(qv[v][i], views[v], a.route_k) for v in (0, 1, 2)]
                parts.append(top_ids(junction_q[i], junction_x, a.route_k))
                cand = np.unique(np.concatenate(parts))
                order = tuple(cand.tolist())
                orders.append(order)
                got = cand[np.argsort(-(item_final[cand] @ query_final[i]))[:a.topk]]
                hits += len(set(got) & truth[i]) / a.topk
                scored += len(cand)
                exact += set(got) == truth[i]
                continue
            parts = [top_ids(qv[v][i], views[v], a.route_k) for v in routes]
            cand = np.unique(np.concatenate(parts))
            order = tuple(cand.tolist())
            orders.append(order)
            got = cand[np.argsort(-(item_final[cand] @ query_final[i]))[:a.topk]]
            hits += len(set(got) & truth[i]) / a.topk
            scored += len(cand)
            exact += set(got) == truth[i]
        return hits / a.queries, scored / a.queries, exact / a.queries, orders

    for mode in ("A", "B"):
        r, n, e, o = run(mode)
        print(f"{a.case} {mode} {'4 views' if mode == 'A' else '3 views + junction'}: "
              f"R@{a.topk}={r:.4f} items_scored={n:.1f} exact={e:.4f} "
              f"deterministic={o == run(mode)[3]}")


if __name__ == "__main__":
    main()
