"""Hybrid Router: LC seed (deterministic identity) + IVF (recall engine).
Combines: deterministic address from LC, recall-per-cost from IVF.
Ingest:  LC seed (primary key, never changes) + IVF label (derived cache).
Query:   lc | ivf | hybrid (union of both candidate sets).
Rebuild: IVF recomputed; LC identity preserved -> data never moves.
"""
import os, sys, numpy as np, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _lc_seed(vec):
    q = [int(x * (1 << 20)) for x in vec]
    m = sum(q) // len(q)
    qc = [x - m for x in q]
    per = len(qc) // 3
    sums = [sum(qc[g * per:(g + 1) * per]) for g in range(3)]
    trit = 0
    for s in sums:
        d = 0 if s < 0 else (2 if s > 0 else 1)
        trit = trit * 3 + d
    trit %= 27
    groups = 5
    per2 = len(qc) // groups
    bits = 0
    for g in range(groups):
        s = sum(qc[g * per2:(g + 1) * per2])
        if s >= 0:
            bits |= (1 << g)
    return (trit << 5) | (bits % 32)


class HybridRouter:
    def __init__(self, n_ivf=256, n_ivf_probe=8, seed=0):
        self.n_ivf = n_ivf
        self.n_probe = n_ivf_probe
        self.rng_seed = seed
        self.X = None          # corpus (N, D)
        self.lc_seeds = None   # (N,) deterministic identity
        self.ivf_labels = None # (N,) derived cache
        self.ivf_centers = None
        self._fitted = False

    def fit(self, X):
        X = np.asarray(X, dtype=np.float64)
        self.X = X
        N, D = X.shape
        self.lc_seeds = np.array([_lc_seed(r) for r in X], dtype=np.int64)
        # IVF (k-means) as derived cache
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=self.n_ivf, random_state=self.rng_seed, n_init=1)
        self.ivf_labels = km.fit_predict(X).astype(np.int64)
        self.ivf_centers = km.cluster_centers_.astype(np.float64)
        self._fitted = True
        return self

    def _lc_bucket(self, q):
        qs = _lc_seed(q)
        return np.where(self.lc_seeds == qs)[0]

    def _ivf_bucket(self, q):
        d = np.sum((self.ivf_centers - q) ** 2, axis=1)
        tabs = np.argsort(d)[:self.n_probe]
        return np.where(np.isin(self.ivf_labels, tabs))[0]

    def query(self, q, mode="hybrid"):
        q = np.asarray(q, dtype=np.float64)
        if mode == "lc":
            cands = self._lc_bucket(q)
        elif mode == "ivf":
            cands = self._ivf_bucket(q)
        elif mode == "hybrid":
            s = set(int(x) for x in self._lc_bucket(q))
            s.update(int(x) for x in self._ivf_bucket(q))
            cands = np.array(list(s), dtype=np.int64)
        else:
            raise ValueError(mode)
        if len(cands) == 0:
            return cands, np.array([], dtype=np.int64)
        d2 = np.sum((self.X[cands] - q) ** 2, axis=1)
        order = np.argsort(d2)
        return cands, cands[order]

    def topk(self, q, k=10, mode="hybrid"):
        cands, ordered = self.query(q, mode)
        return ordered[:k]

    def rebuild_ivf(self, new_seed=None):
        """Recompute IVF index. LC identity untouched -> data never moves."""
        if new_seed is not None:
            self.rng_seed = new_seed
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=self.n_ivf, random_state=self.rng_seed, n_init=1)
        self.ivf_labels = km.fit_predict(self.X).astype(np.int64)
        self.ivf_centers = km.cluster_centers_.astype(np.float64)
        return self

    def lc_identity_stable(self, other_router):
        """Check LC seeds identical across two routers (determinism proof)."""
        return np.array_equal(self.lc_seeds, other_router.lc_seeds)

    def stats(self):
        return {
            "N": len(self.X) if self._fitted else 0,
            "D": self.X.shape[1] if self._fitted else 0,
            "lc_unique": len(np.unique(self.lc_seeds)) if self._fitted else 0,
            "ivf_clusters": self.n_ivf,
            "ivf_probe": self.n_probe,
        }


def _recall_at_k(router, Q, k=10, mode="hybrid"):
    N = len(router.X)
    recs = []; cands = []
    for qi in range(len(Q)):
        q = Q[qi]
        gd = np.sum((router.X - q) ** 2, axis=1)
        gt = set(np.argsort(gd)[:k])
        tk = router.topk(q, k=k, mode=mode)
        cands.append(len(router.query(q, mode)[0]))
        recs.append(len(set(tk) & gt) / k)
    return float(np.mean(recs)), float(np.mean(cands) / N * 100)


if __name__ == "__main__":
    HERE = os.path.dirname(os.path.abspath(__file__))
    X = np.load(os.path.join(HERE, 'data_other', 'sift', 'corpus_embeddings.npy'))[:5000].astype(np.float64)
    Q = X[:30]
    r = HybridRouter(n_ivf=256, n_ivf_probe=8).fit(X)
    print("stats:", r.stats())
    for mode in ("lc", "ivf", "hybrid"):
        rec, cp = _recall_at_k(r, Q, k=10, mode=mode)
        print(f"{mode:8s}: recall@10={rec:.3f}  cand%={cp:5.1f}%")
    # determinism: rebuild IVF with different seed, LC stays
    r2 = HybridRouter(n_ivf=256, n_ivf_probe=8, seed=99).fit(X)
    print("LC identity stable across rebuild:", r.lc_identity_stable(r2))
    # timing
    t0 = time.time()
    for q in Q:
        r.topk(q, k=10, mode="hybrid")
    ms = (time.time() - t0) / len(Q) * 1000
    print(f"hybrid query: {ms:.3f} ms/query")
    print("HYBRID ROUTER OK")
