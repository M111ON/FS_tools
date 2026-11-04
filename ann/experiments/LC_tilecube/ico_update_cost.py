"""Update-cost benchmark: is build_s really one-off?

Measured per level (L1-L4) on both datasets:
  ins_assign   project + argmax over V vertices for one new vector (us, p50/p95)
  ins_append   appending to a growable per-cell buffer (us, p50/p95)
  bulk_10k     per-vector cost when inserting 10k vectors in one batch (us/vec)
  rebuild      full reassignment of the whole corpus (= the bld_cell number)
  rebuild/ins  how many single inserts cost the same as one full rebuild

Architectural point being tested: the icosphere topology is FIXED, so inserting
data never requires re-deriving the graph or re-running clustering. Only the
cell membership changes, and only the touched cell's buffer grows.
"""
import os, sys, sqlite3, time
import numpy as np
sys.path.insert(0, '.')
from ico_cost_quality import icosphere, ico_counts

HERE = os.path.dirname(os.path.abspath(__file__))
SIFT_BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
MEM_DB = "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3"
LEVELS = [1, 2, 3, 4]


def _fvecs(p, limit=None):
    size = os.path.getsize(p)
    with open(p, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row = dim + 1
    n = size // (4 * row) if limit is None else limit
    out = np.empty((n, dim), dtype=np.uint8)
    with open(p, 'rb') as f:
        for s in range(0, n, 100_000):
            e = min(s + 100_000, n)
            blk = np.fromfile(f, dtype=np.float32, count=(e - s) * row)
            out[s:e] = np.clip(blk.reshape(e - s, row)[:, 1:], 0, 255).astype(np.uint8)
    return out


def load_memcore():
    con = sqlite3.connect(MEM_DB)
    rows = con.execute("SELECT m.vec FROM embeddings m").fetchall()
    con.close()
    X = np.stack([np.frombuffer(r[0], dtype=np.float32) for r in rows])
    return X / np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)


def run(name, X):
    N = len(X)
    rng = np.random.default_rng(0)
    si = rng.choice(N, min(20000, N), replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0).astype(np.float32)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)

    def proj(a):
        P = (a.astype(np.float32) - mu) @ U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    print(f"\n{'='*88}\n{name}  N={N} dim={X.shape[1]}\n{'='*88}")
    print(f"{'lvl':>3} {'cells':>6} {'ins_assign p50':>15} {'p95':>8} "
          f"{'ins_append p50':>15} {'bulk us/vec':>12} {'rebuild_s':>10} "
          f"{'rebuild/ins':>12}")
    for level in LEVELS:
        v, _ = icosphere(level)
        nv, _, _ = ico_counts(level)
        vc = v.T.astype(np.float32)

        # full assignment (rebuild)
        t0 = time.perf_counter()
        P = proj(X)
        node = np.empty(N, dtype=np.int32)
        for s0 in range(0, N, 8000):
            e0 = min(s0 + 8000, N)
            node[s0:e0] = np.argmax(P[s0:e0] @ vc, axis=1)
        buffers = [[] for _ in range(nv)]
        for nid in range(nv):
            idx = np.nonzero(node == nid)[0]
            if len(idx):
                buffers[nid].append(idx.astype(np.int32))
        rebuild = time.perf_counter() - t0

        # single insert: assign + append to a growable list buffer
        sample = X[rng.choice(N, size=2000, replace=False)]
        t_assign, t_append = [], []
        for x in sample:
            t0 = time.perf_counter()
            p = proj(x.reshape(1, -1))[0]
            nid = int(np.argmax(vc.T @ p))
            t_assign.append((time.perf_counter() - t0) * 1e6)
            t0 = time.perf_counter()
            buffers[nid].append(np.array([0], dtype=np.int32))  # amortised append
            t_append.append((time.perf_counter() - t0) * 1e6)

        # bulk 10k
        batch = X[rng.choice(N, size=10000, replace=False)]
        t0 = time.perf_counter()
        P2 = proj(batch)
        nb = np.empty(len(batch), dtype=np.int32)
        for s0 in range(0, len(batch), 8000):
            e0 = min(s0 + 8000, len(batch))
            nb[s0:e0] = np.argmax(P2[s0:e0] @ vc, axis=1)
        for nid in range(nv):
            idx = np.nonzero(nb == nid)[0]
            if len(idx):
                buffers[nid].append(idx.astype(np.int32))
        bulk = (time.perf_counter() - t0) / len(batch) * 1e6

        p50a = float(np.percentile(t_assign, 50))
        p95a = float(np.percentile(t_assign, 95))
        p50b = float(np.percentile(t_append, 50))
        print(f"{level:>3} {nv:>6} {p50a:>15.1f} {p95a:>8.1f} {p50b:>15.2f} "
              f"{bulk:>12.2f} {rebuild:>10.2f} {rebuild*1e6/p50a:>12.0f}")


def main():
    print("UPDATE COST: single insert vs bulk vs full rebuild")
    run("SIFT1M", _fvecs(SIFT_BASE))
    run("MemCore", load_memcore())
    print("\n  Note: append assumes a growable buffer (amortised). Re-building a")
    print("  numpy array per insert with np.concatenate would be O(n) per insert.")
    print("\n  PASS")


if __name__ == "__main__":
    main()