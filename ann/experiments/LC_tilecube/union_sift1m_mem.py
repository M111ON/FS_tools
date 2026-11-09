"""Memory-efficient sift1m union runner.
Uses np.memmap for corpus (no full load), sequential query processing,
float32 distance, disk spill for results. Embodies breathing_fs principle:
data stays on disk (memmap), only pointers/active view move.
"""
import os, sys, time, json, struct, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
# paths
SIFT_BASE = "I:/tools/ann/experiments/LC_tilecube/sift/sift_base.fvecs"
SIFT_QUERY = "I:/tools/ann/experiments/LC_tilecube/sift/sift_query.fvecs"
GT_PATH = "I:/tools/ann/experiments/LC_tilecube/sift/sift_groundtruth.ivecs"
OUT = "I:/tools/ann/experiments/LC_tilecube/sift1m_mem_result.json"

def read_fvecs_header(path):
    with open(path, "rb") as f:
        dim = struct.unpack("i", f.read(4))[0]
    # count rows
    fsize = os.path.getsize(path)
    row_size = 4 + dim * 4
    n = (fsize) // row_size
    return dim, n

def memmap_fvecs(path):
    dim, n = read_fvecs_header(path)
    # fvecs: each row = int32 dim + dim float32
    # memmap the whole file then view
    mm = np.memmap(path, dtype=np.uint8, mode="r")
    # parse: each row row_size bytes
    row_size = 4 + dim * 4
    # reshape to (n, row_size) then skip first 4 bytes per row
    arr = mm.reshape(n, row_size)[:, 4:].view(np.float32).reshape(n, dim)
    # memmap view (read-only, zero-copy)
    return arr, dim, n

def read_ivecs(path, nq):
    # ivecs: each row = int32 dim + dim int32
    with open(path, "rb") as f:
        first = struct.unpack("i", f.read(4))[0]
        f.seek(0)
        # assume all rows same dim
        dim = first
        row_size = 4 + dim * 4
        fsize = os.path.getsize(path)
        n = fsize // row_size
        n = min(n, nq)
        gt = np.zeros((n, dim), dtype=np.int32)
        f.seek(0)
        for i in range(n):
            d = struct.unpack("i", f.read(4))[0]
            gt[i] = np.fromfile(f, dtype=np.int32, count=d)
        return gt, n

def lc_seed_vec(vec):
    q = [int(x * (1 << 20)) for x in vec]
    if not q: return 0
    m = sum(q) // len(q)
    qc = [x - m for x in q]
    per = len(qc) // 3
    if per < 1: per = 1
    sums = [sum(qc[g*per:(g+1)*per]) for g in range(3)]
    trit = 0
    for s in sums:
        d = 0 if s < 0 else (2 if s > 0 else 1)
        trit = trit * 3 + d
    trit %= 27
    groups = 5
    per2 = len(qc) // groups
    if per2 < 1: per2 = 1
    bits = 0
    for g in range(groups):
        s = sum(qc[g*per2:(g+1)*per2])
        if s >= 0: bits |= (1 << g)
    return (trit << 5) | (bits % 32)

def main():
    print("memmap corpus...")
    X, D, N = memmap_fvecs(SIFT_BASE)
    print(f"corpus: {N} x {D} (memmap, ~0 RAM)")
    # queries
    Qmm, Dq, Nq = memmap_fvecs(SIFT_QUERY)
    Nq = min(Nq, 100)  # use first 100 queries
    print(f"queries: {Nq}")
    # ground truth
    gt, gt_n = read_ivecs(GT_PATH, Nq)
    print(f"gt: {gt_n} queries, top-{gt.shape[1]}")

    # IVF: k-means on a SUBSAMPLE (memory-efficient: don't fit on 1M)
    SUB = 50000
    rng = np.random.default_rng(0)
    idx_sub = rng.choice(N, size=SUB, replace=False)
    Xsub = np.array(X[idx_sub], dtype=np.float32)  # only 50k x 128 = 25MB
    from sklearn.cluster import KMeans
    print("fitting IVF on subsample...")
    t0 = time.time()
    km = KMeans(n_clusters=64, random_state=0, n_init=1)
    km.fit(Xsub)
    centers = km.cluster_centers_.astype(np.float32)
    print(f"IVF fit: {time.time()-t0:.1f}s")

    # assign labels lazily: for each query, find nearest centers (no full label array)
    # Instead of precomputing labels for all 1M, compute on-demand per candidate set
    # Use: candidates = points whose nearest center is in top-probe centers
    # We need labels per point. Compute labels in chunks and store as memmap on disk.
    label_path = "I:/tools/ann/experiments/LC_tilecube/sift1m_labels.npy"
    if not os.path.exists(label_path):
        print("computing labels in chunks (memmap to disk)...")
        labels_mm = np.memmap(label_path, dtype=np.int8, mode="w+", shape=(N,))
        CHUNK = 65536
        t0 = time.time()
        for start in range(0, N, CHUNK):
            sl = slice(start, min(start+CHUNK, N))
            chunk = np.array(X[sl], dtype=np.float32)
            # nearest center per row
            # (chunk^2 + center^2 - 2*chunk@center)
            d = (chunk**2).sum(1)[:,None] + (centers**2).sum(1)[None,:] - 2*chunk@centers.T
            labels_mm[sl] = np.argmin(d, axis=1).astype(np.int8)
            if start % (CHUNK*4) == 0:
                print(f"  labeled {min(start+CHUNK,N)}/{N}")
        labels_mm.flush()
        print(f"labels done: {time.time()-t0:.1f}s")
    else:
        labels_mm = np.memmap(label_path, dtype=np.int8, mode="r", shape=(N,))
        print("loaded cached labels")

    # LC seeds: compute per-query only (no full array)
    # Process queries sequentially
    K = 6  # views (we'll use 1 view: raw + centered variations via seed)
    results = []
    t0 = time.time()
    for qi in range(Nq):
        q = np.array(Qmm[qi], dtype=np.float32)
        # IVF candidates
        darr = np.sum((centers - q)**2, axis=1)
        tabs = np.argsort(darr)[:4]
        # find candidate indices: scan labels in chunks
        cands = []
        CHUNK = 65536
        for start in range(0, N, CHUNK):
            sl = slice(start, min(start+CHUNK, N))
            chunk_labels = np.array(labels_mm[sl])
            mask = np.isin(chunk_labels, tabs)
            local = np.where(mask)[0] + start
            cands.append(local)
        cands = np.concatenate(cands) if cands else np.array([], dtype=np.int64)
        # rerank in float32
        if len(cands) > 0:
            # gather in chunks to avoid large alloc
            best_idx = -1; best_d = 1e30
            # compute distances in chunks of cands
            CCHUNK = 8192
            all_d = []
            for cs in range(0, len(cands), CCHUNK):
                ce = min(cs+CCHUNK, len(cands))
                cc = cands[cs:ce]
                chunk = np.array(X[cc], dtype=np.float32)
                dd = np.sum((chunk - q)**2, axis=1)
                all_d.append(dd)
            all_d = np.concatenate(all_d)
            order = np.argsort(all_d)[:10]
            top = cands[order]
        else:
            top = np.array([], dtype=np.int64)
        # recall
        gti = set(int(x) for x in gt[qi][:10])
        hit = len(set(int(x) for x in top) & gti)
        results.append({"qi": qi, "recall": hit/10.0, "ncand": len(cands)})
        if qi < 5 or qi % 20 == 0:
            print(f"  q{qi}: recall@10={hit/10:.2f} cand={len(cands)}")
    elapsed = time.time() - t0
    recalls = [r["recall"] for r in results]
    cands = [r["ncand"] for r in results]
    print(f"\n=== sift1m (memmap, float32, IVF-64, 4 probe) ===")
    print(f"queries={Nq}  mean recall@10={np.mean(recalls):.3f}  mean cand%={np.mean(cands)/N*100:.2f}%")
    print(f"time={elapsed:.1f}s  ({elapsed/Nq*1000:.1f} ms/query)")
    # save
    with open(OUT, "w") as f:
        json.dump({"n": N, "nq": Nq, "mean_recall": float(np.mean(recalls)),
                   "mean_cand_pct": float(np.mean(cands)/N*100),
                   "ms_per_query": float(elapsed/Nq*1000)}, f, indent=2)
    print("saved", OUT)

if __name__ == "__main__":
    main()
