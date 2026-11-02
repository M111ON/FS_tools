"""LC_tilecube v2 - adds per-level PCA subset + adaptive K probe.
Variant of lc_label_bucket.py for staged experiments. Original untouched.
"""
import numpy as np, itertools, sys
sys.path.insert(0, '.')
from lc_label_bucket import (SCALES, AXES, CORNERS, DOORS, pack, Bucket, evaluate as _eval_base)

CFG = dict(DIM=3, S=0.3, K=4, L=4, PROJ='pca', P=16, OFF=1 << 11, SH=12,
           PC_START=0, ADAPTIVE=False)

class Label:
    def __init__(self, X, rng, c=CFG):
        self.c = c
        mu = X.mean(0)
        pc_start = c.get('PC_START', 0)
        if c['PROJ'] == 'pca':
            U = np.linalg.svd(X[:4000] - mu, full_matrices=False)[2]
            sl = slice(pc_start, pc_start + c['P'])
            U = U[sl].T
            W = U @ np.linalg.qr(rng.normal(size=(U.shape[1], U.shape[1])))[0][:, :3]
        else:
            W = rng.normal(size=(X.shape[1], 3))
        Z = (X - mu) @ W
        self.mu, self.W, self.sd = mu, W, Z.std(0)

    def __call__(self, V):
        x = 2 * ((V - self.mu) @ self.W / self.sd) / self.c['S']
        a = 2 * np.rint(x / 2); b = 2 * np.floor(x / 2) + 1
        pick = ((x - a) ** 2).sum(1) <= ((x - b) ** 2).sum(1)
        return np.where(pick[:, None], a, b).astype(np.int64), x

def probe(lab, Q, K):
    u, x = lab(Q)
    pts = u[:, None, :] + DOORS[None]
    dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
    if lab.c.get('ADAPTIVE', False):
        # adaptive: include doors within 1.5x nearest door distance
        dmin = dist.min(1, keepdims=True)
        thr = dmin * 1.5 + 1e-9
        sel = dist <= thr
        # cap at least 1, at most 15
        # ensure at least K-ish by including top-K always
        rk = np.argsort(np.argsort(dist, 1, kind='stable'), 1)
        sel = sel | (rk < max(1, K))
    else:
        rk = np.argsort(np.argsort(dist, 1, kind='stable'), 1)
        sel = rk < K
    return pack(pts), sel

# adaptive probe returns variable doors; evaluate needs fixed K. Provide a simpler evaluate for fixed K only.
def evaluate(X, Q, gt, rng, c, Ks=(1,4,8,15), Ls=(1,4)):
    n, nq = len(X), len(Q); st = {}
    for lvl in range(1, max(Ls)+1):
        cc = dict(c, PC_START=(lvl-1)*c.get('P',16))
        lab = Label(X, rng, cc)
        for S in SCALES:
            lab.c = dict(cc, S=S)
            bk = Bucket(pack(lab(X)[0], lab.c))
            gk = pack(lab(X[gt])[0], lab.c)
            # probe with K=15 to get all doors ranked
            u, x = lab(Q); pts = u[:, None, :] + DOORS[None]
            dist = np.linalg.norm(x[:, None, :] - pts, axis=2)
            rk = np.argsort(np.argsort(dist, 1, kind='stable'), 1)
            keys_all = pack(pts, lab.c)
            cnt = bk.count(keys_all)
            hm = keys_all == gk[:, None]
            for K in Ks:
                m = rk < K
                h, w = st.setdefault((K, S), [np.zeros(nq, bool), np.zeros(nq)])
                h |= (m & hm).any(1); w += (cnt * m).sum(1)
        if lvl in Ls:
            for (K, S), (h, w) in st.items():
                yield lvl, K, S, h.mean(), w.mean() / n