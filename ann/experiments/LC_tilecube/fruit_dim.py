"""Data Layer: per-Fruit embedding dimension + adapter + common space.

Geometry Layer (Snake/Fruit/Graft/Junction, v1.0 frozen) is UNTOUCHED.
Data Layer sits beside it: each Fruit carries its own embedding_dim and an
adapter that maps a canonical input dim into that Fruit's dim. Cross-Fruit
comparison goes through a CommonSpace (not by comparing raw vectors).

Rules (owner 2026-11-03):
  D1. embedding_dim is a property of the Fruit, not the cluster.
  D2. Adapter is explicit (projection/padding/truncation), never random drop.
  D3. Cross-Fruit comparison requires CommonSpace or Adapter, never raw dot.
  D4. Up-projection (384->768) does NOT create new information; it is a
      deterministic, reversible-ish mapping declared by projection_id.
"""
import sys, os, json, hashlib, random
sys.path.insert(0, '.')
import numpy as np

# projection ids
PROJ_TRUNCATE = "truncate"          # keep first D dims
PROJ_PAD_ZERO = "pad_zero"          # pad with zeros to D dims
PROJ_REPEAT = "repeat"              # tile/repeat to reach D (lossy-ish, deterministic)
PROJ_RANDOM_ORTHO = "random_ortho"  # seeded random orthogonal projection (D_src -> D_dst)
PROJ_PCA_LIKE = "pca_like"         # seeded random + normalize (placeholder for real PCA)

ALL_PROJS = (PROJ_TRUNCATE, PROJ_PAD_ZERO, PROJ_REPEAT,
             PROJ_RANDOM_ORTHO, PROJ_PCA_LIKE)


class FruitConfig:
    __slots__ = ("fruit_id", "embedding_dim", "schema_id",
                 "projection_id", "metric_id", "common_dim")

    def __init__(self, fruit_id, embedding_dim, schema_id=0,
                 projection_id=PROJ_TRUNCATE, metric_id=0, common_dim=None):
        self.fruit_id = int(fruit_id)
        self.embedding_dim = int(embedding_dim)
        self.schema_id = int(schema_id)
        self.projection_id = str(projection_id)
        self.metric_id = int(metric_id)
        self.common_dim = int(common_dim) if common_dim is not None else None
        if self.projection_id not in ALL_PROJS:
            raise ValueError(f"unknown projection_id {self.projection_id}")

    def to_dict(self):
        return {
            "fruit_id": self.fruit_id,
            "embedding_dim": self.embedding_dim,
            "schema_id": self.schema_id,
            "projection_id": self.projection_id,
            "metric_id": self.metric_id,
            "common_dim": self.common_dim,
        }

    @staticmethod
    def from_dict(d):
        return FruitConfig(**d)


class DimensionAdapter:
    """Maps a source vector (any dim) into a Fruit's embedding_dim."""

    def __init__(self, cfg, seed=0):
        self.cfg = cfg
        self.seed = seed
        self._mat = None  # cached for random_ortho

    def _rng(self):
        return random.Random(hash((self.seed, self.cfg.fruit_id,
                                   self.cfg.projection_id)) & 0xFFFFFFFF)

    def _matrix(self, src_dim, dst_dim):
        if self._mat is not None and self._mat.shape == (dst_dim, src_dim):
            return self._mat
        rng = self._rng()
        # seeded gaussian then gram-schmidt-ish (not full Q, enough for det. ortho-ish)
        m = np.zeros((dst_dim, src_dim), dtype=np.float64)
        for i in range(dst_dim):
            for j in range(src_dim):
                m[i, j] = rng.gauss(0, 1)
        # normalize rows
        for i in range(dst_dim):
            n = np.linalg.norm(m[i])
            if n > 0:
                m[i] /= n
        self._mat = m
        return m

    def adapt(self, vec):
        v = np.asarray(vec, dtype=np.float64).ravel()
        D = self.cfg.embedding_dim
        pid = self.cfg.projection_id
        if pid == PROJ_TRUNCATE:
            if v.size >= D:
                return v[:D].copy()
            # if source smaller, pad with zeros (declared, not silent)
            out = np.zeros(D, dtype=np.float64)
            out[:v.size] = v
            return out
        if pid == PROJ_PAD_ZERO:
            out = np.zeros(D, dtype=np.float64)
            n = min(v.size, D)
            out[:n] = v[:n]
            return out
        if pid == PROJ_REPEAT:
            if v.size == 0:
                return np.zeros(D, dtype=np.float64)
            out = np.zeros(D, dtype=np.float64)
            for i in range(D):
                out[i] = v[i % v.size]
            return out
        if pid in (PROJ_RANDOM_ORTHO, PROJ_PCA_LIKE):
            m = self._matrix(v.size, D)
            out = m @ v
            # pca_like also normalizes (declared)
            if pid == PROJ_PCA_LIKE:
                n = np.linalg.norm(out)
                if n > 0:
                    out = out / n
            return out
        raise ValueError(f"unknown projection {pid}")

    def info(self):
        return {
            "fruit_id": self.cfg.fruit_id,
            "embedding_dim": self.cfg.embedding_dim,
            "projection_id": self.cfg.projection_id,
            "note": ("up-projection does not create new information; "
                     "mapping is deterministic given seed+projection_id"),
        }


class CommonSpace:
    """Maps each Fruit's vector into a shared D_common space for comparison."""

    def __init__(self, common_dim, seed=0):
        self.common_dim = int(common_dim)
        self.seed = seed
        self._adapters = {}  # fruit_id -> DimensionAdapter (src=fruit dim -> common)

    def adapter_for(self, cfg):
        # build a FruitConfig pointing at common_dim using random_ortho
        key = cfg.fruit_id
        if key in self._adapters:
            return self._adapters[key]
        common_cfg = FruitConfig(fruit_id=cfg.fruit_id,
                                 embedding_dim=self.common_dim,
                                 schema_id=cfg.schema_id,
                                 projection_id=PROJ_RANDOM_ORTHO,
                                 metric_id=cfg.metric_id,
                                 common_dim=self.common_dim)
        # source dim = cfg.embedding_dim; adapter's matrix built lazily in adapt()
        # We store a wrapper that knows src dim.
        ad = _CommonAdapter(common_cfg, src_dim=cfg.embedding_dim, seed=self.seed, common_dim=self.common_dim)
        self._adapters[key] = ad
        return ad

    def to_common(self, cfg, vec_fruit_dim):
        ad = self.adapter_for(cfg)
        return ad.adapt(vec_fruit_dim)

    def distance(self, cfg_a, vec_a, cfg_b, vec_b):
        # compare in common space only
        ca = self.to_common(cfg_a, vec_a)
        cb = self.to_common(cfg_b, vec_b)
        return float(np.linalg.norm(ca - cb))


class _CommonAdapter(DimensionAdapter):
    def __init__(self, cfg, src_dim, seed, common_dim):
        super().__init__(cfg, seed=seed)
        self._src_dim = src_dim
        self.common_dim = int(common_dim)

    def adapt(self, vec):
        v = np.asarray(vec, dtype=np.float64).ravel()
        if v.size != self._src_dim:
            out = np.zeros(self._src_dim, dtype=np.float64)
            n = min(v.size, self._src_dim)
            out[:n] = v[:n]
            v = out
        m = self._matrix(self._src_dim, self.common_dim)
        return m @ v


class FruitDataStore:
    """Per-fruit data container. Geometry (Snake/Fruit) is separate."""

    def __init__(self, storage_dir=None):
        self.storage_dir = storage_dir
        self.fruits = {}  # fruit_id -> {"config": FruitConfig, "vectors": {key: np.array}}
        if storage_dir:
            os.makedirs(storage_dir, exist_ok=True)
            self._load()

    def register(self, cfg):
        if cfg.fruit_id in self.fruits:
            # allow re-register only if identical
            old = self.fruits[cfg.fruit_id]["config"]
            if old.to_dict() != cfg.to_dict():
                raise ValueError(f"fruit {cfg.fruit_id} config mismatch")
            return
        self.fruits[cfg.fruit_id] = {"config": cfg, "vectors": {}}
        self._save()

    def add(self, fruit_id, key, vec_canonical):
        """Ingest a canonical-dim vector; adapter maps to fruit dim."""
        if fruit_id not in self.fruits:
            raise KeyError(f"unknown fruit {fruit_id}")
        entry = self.fruits[fruit_id]
        cfg = entry["config"]
        ad = DimensionAdapter(cfg, seed=cfg.fruit_id)
        v_fruit = ad.adapt(vec_canonical)
        assert v_fruit.shape == (cfg.embedding_dim,), "adapter dim mismatch"
        entry["vectors"][str(key)] = v_fruit
        self._save()
        return v_fruit

    def get(self, fruit_id, key):
        return self.fruits[fruit_id]["vectors"][str(key)]

    def dims(self):
        return {fid: self.fruits[fid]["config"].embedding_dim
                for fid in self.fruits}

    def _save(self):
        if not self.storage_dir:
            return
        path = os.path.join(self.storage_dir, "fruit_data.json")
        data = {}
        for fid, e in self.fruits.items():
            data[str(fid)] = {
                "config": e["config"].to_dict(),
                "vectors": {k: v.tolist() for k, v in e["vectors"].items()},
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _load(self):
        if not self.storage_dir:
            return
        path = os.path.join(self.storage_dir, "fruit_data.json")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for fid, e in data.items():
            cfg = FruitConfig.from_dict(e["config"])
            self.fruits[int(fid)] = {
                "config": cfg,
                "vectors": {k: np.array(v, dtype=np.float64) for k, v in e["vectors"].items()},
            }
