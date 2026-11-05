"""LC_tilecube Read Path — read-only layer on top of frozen Write Path + Persistence.

Does NOT modify Geometry v1.0 or Persistence. Reads branches.json, meta.json,
relations.jsonl, fruit_data.json from a storage_dir produced by
IngestionPipeline. Provides:

    read(address)            -> info at an address
    search(branch, query)    -> ranked candidate item keys in a branch
    reconstruct(item_id)     -> full item reconstruction
    list_addresses(branch)   -> addresses present in a branch

All operations are deterministic and load from disk, so results are identical
across fresh processes (exact equality).
"""
import sys, os, json, hashlib, random
sys.path.insert(0, '.')
import numpy as np
from fruit_dim import FruitConfig, DimensionAdapter, FruitDataStore


class Address:
    """A read-only address: (branch, pos tuple)."""
    __slots__ = ("branch", "pos")

    def __init__(self, branch, pos):
        self.branch = int(branch)
        self.pos = tuple(int(x) for x in pos)

    def key(self):
        return (self.branch, self.pos)

    def to_dict(self):
        return {"branch": self.branch, "pos": list(self.pos)}

    @staticmethod
    def from_dict(d):
        return Address(d["branch"], d["pos"])

    def __eq__(self, other):
        return isinstance(other, Address) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def __repr__(self):
        return f"Addr(b={self.branch}, pos={self.pos})"


class ReadPath:
    def __init__(self, storage_dir):
        self.storage_dir = storage_dir
        self.meta = {}
        self.branches = []          # list of Snake state_dicts
        self.branch_snakes = []      # restored Snake objects (read-only use)
        self.relations = []          # list of relation records
        self.fruit_store = None
        self._reverse = {}           # (branch, pos) -> list of (item_id, step_idx)
        self._load()

    # ---------- loading (read-only) ----------
    def _load(self):
        if not self.storage_dir or not os.path.isdir(self.storage_dir):
            self.meta = {}
            self.branches = []
            self.relations = []
            self.fruit_store = FruitDataStore(None)
            self._build_reverse()
            return
        # meta
        mp = os.path.join(self.storage_dir, "meta.json")
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as f:
                self.meta = json.load(f)
        # branches
        bp = os.path.join(self.storage_dir, "branches.json")
        if os.path.exists(bp):
            with open(bp, encoding="utf-8") as f:
                self.branches = json.load(f)
        # relations
        rp = os.path.join(self.storage_dir, "relations.jsonl")
        self.relations = []
        if os.path.exists(rp):
            with open(rp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.relations.append(json.loads(line))
        # fruit data
        self.fruit_store = FruitDataStore(self.storage_dir)
        self._build_reverse()

    def _build_reverse(self):
        self._reverse = {}
        for rec in self.relations:
            item_id = rec.get("item_id")
            branches = rec.get("branches", [])
            positions = rec.get("positions", [])
            for si, (b, pos) in enumerate(zip(branches, positions)):
                if pos is None:
                    continue
                key = (int(b), tuple(int(x) for x in pos))
                self._reverse.setdefault(key, []).append((item_id, si, rec))

    def reload(self):
        """Re-read from disk (still read-only)."""
        self._load()

    # ---------- helpers ----------
    def _branch_count(self):
        return len(self.branches)

    def _branch_state(self, branch):
        if branch < 0 or branch >= len(self.branches):
            return None
        return self.branches[branch]

    def _positions(self, branch):
        st = self._branch_state(branch)
        if st is None:
            return []
        return [tuple(int(x) for x in p) for p in st.get("positions", [])]

    def _letters_at(self, branch, pos):
        st = self._branch_state(branch)
        if st is None:
            return None
        letters = st.get("letters", {})
        key = ",".join(str(x) for x in pos)
        val = letters.get(key)
        if val is None:
            return None
        # normalize to list of [letters, frame]
        out = []
        for rv in val:
            if isinstance(rv, list) and len(rv) == 2:
                out.append([list(rv[0]), rv[1]])
            else:
                out.append(rv)
        return out

    def _fruit_id_for_branch(self, branch):
        # default mapping: fruit_id == branch (matches IngestionPipeline default)
        return int(branch)

    def _fruit_cfg(self, fruit_id):
        if self.fruit_store is None or fruit_id not in self.fruit_store.fruits:
            return None
        return self.fruit_store.fruits[fruit_id]["config"]

    # ---------- public API ----------
    def list_addresses(self, branch):
        """Return list of Address present in the given branch (in storage order)."""
        branch = int(branch)
        out = []
        for pos in self._positions(branch):
            out.append(Address(branch, pos))
        return out

    def read(self, address):
        """Read info at an address.

        address may be an Address, a dict {"branch":..,"pos":..}, or a
        (branch, pos) / (branch, index) tuple.
        """
        # normalize address
        if isinstance(address, Address):
            branch = address.branch
            pos = address.pos
        elif isinstance(address, dict):
            branch = int(address["branch"])
            pos = tuple(int(x) for x in address["pos"])
        elif isinstance(address, (tuple, list)):
            if len(address) == 2 and isinstance(address[1], int) and \
               not (isinstance(address[1], (tuple, list))):
                # (branch, index)
                branch = int(address[0])
                positions = self._positions(branch)
                idx = int(address[1])
                if idx < 0 or idx >= len(positions):
                    return None
                pos = positions[idx]
            else:
                branch = int(address[0])
                pos = tuple(int(x) for x in address[1])
        else:
            return None

        positions = self._positions(branch)
        # find index of pos
        pos_index = None
        for i, p in enumerate(positions):
            if p == pos:
                pos_index = i
                break
        if pos_index is None:
            # address not present in this branch
            pass

        letters = self._letters_at(branch, pos)
        rev = self._reverse.get((branch, pos), [])
        item_ids = []
        steps = []
        for (iid, si, rec) in rev:
            item_ids.append(iid)
            steps.append({"item_id": iid, "step": si,
                          "path_fingerprint": rec.get("path_fingerprint")})

        return {
            "branch": int(branch),
            "pos": [int(x) for x in pos],
            "pos_index": pos_index,
            "present": pos_index is not None,
            "letters": letters,
            "item_ids": item_ids,
            "steps": steps,
        }

    def reconstruct(self, item_id):
        """Reconstruct a full item from persisted relations + fruit vectors."""
        recs = [r for r in self.relations if r.get("item_id") == item_id]
        if not recs:
            return None
        rec = recs[0]
        branches = rec.get("branches", [])
        positions = rec.get("positions", [])
        addresses = []
        for b, pos in zip(branches, positions):
            if pos is None:
                addresses.append(None)
            else:
                addresses.append(Address(int(b), tuple(int(x) for x in pos)).to_dict())

        # gather vectors per step
        vectors = {}
        fruit_ids = rec.get("fruit_ids", [])
        for si, b in enumerate(branches):
            fid = fruit_ids[si] if si < len(fruit_ids) else self._fruit_id_for_branch(b)
            key = f"{item_id}:{si}"
            if self.fruit_store is not None and fid in self.fruit_store.fruits:
                v = self.fruit_store.fruits[fid]["vectors"].get(str(key))
                if v is not None:
                    vectors[str(si)] = [float(x) for x in np.asarray(v).ravel().tolist()]

        return {
            "item_id": item_id,
            "branches": [int(x) for x in branches],
            "positions": [[int(x) for x in p] if p is not None else None for p in positions],
            "step_fingerprints": list(rec.get("step_fingerprints", [])),
            "path_fingerprint": rec.get("path_fingerprint"),
            "item_fingerprint": rec.get("item_fingerprint"),
            "ok_steps": int(rec.get("ok_steps", 0)),
            "total_steps": int(rec.get("total_steps", 0)),
            "fruit_ids": [int(x) for x in fruit_ids] if fruit_ids else [self._fruit_id_for_branch(b) for b in branches],
            "fruit_dims": [int(x) for x in rec.get("fruit_dims", [])],
            "addresses": addresses,
            "vectors": vectors,
            "relation_count": len(recs),
        }

    def search(self, branch, query, top_k=5):
        """Rank stored items in a branch by similarity to query.

        query may be a vector (list/array) or an item_id string. Returns list of
        dicts: {key, item_id, step, score}.
        """
        branch = int(branch)
        fid = self._fruit_id_for_branch(branch)
        cfg = self._fruit_cfg(fid)
        if cfg is None:
            # no fruit data -> empty
            return []

        # resolve query vector in fruit dim
        if isinstance(query, str):
            # item_id: find its stored vector in this branch
            qvec = None
            prefix = query + ":"
            for key, v in self.fruit_store.fruits[fid]["vectors"].items():
                if str(key).startswith(prefix):
                    # find step whose branch == branch
                    qvec = np.asarray(v, dtype=np.float64).ravel()
                    break
            if qvec is None:
                return []
        else:
            qarr = np.asarray(query, dtype=np.float64).ravel()
            ad = DimensionAdapter(cfg, seed=cfg.fruit_id)
            qvec = ad.adapt(qarr)
            qvec = np.asarray(qvec, dtype=np.float64).ravel()

        # collect branch vectors: keys "item_id:step" where step's branch==branch
        # We don't store branch per vector; filter via relations: items whose
        # branches list contains branch.
        allowed_items = set()
        for rec in self.relations:
            if branch in [int(b) for b in rec.get("branches", [])]:
                allowed_items.add(rec.get("item_id"))

        # Collect allowed (key, iid, step, vec) entries, then score in one
        # batched matmul (wires the fast path; bit-identical to the per-item
        # cosine loop above).
        keys = []; iids = []; steps = []; vecs = []
        store = self.fruit_store.fruits[fid]["vectors"]
        for key, v in store.items():
            key = str(key)
            if ":" not in key:
                continue
            iid, step = key.rsplit(":", 1)
            if iid not in allowed_items:
                continue
            rec = next((r for r in self.relations if r.get("item_id") == iid), None)
            if rec is not None:
                branches = [int(b) for b in rec.get("branches", [])]
                try:
                    si = int(step)
                except ValueError:
                    si = None
                if si is not None and si < len(branches) and branches[si] != branch:
                    continue
            keys.append(key); iids.append(iid); steps.append(step)
            vecs.append(np.asarray(v, dtype=np.float64).ravel())

        results = []
        if vecs:
            M = np.stack(vecs)  # (m, d)
            nq = np.linalg.norm(qvec)
            nv = np.linalg.norm(M, axis=1)
            if nq <= 0:
                scores = np.zeros(len(vecs), dtype=np.float64)
            else:
                dots = M @ np.asarray(qvec, dtype=np.float64)
                denom = nv * nq
                scores = np.where(denom > 0, dots / np.where(denom > 0, denom, 1.0), 0.0)
            for k, iid, step, sc in zip(keys, iids, steps, scores):
                results.append({"key": k, "item_id": iid, "step": step,
                                "score": float(sc)})

        results.sort(key=lambda x: (-x["score"], x["key"]))
        if top_k is not None:
            results = results[:int(top_k)]
        return results


def main():
    # smoke: build a tiny store and read back
    import tempfile, shutil, random
    sys.path.insert(0, '.')
    from ingest_fingerprint import IngestionPipeline
    d = tempfile.mkdtemp()
    try:
        pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                                storage_dir=d)
        def mv(s, dim=384):
            r = random.Random(s)
            return [r.gauss(0, 1) for _ in range(dim)]
        for i in range(5):
            pipe.ingest(mv(1000 + i))
        rp = ReadPath(d)
        print("branches:", rp._branch_count())
        addrs0 = rp.list_addresses(0)
        print("branch0 addresses:", len(addrs0))
        if addrs0:
            print("read first:", rp.read(addrs0[0]))
        # reconstruct first item
        recs = rp.relations
        if recs:
            iid = recs[0]["item_id"]
            print("reconstruct:", rp.reconstruct(iid)["item_id"],
                  "steps:", rp.reconstruct(iid)["ok_steps"])
        # search
        res = rp.search(0, mv(1000))
        print("search branch0:", len(res), "candidates")
    finally:
        shutil.rmtree(d)
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
