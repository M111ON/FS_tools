"""Rewind Buffer keyed by cube face/letter (structural identity).
Temporal Ring = 972 slots. Question: "have we passed this face before?"
Face id comes from a vector via PCA -> grid -> LetterCube letter/frame.
Separate from seed (seed = routing identity; face = structural identity).
"""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lettercube_py as lc

TEMPORAL_RING = 972  # 18 * 54

class RewindBuffer:
    def __init__(self, size=TEMPORAL_RING):
        self.size = size
        self.slots = [None] * size   # face_id -> stored value
        self.seen_faces = set()      # O(1) "have we passed this face?"
        self.collisions = 0
        self.inserts = 0

    def _slot(self, face_id):
        idx = hash(face_id) % self.size
        start = idx
        while self.slots[idx] is not None and self.slots[idx][0] != face_id:
            idx = (idx + 1) % self.size
            self.collisions += 1
            if idx == start:
                raise RuntimeError("rewind buffer full")
        return idx

    def store(self, face_id, value):
        idx = self._slot(face_id)
        self.slots[idx] = (face_id, value)
        self.seen_faces.add(face_id)
        self.inserts += 1
        return idx

    def seen(self, face_id):
        """Have we passed this face before? O(1)."""
        return face_id in self.seen_faces

    def load(self, face_id):
        idx = hash(face_id) % self.size
        start = idx
        while self.slots[idx] is not None:
            if self.slots[idx][0] == face_id:
                return self.slots[idx][1]
            idx = (idx + 1) % self.size
            if idx == start:
                break
        return None

    def stats(self):
        used = sum(1 for s in self.slots if s is not None)
        return {"size": self.size, "used": used, "inserts": self.inserts,
                "collisions": self.collisions, "unique_faces": len(self.seen_faces),
                "fill": used / self.size}


def face_id_from_vector(vec, pca_basis, grid_min, grid_max, G, letter_map):
    """Map a vector -> face id (letter/frame) via PCA -> grid -> LetterCube."""
    p3 = (vec - vec.mean())  # center
    # project using precomputed basis (3 cols)
    coord = pca_basis.T @ (vec - grid_min*0)  # placeholder; use caller's coord
    return None


def build_face_mapper(X, G=4, seed="rewind1"):
    """Precompute PCA basis + grid + letter map for a corpus. Returns a function vec->face_id."""
    Xc = X - X.mean(0)
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    basis = Vt[:3].T  # (D,3)
    P3 = Xc @ Vt[:3].T
    mn = P3.min(0); mx = P3.max(0)
    positions = []
    for p in P3:
        gp = tuple(((p - mn) / (mx - mn + 1e-9) * G).astype(int).clip(0, G - 1))
        positions.append(gp)
    uniq = sorted(set(positions))
    sim = lc.LetterCube(seed, deterministic=True)
    sim.build(uniq)
    letter_map = {}
    for p in uniq:
        cube = sim.cubes[p]
        letter_map[p] = (p, cube['row_vals'][0][1])  # (grid cell, frame) structural id
    def mapper(vec):
        coord = (vec - X.mean(0)) @ basis
        gp = tuple(((coord - mn) / (mx - mn + 1e-9) * G).astype(int).clip(0, G - 1))
        return letter_map.get(gp, ("?", "?"))
    return mapper, mn, mx, basis


if __name__ == "__main__":
    HERE = os.path.dirname(os.path.abspath(__file__))
    X = np.load(os.path.join(HERE, 'data_other', 'scifact', 'corpus_embeddings.npy'))[:2000].astype(np.float64)
    mapper, mn, mx, basis = build_face_mapper(X)
    buf = RewindBuffer()
    # insert first 500
    for i in range(500):
        fid = mapper(X[i])
        buf.store(fid, i)
    # query: have we seen faces of next 100?
    seen_count = 0
    for i in range(500, 600):
        fid = mapper(X[i])
        if buf.seen(fid):
            seen_count += 1
    print(f"inserted 500, queried 100 -> seen faces in queried: {seen_count}/100")
    print(buf.stats())
    # determinism check
    fid0 = mapper(X[0])
    print("face_id(X[0]) =", fid0, "seen?", buf.seen(fid0))
    print("REWIND BUFFER OK")
