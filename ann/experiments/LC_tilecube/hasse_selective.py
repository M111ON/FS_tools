"""Prototype: selective Hasse expansion strategies.

Tests expansion strategies between K_ring=0 and K_ring=1 on FD(3) Hasse
diagram (20 nodes, 32 edges). Reuses fd3_hasse and hasse_jump_scan.

Strategies:
  - same_rank: expand only to Hasse nodes at the same rank as start
  - adjacent_rank: expand only to nodes at rank-1 / rank / rank+1
  - top_k_edges: expand to K nearest nodes in Hasse coordinate space (BFS)
  - rank_weighted: expand (Hasse edges) only from middle ranks (3-5)

Measures recall@10, scan%, nodes visited at 5% and 10% scan budgets.
"""
import sys, os
from collections import defaultdict, deque
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fd3_hasse as fd
import hasse_jump_scan as hjs

# Graph distance (shortest path) on Hasse diagram
DIST = [[0] * fd.n for _ in range(fd.n)]
for _s in range(fd.n):
    _dist = [-1] * fd.n
    _dist[_s] = 0
    _q = deque([_s])
    while _q:
        _u = _q.popleft()
        for _nb in fd.hasse_adj[_u]:
            if _dist[_nb] < 0:
                _dist[_nb] = _dist[_u] + 1
                _q.append(_nb)
    for _t in range(fd.n):
        DIST[_s][_t] = _dist[_t] if _dist[_t] >= 0 else 999


def graph_dist(a, b):
    return DIST[a][b]

K_TOP = 3
MIDDLE_RANKS = {3, 4, 5}
N = 500
D = 64


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def setup():
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, D))
    Q = rng.normal(size=(20, D))
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    face_centers = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    face_centers = face_centers / np.linalg.norm(face_centers, axis=1, keepdims=True)

    X_face = [int(np.argmax(face_centers @ v)) for v in Xp]
    fd_map = defaultdict(list)
    for i, f in enumerate(X_face):
        fd_map[fd.face_to_fd[f]].append(i)

    return X, Q, Xn, Qn, Xp, Qp, face_centers, fd_map


def gen_neighbors(node, strategy, visited, start_rank, start_fd):
    r = fd.rank[node]
    if strategy == "none":
        return []
    if strategy == "full_ring":
        if node == start_fd:
            return [nb for nb in fd.hasse_adj[node] if nb not in visited]
        return []
    if strategy == "same_rank":
        cands = [nb for nb in range(fd.n)
                 if nb not in visited and fd.rank[nb] == start_rank]
        cands.sort(key=lambda nb: graph_dist(node, nb))
        return cands
    if strategy == "adjacent_rank":
        allowed = {start_rank - 1, start_rank, start_rank + 1}
        cands = [nb for nb in range(fd.n)
                 if nb not in visited and fd.rank[nb] in allowed]
        cands.sort(key=lambda nb: graph_dist(node, nb))
        return cands
    if strategy == "top_k_edges":
        others = [nb for nb in range(fd.n) if nb not in visited and nb != node]
        others.sort(key=lambda nb: graph_dist(node, nb))
        return others[:K_TOP]
    if strategy == "rank_weighted":
        if r in MIDDLE_RANKS:
            return [nb for nb in fd.hasse_adj[node] if nb not in visited]
        return []
    raise ValueError(strategy)


def run_strategy(q_proj, budget, strategy, face_centers, fd_map):
    qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
    sims = face_centers @ qn
    start_fd = fd.face_to_fd[int(np.argmax(sims))]
    max_cands = int(budget * N)

    visited = {start_fd}
    cands = set(fd_map.get(start_fd, []))
    # cap initial collection
    if len(cands) > max_cands:
        # truncate deterministically
        cands = set(list(cands)[:max_cands])
    queue = deque([start_fd])

    while queue and len(cands) < max_cands:
        node = queue.popleft()
        for nb in gen_neighbors(node, strategy, visited, fd.rank[start_fd], start_fd):
            if nb in visited:
                continue
            visited.add(nb)
            for idx in fd_map.get(nb, []):
                cands.add(idx)
                if len(cands) >= max_cands:
                    break
            queue.append(nb)
            if len(cands) >= max_cands:
                break
    return cands, visited


def evaluate(strategy, budget, Qp, Qn, Xn, face_centers, fd_map):
    recs, scans, nodes = [], [], []
    for qi in range(len(Qp)):
        gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        gtset = set(gt)
        cands, visited = run_strategy(Qp[qi], budget, strategy, face_centers, fd_map)
        ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        recs.append(len(gtset & set(ranked)) / 10.0)
        scans.append(len(cands) / N * 100)
        nodes.append(len(visited))
    return float(np.mean(recs)), float(np.mean(scans)), float(np.mean(nodes))


def pca_baseline(Xn, Qn, Xp, Qp):
    # PCA-to-cubic-grid as in fd3_hasse
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    def pca_cands(q):
        b = pca_bucket(q)
        cands = set(pca_map.get(b, []))
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
        return cands

    recs, scans = [], []
    for qi in range(len(Qp)):
        gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        gtset = set(gt)
        cands = pca_cands(Qp[qi])
        ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
        recs.append(len(gtset & set(ranked)) / 10.0)
        scans.append(len(cands) / N * 100)
    return float(np.mean(recs)), float(np.mean(scans))


def main():
    X, Q, Xn, Qn, Xp, Qp, face_centers, fd_map = setup()

    print("=" * 78)
    print("Selective Hasse expansion strategies on FD(3) (20 nodes, 32 edges)")
    print("=" * 78)

    # Baselines using fd3_hasse fd_candidates (includes icosphere face adjacency for K>=1)
    fc = np.array([fd.verts2[f].mean(axis=0) for f in fd.faces2])
    fc = fc / np.linalg.norm(fc, axis=1, keepdims=True)
    X_face_l2 = [int(np.argmax(fc @ v)) for v in Xp]
    face_map = defaultdict(list)
    for i, f in enumerate(X_face_l2):
        face_map[f].append(i)

    def fd_candidates(q_proj, K_ring):
        qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
        sims = fc @ qn
        f0 = int(np.argmax(sims))
        fd0 = fd.face_to_fd[f0]
        fd_nodes = {fd0}
        frontier = {fd0}
        for _ in range(K_ring):
            nxt = set()
            for node in frontier:
                for nb in fd.hasse_adj[node]:
                    if nb not in fd_nodes:
                        fd_nodes.add(nb)
                        nxt.add(nb)
            frontier = nxt
        faces_set = set()
        for fde in fd_nodes:
            for l2f in fd.l0_to_l2.get(fde, []):
                faces_set.add(l2f)
        if K_ring >= 1:
            expanded = set()
            for f in faces_set:
                expanded.add(f)
                for nb in fd.face_adj[f]:
                    expanded.add(nb)
            faces_set = expanded
        cands = set()
        for f in faces_set:
            for idx in face_map.get(f, []):
                cands.add(idx)
        return cands, fd_nodes

    def baseline_kring(K_ring):
        recs, scans, nodes = [], [], []
        for qi in range(len(Qp)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands, fd_nodes = fd_candidates(Qp[qi], K_ring)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs.append(len(gtset & set(ranked)) / 10.0)
            scans.append(len(cands) / N * 100)
            nodes.append(len(fd_nodes))
        return float(np.mean(recs)), float(np.mean(scans)), float(np.mean(nodes))

    r0, s0, nv0 = baseline_kring(0)
    r1, s1, nv1 = baseline_kring(1)
    rpca, spca = pca_baseline(Xn, Qn, Xp, Qp)

    rows = []
    rows.append(("K_ring=0 (none)", "natural", r0, s0, nv0))
    rows.append(("Full 1-ring", "natural", r1, s1, nv1))
    rows.append(("PCA-grid", "natural", rpca, spca, float("nan")))

    strategies = ["same_rank", "adjacent_rank", "top_k_edges", "rank_weighted"]
    for strat in strategies:
        for budget in [0.05, 0.10]:
            r, s, nv = evaluate(strat, budget, Qp, Qn, Xn, face_centers, fd_map)
            rows.append((strat, f"{budget:.0%}", r, s, nv))

    # Print table
    print(f"{'Strategy':<22} {'Budget':<9} {'Recall@10':>10} {'Scan%':>8} {'Nodes':>8}")
    print("-" * 62)
    for name, bud, r, s, nv in rows:
        nv_str = f"{nv:.1f}" if not (isinstance(nv, float) and np.isnan(nv)) else "-"
        print(f"{name:<22} {bud:<9} {r:>10.3f} {s:>8.1f} {nv_str:>8}")

    print()
    print("Notes:")
    print("  K_TOP (top_k_edges) =", K_TOP)
    print("  middle ranks for rank_weighted =", sorted(MIDDLE_RANKS))
    print("  N=500 Gaussian vectors, 20 queries, cosine GT top-10, seed=3")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
