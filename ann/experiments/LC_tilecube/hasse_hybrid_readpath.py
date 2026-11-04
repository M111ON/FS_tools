"""Prototype: Hasse jump scan (coarse) + Read Path cosine rerank (fine) hybrid.

Combines two complementary stages:
  1. COARSE (structural): jump through Hasse diagram nodes to collect ~5% of data
     as candidates. Seed node chosen by nearest Hasse-node centroid in original
     vector space (cheap: 20 centroids, not 500 points).
  2. FINE (semantic): cosine similarity rerank within candidates (Read Path
     search() style) -> top-10.

Compared against:
  - Hasse jump alone (projected seed, 0.130 @ 5%)
  - PCA baseline (0.105 @ 2.7%)
  - Pure cosine brute-force (upper bound, 1.0 @ 100%)

Test setup: N=500 Gaussian (64D), 20 queries, cosine ground-truth top-10.

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict, deque
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fd3_hasse as fd

N = 500
D = 64
NQ = 20
K = 10
BUDGET = 0.05


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def setup():
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, D))
    Q = rng.normal(size=(NQ, D))
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

    node_centroid = {}
    for node in range(fd.n):
        idxs = fd_map.get(node, [])
        if idxs:
            C = Xn[idxs].mean(axis=0)
            node_centroid[node] = C / (np.linalg.norm(C) + 1e-9)
        else:
            node_centroid[node] = np.zeros(D)

    # PCA grid map (for PCA baseline)
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    return X, Q, Xn, Qn, Xp, Qp, face_centers, fd_map, node_centroid, pca_map, pca_bucket


def gt_set(Qn, Xn, qi):
    return set(sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:K])


# ---------- Coarse candidate generators ----------

def hasse_jump_projected(q_proj, fd_map, face_centers, budget):
    """Original hasse_jump_scan: seed by projected 3D nearest face (0.130 @ 5%)."""
    qn = q_proj / (np.linalg.norm(q_proj) + 1e-9)
    sims = face_centers @ qn
    start_fd = fd.face_to_fd[int(np.argmax(sims))]
    maxc = int(budget * N)
    visited = {start_fd}
    cands = set(fd_map.get(start_fd, []))
    queue = deque([start_fd])
    while queue and len(cands) < maxc:
        node = queue.popleft()
        for nb in fd.hasse_adj[node]:
            if nb in visited:
                continue
            visited.add(nb)
            queue.append(nb)
            for idx in fd_map.get(nb, []):
                cands.add(idx)
                if len(cands) >= maxc:
                    break
            if len(cands) >= maxc:
                break
    return set(list(cands)[:maxc])


def hasse_hybrid_centroid(qi, Qn, fd_map, node_centroid, budget):
    """Hybrid coarse: seed by nearest Hasse-node centroid in original space, then jump."""
    qn = Qn[qi]
    nd = max(range(fd.n), key=lambda n: cos(qn, node_centroid[n]))
    maxc = int(budget * N)
    visited = {nd}
    cands = set(fd_map.get(nd, []))
    queue = deque([nd])
    while queue and len(cands) < maxc:
        node = queue.popleft()
        for nb in fd.hasse_adj[node]:
            if nb in visited:
                continue
            visited.add(nb)
            queue.append(nb)
            for idx in fd_map.get(nb, []):
                cands.add(idx)
                if len(cands) >= maxc:
                    break
            if len(cands) >= maxc:
                break
    return set(list(cands)[:maxc])


def pca_candidates(q_proj, pca_map, pca_bucket, budget_unused=None):
    """PCA grid baseline (0.105 @ 2.7%)."""
    b = pca_bucket(q_proj)
    cands = set(pca_map.get(b, []))
    for pos in range(3):
        for alt in range(12):
            nb = list(b)
            nb[pos] = alt
            for idx in pca_map.get(tuple(nb), []):
                cands.add(idx)
    return cands


def brute_force_candidates():
    return set(range(N))


# ---------- Evaluation ----------

def eval_coarse(coarse_fn, Qn, Xn, Xp, Qp, fd_map, face_centers, node_centroid,
                pca_map, pca_bucket, budget, mode):
    recs = []
    scans = []
    for qi in range(NQ):
        if mode == "hasse_jump":
            cands = hasse_jump_projected(Qp[qi], fd_map, face_centers, budget)
        elif mode == "hybrid":
            cands = hasse_hybrid_centroid(qi, Qn, fd_map, node_centroid, budget)
        elif mode == "pca":
            cands = pca_candidates(Qp[qi], pca_map, pca_bucket)
        elif mode == "brute":
            cands = brute_force_candidates()
        else:
            raise ValueError(mode)

        # Fine stage: cosine rerank (Read Path search() style) -> top-K
        ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:K]
        gts = gt_set(Qn, Xn, qi)
        recs.append(len(gts & set(ranked)) / float(K))
        scans.append(len(cands) / float(N) * 100.0)
    return float(np.mean(recs)), float(np.mean(scans))


def main():
    X, Q, Xn, Qn, Xp, Qp, face_centers, fd_map, node_centroid, pca_map, pca_bucket = setup()

    print("=" * 78)
    print("Hasse hybrid read path: coarse Hasse jump + fine cosine rerank")
    print("=" * 78)
    print("Setup: N=%d Gaussian %dD, %d queries, GT = cosine top-%d, seed=3" % (N, D, NQ, K))
    print()

    rows = []

    # 1. Hasse jump alone (projected seed) @ 5%
    r, s = eval_coarse(None, Qn, Xn, Xp, Qp, fd_map, face_centers, node_centroid,
                        pca_map, pca_bucket, BUDGET, "hasse_jump")
    rows.append(("Hasse jump alone (proj seed)", BUDGET, r, s))

    # 2. Hybrid (centroid seed + jump) @ 5%
    r, s = eval_coarse(None, Qn, Xn, Xp, Qp, fd_map, face_centers, node_centroid,
                        pca_map, pca_bucket, BUDGET, "hybrid")
    rows.append(("Hybrid Hasse+cosine (centroid seed)", BUDGET, r, s))

    # 3. PCA baseline @ ~2.7%
    r, s = eval_coarse(None, Qn, Xn, Xp, Qp, fd_map, face_centers, node_centroid,
                        pca_map, pca_bucket, None, "pca")
    rows.append(("PCA grid baseline", None, r, s))

    # 4. Pure cosine brute-force @ 100%
    r, s = eval_coarse(None, Qn, Xn, Xp, Qp, fd_map, face_centers, node_centroid,
                        pca_map, pca_bucket, None, "brute")
    rows.append(("Pure cosine brute-force (upper bound)", None, r, s))

    # Print table
    print(f"{'Method':<42} {'Budget':>8} {'Recall@10':>10} {'Scan%':>8}")
    print("-" * 72)
    for name, bud, r, s in rows:
        bud_str = (f"{bud:.0%}" if bud is not None else "n/a")
        print(f"{name:<42} {bud_str:>8} {r:>10.3f} {s:>8.1f}")

    print()
    print("Notes:")
    print("  - Coarse stage collects ~5% of data via Hasse graph traversal.")
    print("  - Fine stage = cosine similarity rerank (Read Path search() style).")
    print("  - Hybrid seed uses 20 Hasse-node centroids in original %dD space (cheap)." % D)
    print("  - Brute-force scans 100% (upper bound, recall=1.0 by construction).")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())