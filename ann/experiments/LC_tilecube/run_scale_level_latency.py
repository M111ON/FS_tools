"""Scale test + Level sweep + Latency (p50/p95) + overlap."""
import sys, time, numpy as np
sys.path.insert(0, '.')
from lc_label_bucket import Label, Bucket, pack, SCALES, CFG, DOORS
from ann_bench import brute_nn, kmeans, assign

def load_sift(path, maxr):
    d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
    raw = np.fromfile(path, dtype=np.float32, count=maxr*(d+1))
    a = raw[:(len(raw)//(d+1))*(d+1)].reshape(-1, d+1)[:, 1:]
    return a.astype(np.float32)

rng = np.random.default_rng(7)
QALL = load_sift('I:/tools/ann/data/sift/sift/sift_query.fvecs', 200)
QALL = QALL[:200]
XFULL = load_sift('I:/tools/ann/data/sift/sift/sift_base.fvecs', 1000000)

def run_one(N, L, nq=100, seed=5):
    X = XFULL[:N]
    Q = QALL[:nq]
    gt = brute_nn(Q, X)
    rng_l = np.random.default_rng(seed)

    # IVF
    nlist = max(16, int(1.0 * np.sqrt(N)))
    t0 = time.perf_counter()
    C = kmeans(X, nlist, np.random.default_rng(seed), iters=10)
    cell = assign(X, C)
    t_ivf_build = time.perf_counter() - t0
    cq = (C ** 2).sum(1)[None] - 2 * Q @ C.T
    ivf_order = np.argsort(cq, 1)

    nprobe = 2
    # LC build
    t0 = time.perf_counter()
    lc_labs = []
    lc_buckets = []
    for lvl in range(L):
        c = dict(CFG, PROJ='pca', S=0.3)
        lab = Label(X, rng_l, c)
        lc_labs.append(lab)
        lc_buckets.append(Bucket(pack(lab(X)[0], c)))
    t_lc_build = time.perf_counter() - t0

    # per-query timing + metrics
    ivf_hits = 0; lc_hits = 0; union_hits = 0; rerank_hits = 0
    lc_unique_total = 0
    overlap_total = 0
    union_sizes = []
    latencies = []
    for qi in range(nq):
        t_start = time.perf_counter()
        # IVF candidates
        ivf_c = set()
        for ci in ivf_order[qi, :nprobe]:
            ids = np.where(cell == ci)[0]
            ivf_c.update(ids.tolist())
        # LC candidates
        lc_c = set()
        for lvl in range(L):
            lab = lc_labs[lvl]
            c = dict(CFG, PROJ='pca', S=0.3)
            u, x = lab(Q[qi:qi+1])
            pts = u[:, None, :] + DOORS[None]
            dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
            rk = np.argsort(np.argsort(dist, 1), 1)
            keys = pack(pts, c)
            bk = lc_buckets[lvl]
            sel = rk < 4
            for j in range(15):
                if sel[0, j]:
                    ids = bk.ids(int(keys[0, j]))
                    lc_c.update(ids.tolist())
        union_c = ivf_c | lc_c
        # rerank
        ids_list = list(union_c)
        if ids_list:
            dists = np.linalg.norm(X[ids_list] - Q[qi], axis=1)
            top = ids_list[int(np.argmin(dists))]
            if top == gt[qi]:
                rerank_hits += 1
        if gt[qi] in ivf_c: ivf_hits += 1
        if gt[qi] in lc_c: lc_hits += 1
        if gt[qi] in union_c: union_hits += 1
        lc_unique = len(lc_c - ivf_c)
        lc_unique_total += (1 if (gt[qi] in lc_c and gt[qi] not in ivf_c) else 0)
        overlap_total += len(lc_c & ivf_c)
        union_sizes.append(len(union_c))
        latencies.append((time.perf_counter() - t_start) * 1000)

    lat = np.array(latencies)
    result = {
        'N': N, 'L': L, 'nq': nq,
        'ivf_rec': ivf_hits/nq, 'lc_rec': lc_hits/nq, 'union_rec': union_hits/nq,
        'rerank_rec': rerank_hits/nq,
        'scan_pct': float(np.mean(union_sizes)/N*100),
        'ivf_scan': float(np.mean([len(set()) for _ in range(1)])/N*100),
        'lc_unique_hits': lc_unique_total,
        'overlap_mean': float(overlap_total/nq),
        'union_size_mean': float(np.mean(union_sizes)),
        'ivf_build_s': t_ivf_build, 'lc_build_s': t_lc_build,
        'p50_ms': float(np.percentile(lat, 50)),
        'p95_ms': float(np.percentile(lat, 95)),
        'p99_ms': float(np.percentile(lat, 99)),
        'lat_mean_ms': float(lat.mean()),
    }
    return result

# ---- Scale test ----
print("=" * 80)
print("SCALE TEST (L=4, nprobe=2)")
print("=" * 80)
scale_results = []
for N in [4000, 10000, 100000]:
    print(f"\n--- N={N} ---")
    t0 = time.perf_counter()
    r = run_one(N, 4, nq=100)
    dt = time.perf_counter() - t0
    scale_results.append(r)
    print(f"  ivf_rec={r['ivf_rec']:.3f} lc_rec={r['lc_rec']:.3f} union_rec={r['union_rec']:.3f} rerank={r['rerank_rec']:.3f}")
    print(f"  scan={r['scan_pct']:.2f}%  lc_unique_hits={r['lc_unique_hits']}  overlap_mean={r['overlap_mean']:.1f}  union_size={r['union_size_mean']:.1f}")
    print(f"  p50={r['p50_ms']:.2f}ms p95={r['p95_ms']:.2f}ms p99={r['p99_ms']:.2f}ms  build_ivf={r['ivf_build_s']:.1f}s build_lc={r['lc_build_s']:.1f}s  total={dt:.1f}s")

# Try 1M
print(f"\n--- N=1000000 (attempt) ---")
t0 = time.perf_counter()
try:
    r = run_one(1000000, 4, nq=50)
    dt = time.perf_counter() - t0
    scale_results.append(r)
    print(f"  ivf_rec={r['ivf_rec']:.3f} lc_rec={r['lc_rec']:.3f} union_rec={r['union_rec']:.3f} rerank={r['rerank_rec']:.3f}")
    print(f"  scan={r['scan_pct']:.2f}%  lc_unique_hits={r['lc_unique_hits']}  overlap_mean={r['overlap_mean']:.1f}")
    print(f"  p50={r['p50_ms']:.2f}ms p95={r['p95_ms']:.2f}ms  total={dt:.1f}s")
except Exception as e:
    print(f"  FAILED: {e}")

# ---- Level sweep ----
print("\n" + "=" * 80)
print("LEVEL SWEEP (N=10000, nprobe=2)")
print("=" * 80)
level_results = []
for L in [1, 2, 4, 8]:
    print(f"\n--- L={L} ---")
    t0 = time.perf_counter()
    r = run_one(10000, L, nq=100)
    dt = time.perf_counter() - t0
    level_results.append(r)
    print(f"  ivf_rec={r['ivf_rec']:.3f} lc_rec={r['lc_rec']:.3f} union_rec={r['union_rec']:.3f} rerank={r['rerank_rec']:.3f}")
    print(f"  scan={r['scan_pct']:.2f}%  lc_unique_hits={r['lc_unique_hits']}  overlap={r['overlap_mean']:.1f}  union_size={r['union_size_mean']:.1f}")
    print(f"  p50={r['p50_ms']:.2f}ms p95={r['p95_ms']:.2f}ms  build_lc={r['lc_build_s']:.1f}s  total={dt:.1f}s")

# ---- Summary ----
print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print("\nScale test (L=4):")
print(f"{'N':>8} {'ivf_rec':>8} {'lc_rec':>8} {'union_rec':>10} {'scan%':>7} {'LC_unique':>10} {'p50':>7} {'p95':>7}")
for r in scale_results:
    print(f"{r['N']:>8} {r['ivf_rec']:>8.3f} {r['lc_rec']:>8.3f} {r['union_rec']:>10.3f} {r['scan_pct']:>7.2f} {r['lc_unique_hits']:>10} {r['p50_ms']:>7.2f} {r['p95_ms']:>7.2f}")

print("\nLevel sweep (N=10000):")
print(f"{'L':>3} {'ivf_rec':>8} {'lc_rec':>8} {'union_rec':>10} {'scan%':>7} {'LC_unique':>10} {'overlap':>8} {'p50':>7} {'p95':>7}")
for r in level_results:
    print(f"{r['L']:>3} {r['ivf_rec']:>8.3f} {r['lc_rec']:>8.3f} {r['union_rec']:>10.3f} {r['scan_pct']:>7.2f} {r['lc_unique_hits']:>10} {r['overlap_mean']:>8.1f} {r['p50_ms']:>7.2f} {r['p95_ms']:>7.2f}")

print("\ndone")