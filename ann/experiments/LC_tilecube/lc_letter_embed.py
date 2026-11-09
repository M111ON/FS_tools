"""Deterministic letter-embedding for cube faces (no training required).
Cross-modal bridge: images/text/vectors all map to a face via PCA->grid,
then share the same letter-embedding -> linkage without training.

Letter-embedding = concat of:
  - normalized grid position (3)
  - frame one-hot (8)
  - adjacent-face existence (6)   [which of 6 neighbors exist]
  - twin-pair indicator (1)       [whether frame letter is a twin]
Padded to EMBED_DIM (default 32) with zeros for ANN compatibility.
"""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lettercube_py as lc

EMBED_DIM = 32

def build_face_mapper(X, G=4, seed="letter-embed"):
    """Returns (mapper_fn, mn, mx, basis, letter_map, frame_map)."""
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
    sim = lc.LetterCube(seed, deterministic=True)
    sim.build(uniq)
    letter_map = {}
    frame_map = {}
    neighbor_map = {}
    for p in uniq:
        cube = sim.cubes[p]
        letter_map[p] = cube['inner'][0]
        frame_map[p] = cube['row_vals'][0][1]
        # adjacent faces: 6 directions, exists if neighbor in uniq set
        nbrs = []
        for ax in range(3):
            for d in (-1, 1):
                n = list(p); n[ax] += d; n = tuple(n)
                nbrs.append(1.0 if n in uniq else 0.0)
        neighbor_map[p] = nbrs
    def mapper(vec):
        coord = (vec - X.mean(0)) @ basis
        gp = tuple(((coord - mn) / (mx - mn + 1e-9) * G).astype(int).clip(0, G - 1))
        return gp
    return mapper, mn, mx, basis, letter_map, frame_map, neighbor_map, uniq


def letter_embedding(pos, frame_map, neighbor_map, letter_map):
    """Deterministic embedding for a grid position (face)."""
    vec = np.zeros(EMBED_DIM, dtype=np.float64)
    # normalized grid position (3)
    g = np.array(pos, dtype=np.float64)
    vec[0:3] = g / (g.max() + 1e-9)
    # frame one-hot (8) at indices 3..10
    frame = frame_map[pos]
    fi = ord(frame) - ord('a')
    if 0 <= fi < 8:
        vec[3 + fi] = 1.0
    # adjacent faces (6) at indices 11..16
    vec[11:17] = np.array(neighbor_map[pos])
    # twin indicator: frame letter is a twin if its uppercase appears twice? 
    # simple: 1 if frame in 'abcd' (first half) else 0
    vec[17] = 1.0 if fi < 4 else 0.0
    # letter identity (which inner letter) one-hot-ish via index 18
    li = ord(letter_map[pos]) - ord('A')
    vec[18] = li / 25.0
    return vec


class LetterEmbedStore:
    """Store letter-embeddings and query by cosine similarity (fuzzy face match)."""
    def __init__(self):
        self.items = []  # list of (face_id, embedding, payload)

    def add(self, face_id, embedding, payload=None):
        self.items.append((face_id, np.asarray(embedding, dtype=np.float64), payload))

    def query(self, embedding, topk=5):
        if not self.items:
            return []
        e = np.asarray(embedding, dtype=np.float64)
        en = np.linalg.norm(e) + 1e-9
        res = []
        for fid, emb, payload in self.items:
            sim = float((emb @ e) / (np.linalg.norm(emb) + 1e-9) / en)
            res.append((fid, sim, payload))
        res.sort(key=lambda x: -x[1])
        return res[:topk]

    def __len__(self):
        return len(self.items)


def cross_modal_link(vec, mapper, frame_map, neighbor_map, letter_map, store=None):
    """Map any vector (image/text/weight) to a face + letter-embedding.
    This is the cross-modal bridge: same pipeline for all modalities."""
    pos = mapper(vec)
    emb = letter_embedding(pos, frame_map, neighbor_map, letter_map)
    if store is not None:
        store.add(pos, emb, payload=vec)
    return pos, emb


if __name__ == "__main__":
    HERE = os.path.dirname(os.path.abspath(__file__))
    # use scifact as stand-in corpus (in real use: image embeddings)
    X = np.load(os.path.join(HERE, 'data_other', 'scifact', 'corpus_embeddings.npy'))[:1000].astype(np.float64)
    mapper, mn, mx, basis, letter_map, frame_map, neighbor_map, uniq = build_face_mapper(X)
    store = LetterEmbedStore()
    # embed all corpus faces
    for i in range(len(X)):
        pos, emb = cross_modal_link(X[i], mapper, frame_map, neighbor_map, letter_map, store)
    print(f"stored {len(store)} faces")
    # fuzzy query: query with first vector, find similar faces
    qpos, qemb = cross_modal_link(X[0], mapper, frame_map, neighbor_map, letter_map)
    res = store.query(qemb, topk=5)
    print("top-5 similar faces to query face:")
    for fid, sim, _ in res:
        print(f"  face={fid}  cos={sim:.3f}  frame={frame_map[fid]}")
    # determinism check
    e1 = letter_embedding(uniq[0], frame_map, neighbor_map, letter_map)
    e2 = letter_embedding(uniq[0], frame_map, neighbor_map, letter_map)
    print("determinism:", np.array_equal(e1, e2))
    # cross-modal demo: a "new" vector (mean of 3 random) maps to a face
    new_vec = X[10] + X[20] - X[30]
    npos, nemb = cross_modal_link(new_vec, mapper, frame_map, neighbor_map, letter_map)
    print("new vector -> face:", npos, "frame:", frame_map[npos])
    print("LETTER EMBED OK")
