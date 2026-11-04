"""Measure recall of Read Path search() vs brute force.

Ground truth: global nearest neighbor per query on canonical 384D vectors.
Candidates: union of ReadPath.search(branch, query) across all branches.
Report recall@1, scan%, latency.
"""
import sys, os, tempfile, shutil, random, time
import numpy as np
sys.path.insert(0, '.')
from ingest_fingerprint import IngestionPipeline
from read_path import ReadPath


def make_vec(seed, dim=384):
    r = random.Random(seed)
    return np.array([r.gauss(0, 1) for _ in range(dim)], dtype=np.float64)


def cosine(a, b):
    na = np.linalg.norm(a); nb = np.linalg.norm(b)
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def main():
    N = 200
    d = tempfile.mkdtemp()
    try:
        pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                                storage_dir=d)
        vecs = {}
        ids = []
        for i in range(N):
            v = make_vec(5000 + i)
            r = pipe.ingest(v.tolist())
            ids.append(r.item_id)
            vecs[r.item_id] = v

        rp = ReadPath(d)

        # ground truth: for each query, nearest neighbor among others (canonical)
        queries = ids[:50]
        K = 5  # candidates per branch
        hits = 0
        total_cands = 0
        latencies = []
        for qid in queries:
            qv = vecs[qid]
            # global NN (exclude self)
            best = None; best_score = -2
            for iid, v in vecs.items():
                if iid == qid:
                    continue
                s = cosine(qv, v)
                if s > best_score:
                    best_score = s; best = iid
            gt = best

            # candidates via ReadPath search across branches
            cands = set()
            t0 = time.perf_counter()
            for b in range(3):
                for r in rp.search(b, qv.tolist(), top_k=K):
                    cands.add(r["item_id"])
            latencies.append((time.perf_counter() - t0) * 1000)
            total_cands += len(cands)
            if gt in cands:
                hits += 1

        nq = len(queries)
        print(f"N={N} queries={nq} branches=3 K={K}/branch")
        print(f"recall@1 (global NN in candidate union): {hits}/{nq} = {hits/nq:.4f}")
        print(f"avg candidates/query: {total_cands/nq:.1f}  scan%: {total_cands/nq/N*100:.2f}%")
        print(f"latency p50: {np.median(latencies):.3f} ms  p95: {np.percentile(latencies,95):.3f} ms")
    finally:
        shutil.rmtree(d)
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
