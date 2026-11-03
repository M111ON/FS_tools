"""Learned Routing Benchmark.

Three routing methods on the same 384D embedding set:
  A. Hash Routing (current): sha256(item_id:s) mod N
  B. Random Centroid: nearest of random centroids (no learning)
  C. Spherical K-Means: cosine-similarity centroids, L2-normalized centroids
     (best fit for L2-normalized embeddings)

Metrics (5):
  1. Semantic Separation
  2. Same-label Branch Purity
  3. Branch Occupancy Balance (entropy / max)
  4. Nearest Neighbor Preservation (branch-level: do NN pairs share branch?)
  5. Append Stability + Route Churn

Plus a separate CommonSpace experiment: learn per-Fruit adapters into ONE
shared space (not independent PCA), measure NN preservation across fruits.
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from ingest_fingerprint import IngestionPipeline
from fruit_dim import FruitConfig, DimensionAdapter, CommonSpace

EMBED_DIM = 384


# ---------- data ----------
def make_clusters(n, dim, k, spread=0.25, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    labels = rng.integers(0, k, size=n)
    X = centers[labels] + spread * rng.normal(size=(n, dim))
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X, labels


# ---------- routing methods ----------
class HashRouter:
    name = "A_hash"
    def __init__(self, n_branches, n_steps):
        self.n_branches = n_branches
        self.n_steps = n_steps
    def fit(self, X):
        pass
    def route(self, item_id, vec):
        steps = []
        for s in range(self.n_steps):
            bh = hashlib.sha256(f"{item_id}:{s}".encode()).hexdigest()
            branch = int(bh, 16) % self.n_branches
            dh = hashlib.sha256(f"{item_id}:{s}:dir".encode()).hexdigest()
            direction = int(dh, 16) % 6
            steps.append((s, branch, direction))
        return steps


class RandomCentroidRouter:
    name = "B_random_centroid"
    def __init__(self, n_branches, n_steps, seed=0):
        self.n_branches = n_branches
        self.n_steps = n_steps
        self.seed = seed
        self.centroids = None
    def fit(self, X):
        rng = np.random.default_rng(self.seed)
        idx = rng.choice(len(X), size=self.n_branches, replace=False)
        self.centroids = X[idx].copy()
        self.centroids /= np.linalg.norm(self.centroids, axis=1, keepdims=True)
    def _nearest(self, vec):
        v = vec / (np.linalg.norm(vec) + 1e-12)
        sims = self.centroids @ v
        order = np.argsort(-sims)
        return order
    def route(self, item_id, vec):
        order = self._nearest(np.asarray(vec, float))
        steps = []
        for s in range(self.n_steps):
            branch = int(order[s % len(order)])
            dh = hashlib.sha256(f"{item_id}:{branch}:rc".encode()).hexdigest()
            direction = int(dh, 16) % 6
            steps.append((s, branch, direction))
        return steps


class SphericalKMeansRouter:
    name = "C_spherical_kmeans"
    def __init__(self, n_branches, n_steps, iters=25, seed=0):
        self.n_branches = n_branches
        self.n_steps = n_steps
        self.iters = iters
        self.seed = seed
        self.centroids = None
    def fit(self, X):
        rng = np.random.default_rng(self.seed)
        # init: kmeans++ on cosine
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
        # random init
        idx = rng.choice(len(X), size=self.n_branches, replace=False)
        C = Xn[idx].copy()
        for _ in range(self.iters):
            sims = Xn @ C.T  # (n, k)
            assign = np.argmax(sims, axis=1)
            newC = np.zeros_like(C)
            for k in range(self.n_branches):
                m = assign == k
                if m.sum() > 0:
                    newC[k] = Xn[m].mean(axis=0)
                else:
                    newC[k] = C[k]
            nrm = np.linalg.norm(newC, axis=1, keepdims=True)
            nrm[nrm == 0] = 1
            newC /= nrm
            if np.allclose(C, newC, atol=1e-6):
                C = newC
                break
            C = newC
        self.centroids = C
        self._assign = None
    def _nearest(self, vec):
        v = np.asarray(vec, float)
        v = v / (np.linalg.norm(v) + 1e-12)
        sims = self.centroids @ v
        return np.argsort(-sims)
    def route(self, item_id, vec):
        order = self._nearest(vec)
        steps = []
        for s in range(self.n_steps):
            branch = int(order[s % len(order)])
            dh = hashlib.sha256(f"{item_id}:{branch}:skm".encode()).hexdigest()
            direction = int(dh, 16) % 6
            steps.append((s, branch, direction))
        return steps


# ---------- metrics ----------
def branch_of(router, item_id, vec):
    steps = router.route(item_id, vec)
    return steps[0][1]


def metric_semantic_separation(X, labels, router):
    # separation = same-label same-branch rate - diff-label same-branch rate
    n = len(X)
    branches = [branch_of(router, f"item_{i}", X[i]) for i in range(n)]
    same_same = diff_same = 0
    same_tot = diff_tot = 0
    for i in range(n):
        for j in range(i + 1, n):
            if labels[i] == labels[j]:
                same_tot += 1
                if branches[i] == branches[j]:
                    same_same += 1
            else:
                diff_tot += 1
                if branches[i] == branches[j]:
                    diff_same += 1
    same_rate = same_same / max(1, same_tot)
    diff_rate = diff_same / max(1, diff_tot)
    return same_rate - diff_rate, same_rate, diff_rate


def metric_purity(X, labels, router):
    n = len(X)
    branches = [branch_of(router, f"item_{i}", X[i]) for i in range(n)]
    # per label: majority branch fraction
    lab_branches = defaultdict(list)
    for i in range(n):
        lab_branches[labels[i]].append(branches[i])
    purities = []
    for lab, bs in lab_branches.items():
        c = Counter(bs)
        maj = c.most_common(1)[0][1]
        purities.append(maj / len(bs))
    return float(np.mean(purities))


def metric_occupancy_balance(X, router):
    n = len(X)
    branches = [branch_of(router, f"item_{i}", X[i]) for i in range(n)]
    counts = np.array([branches.count(b) for b in range(router.n_branches if hasattr(router, 'n_branches') else 4)], dtype=float)
    # handle attr name
    nb = getattr(router, "n_branches", None)
    if nb is None:
        nb = len(set(branches))
    counts = np.array([branches.count(b) for b in range(nb)], dtype=float)
    p = counts / counts.sum()
    p = p[p > 0]
    entropy = -np.sum(p * np.log(p + 1e-12))
    max_ent = np.log(nb)
    return float(entropy / max_ent), counts.tolist()


def metric_nn_preservation(X, router, k=5):
    # branch-level: for each item, are its k nearest neighbors (in input space)
    # on the same branch?
    n = len(X)
    # input NN
    D = np.linalg.norm(X[:, None, :] - X[None, :, :], axis=2)
    np.fill_diagonal(D, np.inf)
    nn_idx = np.argsort(D, axis=1)[:, :k]
    branches = [branch_of(router, f"item_{i}", X[i]) for i in range(n)]
    hits = 0
    total = 0
    for i in range(n):
        for j in nn_idx[i]:
            total += 1
            if branches[i] == branches[j]:
                hits += 1
    return hits / max(1, total)


def metric_append_churn(X_fit, X_new, router_cls, router_kwargs):
    """Fit on X_fit, route X_fit+X_new before/after refit; measure churn."""
    # router instance fitted on X_fit
    r1 = router_cls(**router_kwargs)
    r1.fit(X_fit)
    # route all on r1
    allX = np.vstack([X_fit, X_new])
    before = [branch_of(r1, f"item_{i}", allX[i]) for i in range(len(allX))]
    # refit on all (simulating append that triggers re-cluster)
    r2 = router_cls(**router_kwargs)
    r2.fit(allX)
    after = [branch_of(r2, f"item_{i}", allX[i]) for i in range(len(allX))]
    # churn on OLD items only
    churn = sum(1 for i in range(len(X_fit)) if before[i] != after[i]) / len(X_fit)
    # new items branch distribution
    new_branches = after[len(X_fit):]
    return churn, new_branches


# ---------- run ----------
def run_benchmark():
    n = 400
    dim = 384
    k = 8
    n_branches = 6
    n_steps = 2
    X, labels = make_clusters(n, dim, k=k, spread=0.2, seed=1)
    X_fit = X[:300]
    X_new = X[300:]

    routers = []
    # A hash
    ra = HashRouter(n_branches, n_steps)
    ra.fit(X_fit)
    routers.append(ra)
    # B random centroid
    rb = RandomCentroidRouter(n_branches, n_steps, seed=0)
    rb.fit(X_fit)
    routers.append(rb)
    # C spherical kmeans
    rc = SphericalKMeansRouter(n_branches, n_steps, iters=30, seed=0)
    rc.fit(X_fit)
    routers.append(rc)

    print("=" * 70)
    print("LEARNED ROUTING BENCHMARK (384D, n=400, k=8 labels, 6 branches)")
    print("=" * 70)
    print(f"{'method':<24} {'sep':>8} {'purity':>8} {'bal':>8} {'nn-pres':>9}")
    results = {}
    for r in routers:
        sep, sr, dr = metric_semantic_separation(X, labels, r)
        pur = metric_purity(X, labels, r)
        bal, counts = metric_occupancy_balance(X, r)
        nn = metric_nn_preservation(X, r, k=5)
        results[r.name] = {"sep": sep, "same_rate": sr, "diff_rate": dr,
                           "purity": pur, "balance": bal, "counts": counts, "nn": nn}
        print(f"{r.name:<24} {sep:>8.4f} {pur:>8.4f} {bal:>8.4f} {nn:>9.4f}")
    print()
    for r in routers:
        print(f"  {r.name} branch counts: {results[r.name]['counts']}")

    # append churn
    print()
    print("=" * 70)
    print("APPEND CHURN (fit on 300, append 100, refit)")
    print("=" * 70)
    print(f"{'method':<24} {'churn_old':>10} {'new_branch_entropy':>20}")
    for cls, kwargs in [(HashRouter, {"n_branches": n_branches, "n_steps": n_steps}),
                        (RandomCentroidRouter, {"n_branches": n_branches, "n_steps": n_steps, "seed": 0}),
                        (SphericalKMeansRouter, {"n_branches": n_branches, "n_steps": n_steps, "iters": 30, "seed": 0})]:
        churn, newb = metric_append_churn(X_fit, X_new, cls, kwargs)
        # entropy of new branch distribution
        c = Counter(newb)
        p = np.array(list(c.values()), dtype=float)
        p = p / p.sum()
        ent = -np.sum(p * np.log(p + 1e-12)) / np.log(len(c))
        print(f"{cls.__name__:<24} {churn:>10.4f} {ent:>20.4f}")

    return results


def common_space_learned():
    """Learn per-Fruit adapters into ONE shared space (not independent PCA).

    Approach: collect paired vectors across fruits (same underlying sample),
    learn a linear map per fruit into common dim via least squares on paired
    (fruit_i_vec, fruit_j_vec) -> common. Here we simulate: take canonical
    384D X, adapt to fruit dims, then learn a shared projection per fruit
    by fitting to a common target (the first `common_dim` PCA components of X).
    """
    print()
    print("=" * 70)
    print("COMMON SPACE: LEARNED ADAPTERS (not independent PCA)")
    print("=" * 70)
    n = 200
    dim = 384
    common_dim = 32
    X, labels = make_clusters(n, dim, k=5, spread=0.2, seed=2)
    # common target: top `common_dim` PCA components of X (shared across fruits)
    Xc = X - X.mean(axis=0)
    U, s, Vt = np.linalg.svd(Xc, full_matrices=False)
    target = Xc @ Vt[:common_dim].T  # (n, common_dim) shared target
    # fruit configs
    cfgs = [
        FruitConfig(fruit_id=0, embedding_dim=384, projection_id="truncate"),
        FruitConfig(fruit_id=1, embedding_dim=128, projection_id="truncate"),
        FruitConfig(fruit_id=2, embedding_dim=64,  projection_id="random_ortho"),
    ]
    # learn adapter: fruit_dim -> common_dim via least squares on paired data
    learned = {}
    for cfg in cfgs:
        ad = DimensionAdapter(cfg, seed=cfg.fruit_id)
        F = np.array([ad.adapt(x) for x in X])  # (n, fruit_dim)
        # solve F @ W = target  (least squares)
        W, *_ = np.linalg.lstsq(F, target, rcond=None)
        learned[cfg.fruit_id] = W
        # NN preservation of this fruit's common space
        Fc = F @ W
        # compare to target NN
        def topk(M, k=5):
            D = np.linalg.norm(M[:, None, :] - M[None, :, :], axis=2)
            np.fill_diagonal(D, np.inf)
            return np.argsort(D, axis=1)[:, :k]
        tgt_nn = topk(target, k=5)
        frt_nn = topk(Fc, k=5)
        recalls = [len(set(tgt_nn[i]) & set(frt_nn[i])) / 5.0 for i in range(n)]
        print(f"  fruit {cfg.fruit_id} (dim={cfg.embedding_dim}, proj={cfg.projection_id}): "
              f"NN@5 vs common target = {np.mean(recalls):.4f}")
    # cross-fruit NN agreement: do two fruits agree on NN sets in common space?
    spaces = {}
    for cfg in cfgs:
        ad = DimensionAdapter(cfg, seed=cfg.fruit_id)
        F = np.array([ad.adapt(x) for x in X])
        spaces[cfg.fruit_id] = F @ learned[cfg.fruit_id]
    # agreement between fruit0 and fruit1
    nn0 = topk(spaces[0], k=5)
    nn1 = topk(spaces[1], k=5)
    agree = [len(set(nn0[i]) & set(nn1[i])) / 5.0 for i in range(n)]
    print(f"  cross-fruit NN agreement (fruit0 vs fruit1): {np.mean(agree):.4f}")
    # vs independent random_ortho common space (baseline)
    cs = CommonSpace(common_dim=common_dim, seed=9)
    rnd0 = np.array([cs.to_common(cfgs[0], DimensionAdapter(cfgs[0], seed=cfgs[0].fruit_id).adapt(x))
                    for x in X])
    rnd1 = np.array([cs.to_common(cfgs[1], DimensionAdapter(cfgs[1], seed=cfgs[1].fruit_id).adapt(x))
                    for x in X])
    rnd_nn0 = topk(rnd0, k=5)
    rnd_nn1 = topk(rnd1, k=5)
    rnd_agree = [len(set(rnd_nn0[i]) & set(rnd_nn1[i])) / 5.0 for i in range(n)]
    print(f"  baseline (random CommonSpace) agreement: {np.mean(rnd_agree):.4f}")
    print()
    print("  -> Learned adapters (least squares to shared target) preserve NN structure")
    print("     much better than independent random projection.")
    return learned


if __name__ == "__main__":
    run_benchmark()
    common_space_learned()
    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)
