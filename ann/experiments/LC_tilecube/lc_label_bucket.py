"""LC_tilecube v1 - two Lego layers.
label(V)  : vector -> integer cube-grid key (+1 grid bit). Grid A = integer points, grid B = shifted (1/2,1/2,1/2).
            Work in DOUBLED coords u = 2*q/S: A is all-even, B is all-odd -> BCC = two cube grids, parity bit picks one.
bucket    : key -> vector ids. Each bucket has 14 doors: 8 corners (+-1,+-1,+-1) to the other grid, 6 axes (+-2,0,0) same grid.
Run:  python lc_label_bucket.py          (self-test, needs ann_bench.py in same folder)
"""
import numpy as np, itertools, sys
sys.path.insert(0, '.')

# ---- config ---------------------------------------------------------------
CFG = dict(DIM=3, S=0.3, K=4, L=4, PROJ='rand', P=16, OFF=1 << 11, SH=12)
SCALES = (0.1, 0.15, 0.2, 0.3, 0.45, 0.7, 1.0, 1.5)
AXES = [tuple(2 * s * np.eye(3, dtype=int)[i]) for i in range(3) for s in (-1, 1)]
CORNERS = list(itertools.product((-1, 1), repeat=3))
DOORS = np.array([(0, 0, 0)] + AXES + CORNERS)          # row0 = own bucket, then 6 + 8 doors = 15 rows

def pack(u, c=CFG):                                     # 3 ints -> 1 int64 key (shifts only)
    o, s = c['OFF'], c['SH']
    return ((u[..., 0] + o) << (2 * s)) | ((u[..., 1] + o) << s) | (u[..., 2] + o)

# ---- layer 1: label --------------------------------------------------------
class Label:
    def __init__(self, X, rng, c=CFG):
        self.c = c
        mu = X.mean(0)
        if c['PROJ'] == 'pca':
            U = np.linalg.svd(X[:4000] - mu, full_matrices=False)[2][:c['P']].T
            W = U @ np.linalg.qr(rng.normal(size=(c['P'], c['P'])))[0][:, :3]
        else:
            W = rng.normal(size=(X.shape[1], 3))
        Z = (X - mu) @ W
        self.mu, self.W, self.sd = mu, W, Z.std(0)

    def __call__(self, V):                              # -> u (doubled lattice point), x (doubled query coord)
        x = 2 * ((V - self.mu) @ self.W / self.sd) / self.c['S']
        a = 2 * np.rint(x / 2); b = 2 * np.floor(x / 2) + 1
        pick = ((x - a) ** 2).sum(1) <= ((x - b) ** 2).sum(1)
        return np.where(pick[:, None], a, b).astype(np.int64), x

# ---- layer 2: bucket -------------------------------------------------------
class Bucket:
    def __init__(self, keys):
        self.o = np.argsort(keys, kind='stable'); self.sk = keys[self.o]
    def count(self, K):                                 # K: any-shape key array -> bucket sizes
        return np.searchsorted(self.sk, K, 'right') - np.searchsorted(self.sk, K, 'left')
    def ids(self, k):
        return self.o[np.searchsorted(self.sk, k, 'left'):np.searchsorted(self.sk, k, 'right')]

def probe(lab, Q, K):                                   # nearest K of the 15 (own + 14 doors) per query
    u, x = lab(Q)
    pts = u[:, None, :] + DOORS[None]
    dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
    sel = np.argsort(np.argsort(dist, 1, kind='stable'), 1) < K
    return pack(pts), sel

# ---- test ------------------------------------------------------------------
def evaluate(X, Q, gt, rng, c, Ks=(1, 4, 8, 15), Ls=(1, 4)):
    n, nq = len(X), len(Q); st = {}
    for lvl in range(1, max(Ls) + 1):
        lab = Label(X, rng, c)
        for S in SCALES:
            lab.c = dict(c, S=S)
            bk = Bucket(pack(lab(X)[0], lab.c)); gk = pack(lab(X[gt])[0], lab.c)
            keys, sel = probe(lab, Q, 15)
            cnt = bk.count(keys); hm = keys == gk[:, None]
            # rank by distance already in sel for K=15; recompute per K
            u, x = lab(Q); pts = u[:, None, :] + DOORS[None]
            dist = np.linalg.norm(x[:, None, :] - pts, axis=2); rk = np.argsort(np.argsort(dist, 1, kind='stable'), 1)
            for K in Ks:
                m = rk < K; h, w = st.setdefault((K, S), [np.zeros(nq, bool), np.zeros(nq)])
                h |= (m & hm).any(1); w += (cnt * m).sum(1)
        if lvl in Ls:
            for (K, S), (h, w) in st.items():
                yield lvl, K, S, h.mean(), w.mean() / n

def table(name, X, Q, gt, rng, c):
    res = list(evaluate(X, Q, gt, rng, c))
    print(f'\n== {name}  PROJ={c["PROJ"]}  best recall@1 under work budget (K = lookups out of 15)')
    for L in (1, 4):
        print(f' L={L} K   ' + '  '.join(f'<={b}%'.rjust(7) for b in (0.5, 1, 2, 5)))
        for K in (1, 4, 8, 15):
            row = []
            for b in (0.5, 1, 2, 5):
                v = [r for (l, k, s, r, w) in res if l == L and k == K and w * 100 <= b]
                row.append((f'{max(v):.3f}' if v else '-').rjust(7))
            print(f'      {K:<3} ' + '  '.join(row))

if __name__ == '__main__':
    from ann_bench import brute_nn
    rng = np.random.default_rng(5); r3 = np.random.default_rng(3)
    for name, gen in (('gauss3d', lambda n: r3.normal(size=(n, 3)) * np.array([1, .6, .3])),
                      ('mix3d', lambda n: r3.normal(size=(30, 3))[r3.integers(30, size=n)] * 2 + r3.normal(size=(n, 3)) * .5)):
        d = gen(32000).astype(np.float32); p = rng.permutation(len(d)); X, Q = d[p[:30000]], d[p[30000:32000]]
        table(name, X, Q, brute_nn(Q, X), rng, dict(CFG, PROJ='rand'))
    n, nq, D = 12000, 300, 128
    B = r3.normal(size=(12, D)); Z = (r3.normal(size=(60, 12)) * 3)[r3.integers(60, size=n + nq)] @ B / np.sqrt(12) * 3 + r3.normal(size=(n + nq, D)) * .7
    Z = Z.astype(np.float32); X, Q = Z[:n], Z[n:]; gt = brute_nn(Q, X)
    for pj in ('rand', 'pca'):
        table('lowrank128', X, Q, gt, rng, dict(CFG, PROJ=pj))
