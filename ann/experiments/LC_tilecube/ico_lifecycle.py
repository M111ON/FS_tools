"""Index lifecycle: growth, fragmentation/compaction, insert:query ratio.

Fixes over the first run:
  * ground truth excludes the query itself (the previous run let the query be
    its own neighbour, which put a free +0.1 floor on recall/NDCG at small N)
  * in the fragmentation section GT is computed among LIVE rows only
  * the insert-timing index is separate from the query index (inserting used to
    grow N mid-loop and index out of bounds)

Sections (select with argv: a / b / c / all):
  A GROWTH        truncation + NDCG as N grows at fixed level and budget
  B FRAGMENTATION tombstones waste the budget; compaction restores quality
  C INSERT:QUERY  steady-state cost with cost terms kept separate
"""
import os, sys, time
from collections import deque
import numpy as np
sys.path.insert(0, '.')
from ico_cost_quality import icosphere, ico_counts, vert_adj, ndcg, TOPK, pct

HERE = os.path.dirname(os.path.abspath(__file__))
SIFT_BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')

N_GRID = [10_000, 30_000, 100_000, 300_000, 1_000_000]
BUDGETS = [5.0, 10.0, 20.0]
LEVELS = [1, 2, 3, 4]
NQ = 50
TRUNC_LIMIT = 0.10


def _fvecs(p):
    size = os.path.getsize(p)
    with open(p, 'rb') as f:
        dim = int(np.frombuffer(f.read(4), dtype=np.int32)[0])
    row = dim + 1
    n = size // (4 * row)
    out = np.empty((n, dim), dtype=np.uint8)
    with open(p, 'rb') as f:
        for s in range(0, n, 100_000):
            e = min(s + 100_000, n)
            blk = np.fromfile(f, dtype=np.float32, count=(e - s) * row)
            out[s:e] = np.clip(blk.reshape(e - s, row)[:, 1:], 0, 255).astype(np.uint8)
    return out


class Ico:
    def __init__(self, X, level):
        self.X = X
        self.N = len(X)
        self.level = level
        t0 = time.perf_counter()
        self.v, self.f = icosphere(level)
        self.nv, self.ne, self.nf = ico_counts(level)
        self.adj = vert_adj(self.nv, self.f)
        self.topology_build = time.perf_counter() - t0
        self.vc = self.v.T.astype(np.float32)

        rng = np.random.default_rng(0)
        si = rng.choice(self.N, min(20000, self.N), replace=False)
        S = self.X[si].astype(np.float64)
        self.mu = S.mean(0).astype(np.float32)
        w, V = np.linalg.eigh(np.cov((S - self.mu).T))
        self.U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)
        self.cells = {}
        self.index_build = float('nan')
        self.rebuild()

    def proj(self, A):
        P = (A.astype(np.float32) - self.mu) @ self.U3
        return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None)

    def assign(self, A):
        P = self.proj(A)
        out = np.empty(len(A), dtype=np.int32)
        for s in range(0, len(A), 8000):
            e = min(s + 8000, len(A))
            out[s:e] = np.argmax(P[s:e] @ self.vc, axis=1)
        return out

    def rebuild(self):
        t0 = time.perf_counter()
        self.node = self.assign(self.X)
        self.buffers = [[] for _ in range(self.nv)]
        for nid in range(self.nv):
            idx = np.nonzero(self.node == nid)[0]
            if idx.size:
                self.buffers[nid] = [idx.astype(np.int32)]
        self.cells = {}
        self.live = np.ones(self.N, dtype=bool)
        self.index_build = time.perf_counter() - t0
        return self.index_build

    def insert(self, A):
        t0 = time.perf_counter()
        ids = self.assign(A)
        base = self.N
        for k, nid in enumerate(ids):
            self.buffers[nid].append(np.array([base + k], dtype=np.int32))
            self.cells[nid] = None
        self.N += len(A)
        self.live = np.concatenate([self.live, np.ones(len(A), dtype=bool)])
        return (time.perf_counter() - t0) / len(A) * 1e6

    def cell(self, nid):
        if self.cells.get(nid) is None:
            b = self.buffers[nid]
            self.cells[nid] = np.concatenate(b) if b else np.empty(0, np.int32)
        return self.cells[nid]

    def query(self, q, budget_pct, max_hops=10 ** 9):
        max_c = int(budget_pct / 100.0 * self.N)
        start = int(np.argmax(self.v @ self.proj(q.reshape(1, -1))[0]))
        a0 = self.cell(start)
        if len(a0) >= max_c:
            return a0[:max_c], {start}, True, 0
        parts = [a0]; seen = len(a0); vis = {start}; hops = 0
        qq = deque([start])
        while qq and seen < max_c and hops < max_hops:
            node = qq.popleft()
            for nb in self.adj[node]:
                if nb in vis:
                    continue
                vis.add(nb); qq.append(nb); hops += 1
                a = self.cell(nb)
                take = min(len(a), max_c - seen)
                if take:
                    parts.append(a[:take]); seen += take
                if seen >= max_c or hops >= max_hops:
                    break
            if seen >= max_c or hops >= max_hops:
                break
        return (np.concatenate(parts) if len(parts) > 1 else parts[0]), vis, False, hops


def l2_topk(X, q, idx, k):
    qf = q.astype(np.int32)
    bd, bi = [], []
    for s in range(0, len(idx), 50_000):
        sub = X[idx[s:s + 50_000]].astype(np.int32)
        d = np.einsum('ij,ij->i', sub - qf, sub - qf)
        kk = min(k, len(d))
        part = np.argpartition(d, kk - 1)[:kk]
        bd.append(d[part]); bi.append(idx[s:s + 50_000][part])
    d = np.concatenate(bd); i = np.concatenate(bi)
    return [int(x) for x in i[np.argsort(d)[:k]]]


def gt_set(X, q, exclude, allowed=None, k=TOPK):
    """Top-k by L2, excluding the query itself and (optionally) dead rows."""
    qf = q.astype(np.int32)
    bd, bi = [], []
    for s in range(0, len(X), 100_000):
        sub = X[s:s + 100_000].astype(np.int32)
        d = np.einsum('ij,ij->i', sub - qf, sub - qf)
        kk = min(max(k * 4, 40), len(d))
        part = np.argpartition(d, kk - 1)[:kk]
        bd.append(d[part]); bi.append(part + s)
    d = np.concatenate(bd); i = np.concatenate(bi)
    order = np.argsort(d)
    out = []
    for pos in order:
        idx = int(i[pos])
        if idx == exclude:
            continue
        if allowed is not None and not allowed[idx]:
            continue
        out.append(idx)
        if len(out) == k:
            break
    return set(out)


def section_a(X_all):
    print("=" * 104)
    print("A) GROWTH — truncation and NDCG as N grows (SIFT1M random subsets)")
    print("=" * 104)
    print(f"{'N':>9} {'lvl':>3} {'cells':>6} {'meanCell':>9} {'budget':>6} "
          f"{'trunc':>6} {'NDCG':>6} {'hops':>6} {'indexBuild_s':>12} {'verdict'}")
    rng = np.random.default_rng(5)
    for N in N_GRID:
        sub = X_all[rng.choice(len(X_all), size=N, replace=False)]
        qidx = rng.choice(N, size=NQ, replace=False)
        qs = sub[qidx]
        for level in LEVELS:
            ico = Ico(sub, level)
            sizes = [len(ico.cell(i)) for i in range(ico.nv)]
            mean_cell = int(np.mean(sizes))
            for budget in BUDGETS:
                truncs, ndcgs, hops_l = [], [], []
                for k, q in enumerate(qs):
                    cands, vis, tr, hops = ico.query(q, budget)
                    gt = gt_set(sub, q, int(qidx[k]))
                    ranked = l2_topk(sub, q, cands, TOPK)
                    truncs.append(1.0 if tr else 0.0)
                    ndcgs.append(ndcg(ranked, gt))
                    hops_l.append(hops)
                t = float(np.mean(truncs))
                print(f"{N:>9} {level:>3} {ico.nv:>6} {mean_cell:>9} {budget:>5.0f}% "
                      f"{t:>6.2f} {np.mean(ndcgs):>6.3f} {np.mean(hops_l):>6.1f} "
                      f"{ico.index_build:>12.2f} "
                      f"{'ok' if t <= TRUNC_LIMIT else 'TRUNC'}")
        del sub, qs, qidx


def section_b(X_all):
    print("\n" + "=" * 104)
    print("B) FRAGMENTATION / COMPACTION — tombstones waste the candidate budget")
    print("=" * 104)
    rng = np.random.default_rng(11)
    N, level = 100_000, 2
    sub = X_all[rng.choice(len(X_all), size=N, replace=False)]
    qidx = rng.choice(N, size=NQ, replace=False)
    qs = sub[qidx]
    print(f"  N={N} level={level}")
    print(f"  {'dead%':>6} {'budget':>6} {'cands':>7} {'deadInCands':>12} "
          f"{'liveCands':>10} {'NDCG':>6} {'NDCG_compact':>13} {'compact_s':>10}")
    for dead_frac in [0.0, 0.10, 0.30, 0.50]:
        ico = Ico(sub, level)
        live = np.ones(N, dtype=bool)
        if dead_frac > 0:
            dead = rng.choice(N, size=int(dead_frac * N), replace=False)
            live[dead] = False
        # GT among live only, computed once per dead fraction
        gts = [gt_set(sub, q, int(qidx[k]), allowed=live) for k, q in enumerate(qs)]
        if dead_frac > 0:
            t0 = time.perf_counter()
            keep = np.nonzero(live)[0]
            Xc = sub[keep]
            ico_c = Ico(Xc, level)
            compact_s = time.perf_counter() - t0
            # map query position into the compacted array
            kidx = {int(o): j for j, o in enumerate(keep)}
            qpos = [kidx[int(qidx[k])] for k in range(NQ)]
            gts_c = [gt_set(Xc, Xc[qpos[k]], qpos[k]) for k in range(NQ)]
        for budget in [5.0, 10.0]:
            nd_b, nd_a, deadc, livec, totc = [], [], [], [], []
            for k, q in enumerate(qs):
                cands, vis, tr, hops = ico.query(q, budget)
                lc = cands[live[cands]]
                ranked = l2_topk(sub, q, lc, TOPK)
                nd_b.append(ndcg(ranked, gts[k]))
                deadc.append(len(cands) - len(lc))
                livec.append(len(lc))
                totc.append(len(cands))
            if dead_frac > 0:
                for k in range(NQ):
                    cands, vis, tr, hops = ico_c.query(Xc[qpos[k]], budget)
                    ranked = l2_topk(Xc, Xc[qpos[k]], cands, TOPK)
                    nd_a.append(ndcg(ranked, gts_c[k]))
                cs = f"{compact_s:>10.2f}"
            else:
                nd_a = nd_b
                cs = f"{0.0:>10.2f}"
            print(f"  {dead_frac*100:>5.0f}% {budget:>5.0f}% {np.mean(totc):>7.0f} "
                  f"{np.mean(deadc):>12.0f} {np.mean(livec):>10.0f} "
                  f"{np.mean(nd_b):>6.3f} {np.mean(nd_a):>13.3f} {cs}")
        del ico


def section_c(X_all):
    print("\n" + "=" * 104)
    print("C) INSERT:QUERY RATIO — steady-state cost, cost terms kept separate")
    print("=" * 104)
    rng = np.random.default_rng(21)
    N = 100_000
    sub = X_all[rng.choice(len(X_all), size=N, replace=False)]
    qidx = rng.choice(N, size=NQ, replace=False)
    qs = sub[qidx]
    for level in [2, 3]:
        ico_q = Ico(sub, level)
        ico_i = Ico(sub[:20000], level)   # separate index, only for insert timing
        single = np.mean([ico_i.insert(sub[20001 + k:20002 + k]) for k in range(200)])
        bulk = ico_i.insert(sub[20500:21000])
        print(f"\n  level {level}: topology_build={ico_q.topology_build*1000:.1f} ms  "
              f"index_build={ico_q.index_build:.2f} s  cells={ico_q.nv}")
        print(f"  insert: single {single:.1f} us/vec   bulk(500) {bulk:.2f} us/vec")
        print(f"  {'budget':>6} {'query p50':>10} {'@0 ins':>8} {'@1':>8} "
              f"{'@10':>8} {'@100':>8} {'@1000':>9}")
        for budget in BUDGETS:
            tt = []
            for q in qs:
                t0 = time.perf_counter()
                cands, vis, tr, hops = ico_q.query(q, budget)
                l2_topk(sub, q, cands, TOPK)
                tt.append((time.perf_counter() - t0) * 1000)
            q_ms = pct(tt, 50)
            print(f"  {budget:>5.0f}% {q_ms:>10.1f} {q_ms:>8.1f} "
                  f"{q_ms+single/1000:>8.1f} {q_ms+10*single/1000:>8.1f} "
                  f"{q_ms+100*single/1000:>8.1f} {q_ms+1000*single/1000:>9.1f}")
        del ico_q, ico_i
    print("\n  columns = total ms/query when each query is accompanied by 0/1/10/100/1000 inserts")


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else 'all').lower()
    X_all = _fvecs(SIFT_BASE)
    if which in ('a', 'all'):
        section_a(X_all)
    if which in ('b', 'all'):
        section_b(X_all)
    if which in ('c', 'all'):
        section_c(X_all)
    print("\n  Scope: candidate retrieval cost/quality only.")
    print("\n  PASS")


if __name__ == '__main__':
    main()