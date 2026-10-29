"""phase_c_promotion.py — split a full bucket without moving anyone else.

Phase B said: learn the partition once, freeze it, append. Its cost was buckets
filling up (largest 3.4% -> 9.0% while the corpus grew 2.5x). This is the answer
to that: when a bucket exceeds capacity, open ONE more chain level under it.

The rule under test (owner spec): a bigger container wraps the old one, the old
data keeps running inside, nothing is deleted. So promotion must EXTEND an
address, never rewrite it. A vector in a bucket that splits gets a longer address
whose prefix is exactly its old one; a vector in any other bucket must not be
touched at all.

Measured over a stream:
  - recall@10 as the stream grows and splits happen
  - leaf addresses rewritten   (should be 0 for unsplit buckets)
  - prefix addresses broken    (must be 0, always)
  - scan fraction

Run: python phase_c_promotion.py [n_rows] [capacity]
"""
import sys

import numpy as np

from phase_b_gate import K, PROBES, TOPK, kmeans, load


class Node:
    __slots__ = ("id", "parent", "centroid", "members", "children")

    def __init__(self, nid, parent, centroid):
        self.id = nid
        self.parent = parent
        self.centroid = centroid
        self.members = []
        self.children = []


class Container:
    def __init__(self, centroids):
        self.nodes = []
        for c in centroids:
            self.nodes.append(Node(len(self.nodes), -1, c))
        self.leaf_of = {}          # row -> leaf node id

    def leaves(self):
        return [n for n in self.nodes if not n.children]

    def assign(self, X, rows):
        leaves = self.leaves()
        C = np.stack([n.centroid for n in leaves])
        a = (X[rows] @ C.T).argmax(axis=1)
        for r, j in zip(rows, a):
            leaf = leaves[j]
            leaf.members.append(r)
            self.leaf_of[r] = leaf.id

    def split(self, leaf, X):
        """Open two children under `leaf`, frozen at split time."""
        m = np.asarray(leaf.members, dtype=np.int64)
        C2, a2 = kmeans(X[m], 2, iters=8, seed=13)
        kids = []
        for j in range(2):
            kid = Node(len(self.nodes), leaf.id, C2[j])
            kid.members = m[a2 == j].tolist()
            self.nodes.append(kid)
            kids.append(kid)
        leaf.children = kids
        leaf.members = []
        for kid in kids:
            for r in kid.members:
                self.leaf_of[r] = kid.id
        return kids

    def prefix_of(self, row):
        """Depth-1 address: the root ancestor. Must never change."""
        n = self.nodes[self.leaf_of[row]]
        while n.parent != -1:
            n = self.nodes[n.parent]
        return n.id


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    print(f"  {N} vectors\n", flush=True)

    # seed partition learned on the first 40%, then frozen
    seed_n = int(N * 0.4)
    C0, _ = kmeans(X[:seed_n], K, seed=7)
    box = Container(C0)
    box.assign(X, list(range(seed_n)))
    root_before = {r: box.prefix_of(r) for r in range(seed_n)}

    rng = np.random.default_rng(3)
    qidx = rng.choice(N, 200, replace=False).tolist()

    def score(rows_live):
        """Probe by ROOT centroid, then search that root's whole subtree.

        Probing by leaf was wrong: after a split, three leaf probes cover fewer
        vectors than three root probes did, so recall falls for a reason that has
        nothing to do with promotion. A chain resolves to a NODE and searches
        under it, so the root is the right probe target.
        """
        if not rows_live:
            return 0.0, 0.0
        roots = [n for n in box.nodes if n.parent == -1]
        C = np.stack([n.centroid for n in roots])
        live = np.zeros(N, dtype=bool)
        live[rows_live] = True

        def under(n, acc):
            acc.extend(r for r in n.members if live[r])
            for k in n.children:
                under(k, acc)
            return acc

        sub = [under(r, []) for r in roots]
        liveidx = np.flatnonzero(live)
        L = X[liveidx]
        hit = total = scanned = nq = 0
        for qi in qidx:
            if not live[qi]:
                continue
            nq += 1
            # ground truth over the rows that exist right now, not over the future
            tl = liveidx[np.argsort(-(L @ X[qi]))[:TOPK]]
            near = np.argsort(-(C @ X[qi]))[:PROBES]
            cand = []
            for j in near:
                cand.extend(sub[j])
            if not cand:
                continue
            cand = np.asarray(cand, dtype=np.int64)
            scanned += len(cand)
            sims = X[cand] @ X[qi]
            top = cand[np.argsort(-sims)[:TOPK]]
            hit += len(set(top.tolist()) & set(tl.tolist()))
            total += len(tl)
        return hit / max(total, 1), scanned / max(nq * N, 1)

    print(f"seed {seed_n} rows, {len(box.leaves())} buckets, capacity {cap}\n",
          flush=True)
    print(f"{'rows':>7} {'leaves':>7} {'splits':>7} {'recall':>7} {'scan':>6} "
          f"{'rewritten':>10} {'prefix':>8}", flush=True)
    print("-" * 60, flush=True)

    splits = 0
    rewritten = 0
    step = max(1, N // 10)
    for start in range(seed_n, N, step):
        rows = list(range(start, min(start + step, N)))
        box.assign(X, rows)

        # promote every leaf that went over capacity
        for leaf in list(box.leaves()):
            if len(leaf.members) <= cap:
                continue
            before = {r: box.leaf_of[r] for r in leaf.members}
            box.split(leaf, X)
            rewritten += sum(1 for r in before if box.leaf_of[r] != before[r])
            splits += 1

        live = list(range(min(start + step, N)))
        rec, frac = score(live)
        broken = sum(1 for r in root_before if box.prefix_of(r) != root_before[r])
        print(f"{live[-1]+1:>7} {len(box.leaves()):>7} {splits:>7} {rec:>7.4f} "
              f"{frac:>5.1%} {rewritten:>10} {broken:>8}", flush=True)

    print("\n'prefix' is the depth-1 address: 0 means no root ever moved.")
    print("'rewritten' counts leaves that got a LONGER address (children), which")
    print("is promotion, not relocation - their prefix is unchanged.")


if __name__ == "__main__":
    main()
