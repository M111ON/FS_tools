"""Experiment runner for LC_tilecube (lc_label_bucket) across multiple datasets.
Reports recall@1 by dim, scan%, lift, and per-query latency.
"""
import os, sys, time, csv, numpy as np
sys.path.insert(0, '.')
from lc_label_bucket import Label, Bucket, probe, evaluate, SCALES, CFG
from ann_bench import load, usable, brute_nn, kmeans, assign

OUT = 'exp_results.csv'

def time_probe(lab, Q, nq):
    t0 = time.perf_counter()
    keys, sel = probe(lab, Q, 15)
    t_probe = time.perf_counter() - t0
    # bucket ids fetch for unique keys
    bk = None
    t1 = time.perf_counter()
    uniq = np.unique(keys.ravel())
    total_ids = 0
    for k in uniq[:2000]:  # cap to avoid huge
        pass
    t_ids = time.perf_counter() - t1
    return t_probe, t_ids, keys, sel

def run_dataset(name, X, Q, gt, rng, proj_list=('rand','pca')):
    n, nq = len(X), len(Q)
    rows = []
    for pj in proj_list:
        c = dict(CFG, PROJ=pj, DIM=X.shape[1])
        # timing representative: S=0.3, L=1
        lab = Label(X, rng, c)
        lab.c = dict(c, S=0.3)
        t0 = time.perf_counter()
        keys, sel = probe(lab, Q, 15)
        t_probe = (time.perf_counter()-t0)/nq*1000  # ms per query
        # bucket build
        t0 = time.perf_counter()
        bk = Bucket(np.unique(keys.ravel()))  # dummy-ish; real build below
        # real bucket build on base
        base_keys = probe(lab, X, 1)[0].ravel() if False else None
        # build bucket on X
        ux, _ = lab(X)
        base_packed = None
        # pack base keys
        import lc_label_bucket as LCB
        base_keys_arr = LCB.pack(ux, lab.c).ravel()
        t0 = time.perf_counter()
        bucket = Bucket(base_keys_arr)
        t_build = time.perf_counter()-t0
        # fetch ids timing for one query's 15 doors
        k0 = keys[0]
        t0 = time.perf_counter()
        for k in k0:
            bucket.ids(int(k))
        t_fetch = (time.perf_counter()-t0)/15*1000  # ms per bucket fetch
        # evaluate
        t0 = time.perf_counter()
        res = list(evaluate(X, Q, gt, rng, c))
        t_eval = time.perf_counter()-t0
        for (L, K, S, rec, scan) in res:
            rows.append(dict(dataset=name, dim=X.shape[1], n=n, nq=nq, proj=pj,
                            L=L, K=K, S=S, recall=rec, scan=scan,
                            t_probe_ms=t_probe, t_fetch_ms=t_fetch, t_eval_s=t_eval))
    return rows

def main():
    rng = np.random.default_rng(7)
    r3 = np.random.default_rng(3)
    items = []

    # 1. synthetic 3D
    for name, gen in (('syn_gauss3d', lambda n: r3.normal(size=(n,3))*np.array([1,.6,.3])),
                      ('syn_mix3d', lambda n: r3.normal(size=(30,3))[r3.integers(30,size=n)]*2 + r3.normal(size=(n,3))*.5)):
        d = gen(32000).astype(np.float32)
        p = rng.permutation(len(d)); X, Q = d[p[:30000]], d[p[30000:32000]]
        items.append((name, X, Q, None))

    # 2. synthetic lowrank 128
    n, nq, D = 12000, 300, 128
    B = r3.normal(size=(12, D))
    Z = (r3.normal(size=(60,12))*3)[r3.integers(60,size=n+nq)] @ B / np.sqrt(12)*3 + r3.normal(size=(n+nq,D))*.7
    Z = Z.astype(np.float32); X, Q = Z[:n], Z[n:]
    items.append(('syn_lowrank128', X, Q, None))

    # 3. random high-dim (no structure)
    R = r3.normal(size=(5000, 128)).astype(np.float32)
    Rq = r3.normal(size=(300, 128)).astype(np.float32)
    items.append(('syn_random128', R, Rq, None))

    # 4. siftsmall
    try:
        import struct
        def load_fvecs(p, maxr):
            d=int(np.fromfile(p,dtype=np.int32,count=1)[0])
            raw=np.fromfile(p,dtype=np.float32,count=maxr*(d+1))
            a=raw[:(len(raw)//(d+1))*(d+1)].reshape(-1,d+1)[:,1:]
            return a.astype(np.float32)
        Xs=load_fvecs('I:/tools/ann/data/sift/siftsmall/siftsmall_base.fvecs', 10000)
        Qs=load_fvecs('I:/tools/ann/data/sift/siftsmall/siftsmall_query.fvecs', 500)
        items.append(('siftsmall128', Xs[:8000], Qs[:300], None))
    except Exception as e:
        print('sift load fail', e)

    # 5. beir (pick a few domains)
    for dom in ('android','english','gaming'):
        try:
            Xb=np.load(f'I:/tools/ann/data/beir_embeddings/{dom}/corpus_embeddings.npy', mmap_mode='r')
            Qb=np.load(f'I:/tools/ann/data/beir_embeddings/{dom}/query_embeddings.npy', mmap_mode='r')
            Xb=np.asarray(Xb[:8000]).astype(np.float32)
            Qb=np.asarray(Qb[:300]).astype(np.float32)
            items.append((f'beir_{dom}_384', Xb, Qb, None))
        except Exception as e:
            print('beir load fail', dom, e)

    all_rows = []
    for name, X, Q, _ in items:
        if X is None: continue
        gt = brute_nn(Q, X)
        print(f'\n### {name}  n={len(X)} dim={X.shape[1]} nq={len(Q)}')
        # limit n for speed
        if len(X) > 12000:
            idx = rng.permutation(len(X))[:12000]
            X = X[idx]; gt = brute_nn(Q, X)
        projs = ('rand','pca') if X.shape[1] > 3 else ('rand',)
        rows = run_dataset(name, X, Q, gt, rng, projs)
        all_rows.extend(rows)
        # print compact: best recall per (proj, L, K) at scan<=2%
        for pj in projs:
            print(f'  PROJ={pj}')
            for L in (1,4):
                for K in (1,4,8,15):
                    vs=[r['recall'] for r in rows if r['proj']==pj and r['L']==L and r['K']==K and r['scan']*100<=2.0]
                    if vs:
                        print(f'    L={L} K={K:<2} scan<=2%: recall={max(vs):.3f} (S={ [r["S"] for r in rows if r["proj"]==pj and r["L"]==L and r["K"]==K and r["scan"]*100<=2.0 and r["recall"]==max(vs)][0] })')

    # write csv
    with open(OUT,'w',newline='') as f:
        w=csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader(); w.writerows(all_rows)
    print(f'\nsaved {OUT}  ({len(all_rows)} rows)')

if __name__=='__main__':
    main()