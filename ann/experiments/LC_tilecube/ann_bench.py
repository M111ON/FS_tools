#!/usr/bin/env python3
"""
ann_bench.py - LetterCube-LSH vs IVF vs random-scan on real vectors.

Usage (Windows):
  python ann_bench.py --list                      # list files in I:\\tools\\ann\\data + shapes
  python ann_bench.py                             # run on every usable file in the folder
  python ann_bench.py --file sift_base.fvecs      # run on one file
  python ann_bench.py --n 50000 --nq 500          # base size / number of queries
  python ann_bench.py --synthetic                 # sanity check, no data needed

Supported files: .npy (2D)  .fvecs  .bvecs  .csv/.tsv/.txt (numeric, optional 1 header line)

Metric (same as lsh_test.py): recall@1 = P(true nearest neighbour is inside candidate set)
                              scan%    = candidate-set size / n
A method is only interesting if recall is clearly ABOVE scan% (random-scan baseline: recall = scan%).

Methods
  rp3   : LetterCube-LSH, letters = equal-population bins of 3 random projections   (your lsh_test.py)
  pq3   : LetterCube-PQ,  letters = k-means codeword of 1 of 3 sub-vectors, codewords
          ordered along their 1st principal axis so that "slide +-1" = neighbouring codeword
  ivf   : plain IVF (k-means cells, probe nprobe nearest cells) - recall of the candidate set,
          i.e. BEFORE any PQ ranking, so it is comparable with the other rows
  each LetterCube method is shown "exact" (1 cell/level) and "slide6" (cell + 6 neighbours/level)
"""
import argparse, os, sys, time, csv
import numpy as np

EXTS = ('.npy', '.fvecs', '.bvecs', '.csv', '.tsv', '.txt')
DEFAULT_DIR = r'I:\tools\ann\data'


# ----------------------------------------------------------------- loading
def load(path, max_rows):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.npy':
        a = np.asarray(np.load(path, mmap_mode='r')[:max_rows])
    elif ext == '.fvecs':
        d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
        raw = np.fromfile(path, dtype=np.float32, count=max_rows * (d + 1))
        a = raw[: (len(raw) // (d + 1)) * (d + 1)].reshape(-1, d + 1)[:, 1:]
    elif ext == '.bvecs':
        d = int(np.fromfile(path, dtype=np.int32, count=1)[0])
        raw = np.fromfile(path, dtype=np.uint8, count=max_rows * (d + 4))
        a = raw[: (len(raw) // (d + 4)) * (d + 4)].reshape(-1, d + 4)[:, 4:]
    else:
        delim = ',' if ext == '.csv' else ('\t' if ext == '.tsv' else None)
        try:
            a = np.loadtxt(path, delimiter=delim, max_rows=max_rows, ndmin=2)
        except ValueError:
            a = np.loadtxt(path, delimiter=delim, max_rows=max_rows, ndmin=2, skiprows=1)
    a = np.asarray(a)
    if a.ndim != 2 or not np.issubdtype(a.dtype, np.number):
        raise ValueError('not a 2D numeric array')
    a = a.astype(np.float32)
    a = a[np.isfinite(a).all(1)]
    return np.ascontiguousarray(a)


def usable(a):
    return a.ndim == 2 and a.shape[0] >= 2000 and a.shape[1] >= 4


def synthetic(rng, n=20000, d=64, nc=50):
    C = rng.normal(size=(nc, d)) * 3
    return (C[rng.integers(nc, size=n)] + rng.normal(size=(n, d))).astype(np.float32)


# ----------------------------------------------------------------- helpers
def assign(X, C, chunk=4096):
    cc = (C ** 2).sum(1)
    out = np.empty(len(X), np.int64)
    for s in range(0, len(X), chunk):
        x = X[s:s + chunk]
        out[s:s + chunk] = (cc[None] - 2 * x @ C.T).argmin(1)
    return out


def kmeans(X, k, rng, iters=8, max_train=20000):
    if len(X) > max_train:
        X = X[rng.choice(len(X), max_train, replace=False)]
    k = min(k, len(X))
    C = X[rng.choice(len(X), k, replace=False)].copy()
    for _ in range(iters):
        a = assign(X, C)
        cnt = np.bincount(a, minlength=k)
        for j in range(X.shape[1]):
            C[:, j] = np.where(cnt > 0, np.bincount(a, weights=X[:, j], minlength=k) / np.maximum(cnt, 1), C[:, j])
        dead = np.where(cnt == 0)[0]
        if len(dead):
            C[dead] = X[rng.choice(len(X), len(dead), replace=False)]
    return C.astype(np.float32)


def brute_nn(Q, X, chunk=256):
    xx = (X ** 2).sum(1)
    return np.concatenate([(xx[None] - 2 * Q[s:s + chunk] @ X.T).argmin(1) for s in range(0, len(Q), chunk)])


# ----------------------------------------------------------------- letter encoders (one call = one level)
def enc_rp3(X, Q, A, rng):
    d = X.shape[1]
    W = rng.normal(size=(d, 3)).astype(np.float32)
    P, PQ = X @ W, Q @ W
    qs = np.quantile(P, np.linspace(0, 1, A + 1)[1:-1], axis=0)
    bx = np.stack([np.searchsorted(qs[:, i], P[:, i]) for i in range(3)], 1)
    qb = np.stack([np.searchsorted(qs[:, i], PQ[:, i]) for i in range(3)], 1)
    return bx, qb


def enc_pq3(X, Q, A, rng):
    d = X.shape[1]
    R = np.linalg.qr(rng.normal(size=(d, d)))[0].astype(np.float32)
    Xr, Qr = X @ R, Q @ R
    bx, qb = [], []
    for idx in np.array_split(np.arange(d), 3):
        sx, sq = Xr[:, idx], Qr[:, idx]
        C = kmeans(sx, A, rng)
        u = np.linalg.svd(C - C.mean(0), full_matrices=False)[2][0]      # 1st principal axis
        rank = np.argsort(np.argsort(C @ u))                              # codeword -> position on that axis
        bx.append(rank[assign(sx, C)])
        qb.append(rank[assign(sq, C)])
    return np.stack(bx, 1), np.stack(qb, 1)


def probe_keys(qb, A, slide):
    """returns (nq, 1 or 7) int array of cell keys, -1 = outside the grid"""
    base = qb[:, 0] * A * A + qb[:, 1] * A + qb[:, 2]
    if not slide:
        return base[:, None]
    cols = [base]
    for ax in range(3):
        for s in (-1, 1):
            q = qb.copy()
            q[:, ax] += s
            ok = (q[:, ax] >= 0) & (q[:, ax] < A)
            cols.append(np.where(ok, q[:, 0] * A * A + q[:, 1] * A + q[:, 2], -1))
    return np.stack(cols, 1)


def run_letters(X, Q, gt, enc, A, Ls, rng):
    n, nq = len(X), len(Q)
    cands = {0: [set() for _ in range(nq)], 1: [set() for _ in range(nq)]}
    rows = []
    for lvl in range(1, max(Ls) + 1):
        bx, qb = enc(X, Q, A, rng)
        key = bx[:, 0] * A * A + bx[:, 1] * A + bx[:, 2]
        order = np.argsort(key, kind='stable')
        sk = key[order]
        for slide in (0, 1):
            pk = probe_keys(qb, A, slide)
            lo = np.searchsorted(sk, pk, 'left')
            hi = np.searchsorted(sk, pk, 'right')
            cs = cands[slide]
            for qi in range(nq):
                for j in range(pk.shape[1]):
                    if pk[qi, j] >= 0 and hi[qi, j] > lo[qi, j]:
                        cs[qi].update(order[lo[qi, j]:hi[qi, j]].tolist())
        if lvl in Ls:
            for slide in (0, 1):
                cs = cands[slide]
                rec = float(np.mean([gt[i] in cs[i] for i in range(nq)]))
                scan = float(np.mean([len(c) for c in cs]) / n)
                rows.append(dict(A=A, key_space=A ** 3, mode='slide6' if slide else 'exact', L=lvl, rec=rec, scan=scan))
    return rows


def run_ivf(X, Q, gt, nlist, probes, rng):
    n = len(X)
    C = kmeans(X, nlist, rng, iters=10)
    cell = assign(X, C)
    sizes = np.bincount(cell, minlength=len(C))
    cq = (C ** 2).sum(1)[None] - 2 * Q @ C.T
    order = np.argsort(cq, 1)
    rank_of_cell = np.argsort(order, 1)
    gt_rank = rank_of_cell[np.arange(len(Q)), cell[gt]]
    rows = []
    for p in probes:
        scan = float(np.mean(sizes[order[:, :p]].sum(1)) / n)
        rows.append(dict(nlist=len(C), nprobe=p, rec=float(np.mean(gt_rank < p)), scan=scan))
    return rows


# ----------------------------------------------------------------- driver
def bench(name, data, args, rng, writer):
    n_all = len(data)
    nq = min(args.nq, n_all // 5)
    perm = rng.permutation(n_all)
    n = min(args.n, n_all - nq)
    X = data[perm[:n]]
    if args.noise > 0:
        src = rng.integers(n, size=nq)
        Q = X[src] + rng.normal(size=(nq, X.shape[1])).astype(np.float32) * args.noise * X.std()
    else:
        Q = data[perm[n:n + nq]]                         # held-out vectors = honest queries
    t = time.time()
    gt = brute_nn(Q, X)
    print(f'\n=== {name}  base n={n} dim={X.shape[1]} queries={nq}  (brute force {time.time() - t:.1f}s)')
    Ls = tuple(l for l in (1, 2, 4, 8, 16) if l <= args.maxL)
    out = []

    def show(method, params, tables, rec, scan):
        lift = rec / scan if scan > 0 else float('nan')
        print(f'{method:<5} {params:<22} {tables:>6} {rec:>8.3f} {scan * 100:>7.2f} {lift:>7.1f}')
        writer.writerow([name, n, X.shape[1], method, params, tables, f'{rec:.4f}', f'{scan * 100:.3f}', f'{lift:.2f}'])

    print(f'{"meth":<5} {"params":<22} {"tables":>6} {"recall@1":>8} {"scan%":>7} {"lift":>7}   (lift = recall/scan%, random scan = 1.0)')
    for mname, enc in (('rp3', enc_rp3), ('pq3', enc_pq3)):
        for A in args.A:
            t = time.time()
            for r in run_letters(X, Q, gt, enc, A, Ls, rng):
                show(mname, f'A={r["A"]} {r["mode"]}', r['L'], r['rec'], r['scan'])
            print(f'      ({mname} A={A} took {time.time() - t:.0f}s)', file=sys.stderr)
    nlist = max(16, int(args.nlist_mult * np.sqrt(n)))
    for r in run_ivf(X, Q, gt, nlist, (1, 2, 4, 8, 16, 32, 64), rng):
        if r['nprobe'] <= r['nlist']:
            show('ivf', f'nlist={r["nlist"]} nprobe={r["nprobe"]}', 1, r['rec'], r['scan'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=DEFAULT_DIR)
    ap.add_argument('--file', help='file name inside --dir (or full path)')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--n', type=int, default=50000, help='max base vectors')
    ap.add_argument('--nq', type=int, default=500)
    ap.add_argument('--A', type=int, nargs='+', default=[8, 24], help='letters per axis')
    ap.add_argument('--maxL', type=int, default=16, help='max levels/tables (1,2,4,8,16 reported)')
    ap.add_argument('--noise', type=float, default=0.0, help='>0: queries = base + noise*std (like lsh_test.py) instead of held-out')
    ap.add_argument('--nlist-mult', type=float, default=1.0, help='IVF nlist = mult*sqrt(n)')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out', default='ann_bench_results.csv')
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    if args.synthetic:
        items = [('synthetic', synthetic(rng, max(args.n, 20000)))]
    else:
        if args.file:
            paths = [args.file if os.path.isabs(args.file) else os.path.join(args.dir, args.file)]
        else:
            paths = [os.path.join(args.dir, f) for f in sorted(os.listdir(args.dir)) if f.lower().endswith(EXTS)]
        items = []
        for p in paths:
            try:
                a = load(p, args.n + args.nq + 1000)
            except Exception as e:
                print(f'skip {os.path.basename(p)}: {e}')
                continue
            ok = usable(a)
            print(f'{os.path.basename(p):<40} shape={a.shape}  {"OK" if ok else "skipped (need >=2000 rows, >=4 dims)"}')
            if ok:
                items.append((os.path.basename(p), a))
        if args.list:
            return
    if not items:
        print('no usable files')
        return
    with open(args.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['dataset', 'n', 'dim', 'method', 'params', 'tables', 'recall1', 'scan_pct', 'lift'])
        for name, data in items:
            bench(name, data, args, rng, w)
    print(f'\nsaved {args.out}')


if __name__ == '__main__':
    main()
