"""Multi-dim Hybrid Router + Rewind Buffer integration.
Combines: multi-dim LC seed (dim-tagged, zero cross-dim collision)
          + per-dim IVF (recall engine)
          + shared face grid (rewind buffer, seen() O(1))
          + letter-embedding (cross-modal bridge).
"""
import os, sys, numpy as np, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lc_hybrid_router import _lc_seed
from lc_rewind_buffer import build_face_mapper
from lc_letter_embed import letter_embedding

DIM_TAG_SHIFT = 10  # base(0..863) | (dim_id << 10)


def _tagged_seed(vec, dim_id):
    base = _lc_seed(vec)
    return base | (int(dim_id) << DIM_TAG_SHIFT)


class MultiDimHybridRouter:
    def __init__(self, n_ivf=256, n_probe=8, grid=4, seed=0):
        self.n_ivf = n_ivf
        self.n_probe = n_probe
        self.grid = grid
        self.rng_seed = seed
        # per-dim storage
        self.corpora = {}        # dim_id -> X (N, D)
        self.lc_seeds = {}       # dim_id -> (N,) tagged seeds
        self.ivf_labels = {}     # dim_id -> (N,)
        self.ivf_centers = {}    # dim_id -> (K, D)
        # shared face grid (built on concatenated corpus)
        self._face_mapper = None
        self._frame_map = None
        self._neighbor_map = None
        self._letter_map = None
        self._mn = None
        self._mx = None
        self._basis = None
        self.rewind = None       # RewindBuffer instance
        self._fitted = False

    def fit(self, corpora):
        """corpora: dict dim_id -> np.ndarray (N, D). Dims may differ."""
        from sklearn.cluster import KMeans
        from lc_rewind_buffer import RewindBuffer
        self.corpora = {int(d): np.asarray(X, dtype=np.float64) for d, X in corpora.items()}
        # per-dim LC seeds + IVF
        for dim_id, X in self.corpora.items():
            self.lc_seeds[dim_id] = np.array([_tagged_seed(r, dim_id) for r in X], dtype=np.int64)
            km = KMeans(n_clusters=self.n_ivf, random_state=self.rng_seed, n_init=1)
            self.ivf_labels[dim_id] = km.fit_predict(X).astype(np.int64)
            self.ivf_centers[dim_id] = km.cluster_centers_.astype(np.float64)
        # shared face grid on concatenated (pad to common? use per-dim mapper lazily)
        # Build one mapper per dim (PCA on that dim's corpus)
        self._face_mappers = {}
        self._face_meta = {}
        all_faces = set()
        for dim_id, X in self.corpora.items():
            mapper, mn, mx, basis, letter_map, frame_map, neighbor_map, uniq = \
                _build_face_mapper(X, self.grid, f"md-{dim_id}")
            self._face_mappers[dim_id] = (mapper, mn, mx, basis)
            self._face_meta[dim_id] = (letter_map, frame_map, neighbor_map)
            all_faces.update(uniq)
        self.rewind = RewindBuffer()
        self._fitted = True
        return self

    def _face_of(self, vec, dim_id):
        mapper, mn, mx, basis = self._face_mappers[dim_id]
        coord = (vec - self.corpora[dim_id].mean(0)) @ basis
        gp = tuple(((coord - mn) / (mx - mn + 1e-9) * self.grid).astype(int).clip(0, self.grid - 1))
        return gp

    def _lc_bucket(self, q, dim_id):
        qs = _tagged_seed(q, dim_id)
        return np.where(self.lc_seeds[dim_id] == qs)[0]

    def _ivf_bucket(self, q, dim_id):
        centers = self.ivf_centers[dim_id]
        d = np.sum((centers - q) ** 2, axis=1)
        tabs = np.argsort(d)[:self.n_probe]
        return np.where(np.isin(self.ivf_labels[dim_id], tabs))[0]

    def query(self, q, dim_id, mode="hybrid"):
        dim_id = int(dim_id)
        q = np.asarray(q, dtype=np.float64)
        if mode == "lc":
            cands = self._lc_bucket(q, dim_id)
        elif mode == "ivf":
            cands = self._ivf_bucket(q, dim_id)
        elif mode == "hybrid":
            s = set(int(x) for x in self._lc_bucket(q, dim_id))
            s.update(int(x) for x in self._ivf_bucket(q, dim_id))
            cands = np.array(list(s), dtype=np.int64)
        else:
            raise ValueError(mode)
        # rewind: have we seen this face before?
        face = self._face_of(q, dim_id)
        seen_before = self.rewind.seen(face)
        # record visit
        self.rewind.store(face, None)
        if len(cands) == 0:
            return cands, np.array([], dtype=np.int64), face, seen_before
        d2 = np.sum((self.corpora[dim_id][cands] - q) ** 2, axis=1)
        order = np.argsort(d2)
        return cands, cands[order], face, seen_before

    def topk(self, q, dim_id, k=10, mode="hybrid"):
        cands, ordered, face, seen = self.query(q, dim_id, mode)
        return ordered[:k], face, seen

    def letter_embed(self, q, dim_id):
        dim_id = int(dim_id)
        letter_map, frame_map, neighbor_map = self._face_meta[dim_id]
        face = self._face_of(q, dim_id)
        return face, letter_embedding(face, frame_map, neighbor_map, letter_map)

    def stats(self):
        return {
            "dims": list(self.corpora.keys()),
            "per_dim_N": {d: len(X) for d, X in self.corpora.items()},
            "per_dim_lc_unique": {d: len(np.unique(self.lc_seeds[d])) for d in self.corpora},
            "rewind": self.rewind.stats() if self.rewind else None,
        }


def _build_face_mapper(X, G, seed):
    Xc = X - X.mean(0)
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    basis = Vt[:3].T
    P3 = Xc @ Vt[:3].T
    mn = P3.min(0); mx = P3.max(0)
    positions = []
    for p in P3:
        gp = tuple(((p - mn) / (mx - mn + 1e-9) * G).astype(int).clip(0, G - 1))
        positions.append(gp)
    uniq = sorted(set(positions))
    import lettercube_py as lc
    sim = lc.LetterCube(seed, deterministic=True)
    sim.build(uniq)
    letter_map = {}; frame_map = {}; neighbor_map = {}
    for p in uniq:
        cube = sim.cubes[p]
        letter_map[p] = cube['inner'][0]
        frame_map[p] = cube['row_vals'][0][1]
        nbrs = []
        for ax in range(3):
            for dd in (-1, 1):
                n = list(p); n[ax] += dd; n = tuple(n)
                nbrs.append(1.0 if n in uniq else 0.0)
        neighbor_map[p] = nbrs

    def mapper(vec):
        coord = (vec - X.mean(0)) @ basis
        gp = tuple(((coord - mn) / (mx - mn + 1e-9) * G).astype(int).clip(0, G - 1))
        return gp

    return mapper, mn, mx, basis, letter_map, frame_map, neighbor_map, uniq


if __name__ == "__main__":
    HERE = os.path.dirname(os.path.abspath(__file__))
    # two dims: scifact (384D) + sift (128D)
    X384 = np.load(os.path.join(HERE, 'data_other', 'scifact', 'corpus_embeddings.npy'))[:3000].astype(np.float64)
    X128 = np.load(os.path.join(HERE, 'data_other', 'sift', 'corpus_embeddings.npy'))[:3000].astype(np.float64)
    router = MultiDimHybridRouter(n_ivf=64, n_probe=4).fit({0: X384, 1: X128})
    print("stats:", router.stats())
    # cross-dim collision check on tagged seeds
    s0 = router.lc_seeds[0]; s1 = router.lc_seeds[1]
    coll = len(set(s0.tolist()) & set(s1.tolist()))
    print(f"cross-dim seed collisions: {coll} (expect 0)")
    # query each dim
    for dim_id, X in ((0, X384), (1, X128)):
        Q = X[:20]
        recs = {"lc": [], "ivf": [], "hybrid": []}
        candpct = {"lc": [], "ivf": [], "hybrid": []}
        seen_counts = {"lc": 0, "ivf": 0, "hybrid": 0}
        for qi in range(20):
            q = Q[qi]
            gd = np.sum((X - q) ** 2, axis=1)
            gt = set(np.argsort(gd)[:10])
            for mode in ("lc", "ivf", "hybrid"):
                tk, face, seen = router.topk(q, dim_id, k=10, mode=mode)
                cands, _, _, _ = router.query(q, dim_id, mode)
                candpct[mode].append(len(cands) / len(X) * 100)
                recs[mode].append(len(set(tk) & gt) / 10.0)
                if seen:
                    seen_counts[mode] += 1
        print(f"\ndim {dim_id} ({X.shape[1]}D, N={len(X)}):")
        for mode in ("lc", "ivf", "hybrid"):
            print(f"  {mode:8s}: recall@10={np.mean(recs[mode]):.3f}  cand%={np.mean(candpct[mode]):5.1f}%  seen={seen_counts[mode]}/20")
    # letter embed demo
    face, emb = router.letter_embed(X384[0], 0)
    print("\nletter_embed face:", face, "dim:", emb.shape)
    print("MULTIDIM HYBRID + REWIND OK")
