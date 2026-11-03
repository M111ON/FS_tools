"""Single Ingress & Branch Fingerprinting prototype (Python).

Sits on top of the FROZEN Baseline v1.0 primitives (Snake/Fruit/Graft/persistence).
Does NOT modify baseline. Does NOT add ANN. Does NOT change geometry.

Pipeline:
  SINGLE INGRESS
    -> INGESTION GATEWAY   (validate . normalize . identify)
    -> BRANCH TRAVERSAL    (routing rules, deterministic + auditable)
    -> FINGERPRINT GEN     (per-path signature: which branches/positions/frames)
    -> APPEND / PERSIST    (append to existing branches + append-only relation log)

Design rules (restated from owner spec):
  R1. One ingress function for all 384D embedding input.
  R2. Gateway validates shape/dtype/finite/non-zero, L2-normalizes, identifies by
      hash of normalized bytes (value-blind: no raw vector stored downstream).
  R3. Routing is deterministic + auditable: branch[s] = sha256(item_id:s) mod N,
      direction[s] = sha256(item_id:s:dir) mod 6.
  R4. Each traversed path produces its own fingerprint (branch + position + cube
      letters). Fingerprint describes the PATH, not just cube identity.
  R5. New items append to existing branches + relation log; no rebuild.
  R6. All geometry/branch core stays in existing primitives (Snake.grow etc.).
"""
import sys, os, json, hashlib, time, random
sys.path.insert(0, '.')
import numpy as np
from snake_sim import Snake, domain_of, seed_rng
from fruit_dim import FruitConfig, DimensionAdapter, FruitDataStore, CommonSpace

EMBED_DIM = 384


class IngestResult:
    def __init__(self, item_id, ok, reason, route, step_records, fingerprints,
                 path_fp, item_fp, elapsed_ms):
        self.item_id = item_id
        self.ok = ok
        self.reason = reason
        self.route = route
        self.step_records = step_records
        self.fingerprints = fingerprints
        self.path_fp = path_fp
        self.item_fp = item_fp
        self.elapsed_ms = elapsed_ms


class IngestionGateway:
    @staticmethod
    def validate(vec):
        try:
            arr = np.asarray(vec)
        except Exception as e:
            return False, f"not_array:{e}", None
        if arr.ndim != 1 or arr.size != EMBED_DIM:
            return False, f"bad_shape:{tuple(arr.shape)}", None
        arr = arr.astype(np.float64)
        if not np.all(np.isfinite(arr)):
            return False, "non_finite", None
        if float(np.linalg.norm(arr)) <= 0.0:
            return False, "zero_vector", None
        return True, "ok", arr

    @staticmethod
    def normalize(arr):
        n = float(np.linalg.norm(arr))
        if n <= 0.0:
            return None
        return arr / n

    @staticmethod
    def identify(arr_norm):
        return hashlib.sha256(arr_norm.tobytes()).hexdigest()[:16]


class RoutingRules:
    def __init__(self, num_branches, num_steps):
        self.num_branches = num_branches
        self.num_steps = num_steps

    def route(self, item_id):
        steps = []
        for s in range(self.num_steps):
            bh = hashlib.sha256(f"{item_id}:{s}".encode()).hexdigest()
            branch = int(bh, 16) % self.num_branches
            dh = hashlib.sha256(f"{item_id}:{s}:dir".encode()).hexdigest()
            direction = int(dh, 16) % 6
            steps.append({"step": s, "branch": branch, "direction": direction})
        return steps


class FingerprintGenerator:
    @staticmethod
    def step_fp(branch_idx, pos, letters, step):
        try:
            from compact_identity import encode_string
            enc = encode_string(letters)
        except Exception:
            enc = str(letters)
        return f"B{branch_idx}:P{pos}:S{step}:{enc}"

    @staticmethod
    def path_fp(step_fps):
        if not step_fps:
            return None
        return hashlib.sha256("|".join(step_fps).encode()).hexdigest()[:16]

    @staticmethod
    def item_fp(item_id, path_fp):
        if path_fp is None:
            return None
        return f"{item_id}:{path_fp}"


class BranchManager:
    def __init__(self, branch_count, domain_size, seed):
        self.branch_count = branch_count
        self.domain_size = domain_size
        self.seed = seed
        self.branches = []
        self._init_branches()

    def _init_branches(self):
        self.branches = []
        for i in range(self.branch_count):
            bseed = f"{self.seed}_branch_{i}"
            start = (self.domain_size // 2 + (i * 7) % 50,
                     self.domain_size // 2 + (i * 11) % 50,
                     self.domain_size // 2 + (i * 13) % 50)
            sn = Snake(bseed, mode='chain', domain_size=self.domain_size, start_pos=start)
            self.branches.append(sn)

    def append(self, branch_idx, direction):
        br = self.branches[branch_idx]
        ok, reason = br.grow(direction)
        if ok:
            pos = br.positions[-1]
            return pos, br.letters[pos], direction, False, True, "ok"
        for d in range(6):
            ok2, r2 = br.grow(d)
            if ok2:
                pos = br.positions[-1]
                return pos, br.letters[pos], d, True, True, "fallback_ok"
        return None, None, None, False, False, "no_placement"

    def state_dicts(self):
        return [b.state_dict() for b in self.branches]

    def restore_from(self, states):
        if len(states) != len(self.branches):
            self.branches = []
            for i, st in enumerate(states):
                bseed = f"{self.seed}_branch_{i}"
                sn = Snake(bseed, mode='chain', domain_size=self.domain_size)
                sn.restore(st)
                self.branches.append(sn)
            self.branch_count = len(self.branches)
        else:
            for b, st in zip(self.branches, states):
                b.restore(st)


class RelationStore:
    def __init__(self, path):
        self.path = path
        self._count = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._count = sum(1 for _ in f if _.strip())

    def append(self, record):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        self._count += 1

    def load_all(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def count(self):
        return self._count


class IngestionPipeline:
    def __init__(self, branch_count=4, steps_per_item=3, domain_size=1000,
                 seed="lc_ingest", storage_dir=None, fruit_configs=None,
                 common_dim=None):
        self.branch_count = branch_count
        self.steps_per_item = steps_per_item
        self.domain_size = domain_size
        self.seed = seed
        self.storage_dir = storage_dir
        # per-branch (Fruit) embedding config; default = 384D truncate for all
        if fruit_configs is None:
            fruit_configs = [FruitConfig(fruit_id=i, embedding_dim=384,
                                         projection_id="truncate")
                             for i in range(branch_count)]
        assert len(fruit_configs) == branch_count, "fruit_configs must match branch_count"
        self.fruit_configs = list(fruit_configs)
        self.common_dim = common_dim
        self.common_space = CommonSpace(common_dim, seed=seed) if common_dim else None
        self.gateway = IngestionGateway()
        self.router = RoutingRules(branch_count, steps_per_item)
        self.fpgen = FingerprintGenerator()
        self.branch_mgr = BranchManager(branch_count, domain_size, seed)
        self.fruit_store = FruitDataStore(storage_dir) if storage_dir else None
        if self.fruit_store is not None:
            for cfg in self.fruit_configs:
                self.fruit_store.register(cfg)
        self.relations = None
        if storage_dir:
            os.makedirs(storage_dir, exist_ok=True)
            self.relations = RelationStore(os.path.join(storage_dir, "relations.jsonl"))
            self._load_if_exists()

    def _meta_path(self):
        return os.path.join(self.storage_dir, "meta.json") if self.storage_dir else None

    def _branches_path(self):
        return os.path.join(self.storage_dir, "branches.json") if self.storage_dir else None

    def _load_if_exists(self):
        if not self.storage_dir:
            return
        bp = self._branches_path()
        if os.path.exists(bp):
            with open(bp, encoding="utf-8") as f:
                states = json.load(f)
            self.branch_mgr.restore_from(states)
            self.branch_count = len(self.branch_mgr.branches)
            self.router = RoutingRules(self.branch_count, self.steps_per_item)
        mp = self._meta_path()
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            self.steps_per_item = m.get("steps_per_item", self.steps_per_item)
            self.domain_size = m.get("domain_size", self.domain_size)
            self.seed = m.get("seed", self.seed)

    def _save_branches(self):
        if not self.storage_dir:
            return
        with open(self._branches_path(), "w", encoding="utf-8") as f:
            json.dump(self.branch_mgr.state_dicts(), f)
        meta = {
            "branch_count": self.branch_count,
            "steps_per_item": self.steps_per_item,
            "domain_size": self.domain_size,
            "seed": self.seed,
        }
        with open(self._meta_path(), "w", encoding="utf-8") as f:
            json.dump(meta, f)

    def ingest(self, vec):
        t0 = time.perf_counter()
        ok, reason, arr = self.gateway.validate(vec)
        if not ok:
            return IngestResult(None, False, reason, [], [], [], None, None,
                               (time.perf_counter() - t0) * 1000)
        arr_n = self.gateway.normalize(arr)
        if arr_n is None:
            return IngestResult(None, False, "normalize_failed", [], [], [], None, None,
                               (time.perf_counter() - t0) * 1000)
        item_id = self.gateway.identify(arr_n)

        steps = self.router.route(item_id)
        step_records = []
        step_fps = []
        fruit_dims_used = []
        for st in steps:
            bidx = st["branch"]
            d = st["direction"]
            pos, letters, dir_used, fallback, ok2, reason2 = self.branch_mgr.append(bidx, d)
            if not ok2:
                step_records.append({"step": st["step"], "branch": bidx, "direction": d,
                                    "ok": False, "reason": reason2, "pos": None,
                                    "fallback": False})
                continue
            fp = self.fpgen.step_fp(bidx, pos, letters, st["step"])
            step_fps.append(fp)
            # Data Layer: adapt canonical vector into this Fruit's dim + store
            cfg = self.fruit_configs[bidx]
            ad = DimensionAdapter(cfg, seed=cfg.fruit_id)
            v_fruit = ad.adapt(arr_n)  # numpy array, shape (cfg.embedding_dim,)
            if self.fruit_store is not None:
                self.fruit_store.add(cfg.fruit_id, f"{item_id}:{st['step']}", v_fruit)
            fruit_dims_used.append(cfg.embedding_dim)
            step_records.append({"step": st["step"], "branch": bidx, "direction": dir_used,
                                "ok": True, "reason": "ok", "pos": list(pos),
                                "fallback": fallback, "fingerprint": fp,
                                "fruit_id": cfg.fruit_id,
                                "fruit_dim": cfg.embedding_dim})

        path_fp = self.fpgen.path_fp(step_fps)
        item_fp = self.fpgen.item_fp(item_id, path_fp)

        if self.relations is not None:
            self.relations.append({
                "item_id": item_id,
                "branches": [s["branch"] for s in steps],
                "positions": [sr["pos"] for sr in step_records if sr["ok"]],
                "step_fingerprints": step_fps,
                "path_fingerprint": path_fp,
                "item_fingerprint": item_fp,
                "ok_steps": sum(1 for sr in step_records if sr["ok"]),
                "total_steps": len(steps),
                "fruit_dims": fruit_dims_used,
                "fruit_ids": [self.fruit_configs[s["branch"]].fruit_id for s in steps],
            })
        self._save_branches()

        overall_ok = len(step_fps) > 0
        elapsed = (time.perf_counter() - t0) * 1000
        return IngestResult(item_id, overall_ok,
                            "ok" if overall_ok else "no_steps_succeeded",
                            [s["branch"] for s in steps], step_records, step_fps,
                            path_fp, item_fp, elapsed)

    def status(self):
        sizes = [len(b.positions) for b in self.branch_mgr.branches]
        rel_count = self.relations.count() if self.relations else 0
        return {"branches": self.branch_count, "branch_sizes": sizes,
                "total_cubes": sum(sizes), "relations": rel_count,
                "fruit_dims": [c.embedding_dim for c in self.fruit_configs]}
