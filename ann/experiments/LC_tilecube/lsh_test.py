import numpy as np, itertools, math, time
rng=np.random.default_rng(7)
def make(n=20000,d=64,nc=50):
    C=rng.normal(size=(nc,d))*3
    X=C[rng.integers(nc,size=n)]+rng.normal(size=(n,d))
    return X.astype(np.float32)
def run(X,Q,gt,A,L,probe):
    n,d=X.shape;nq=len(Q);cands=[set() for _ in range(nq)]
    for _ in range(L):
        W=rng.normal(size=(d,3)).astype(np.float32)
        P=X@W; qs=np.quantile(P,np.linspace(0,1,A+1)[1:-1],axis=0)   # equal-population bins per axis (letter)
        bx=np.stack([np.searchsorted(qs[:,i],P[:,i]) for i in range(3)],1)
        qb=np.stack([np.searchsorted(qs[:,i],(Q@W)[:,i]) for i in range(3)],1)
        tab={}
        for i,k in enumerate(map(tuple,bx)): tab.setdefault(k,[]).append(i)
        for qi,k in enumerate(map(tuple,qb)):
            keys=[k]
            if probe:   # slide ±1 on each axis = 6 neighbours
                for ax in range(3):
                    for s in(-1,1):
                        kk=list(k);kk[ax]+=s;keys.append(tuple(kk))
            for kk in keys: cands[qi].update(tab.get(kk,()))
    rec=np.mean([gt[i] in cands[i] for i in range(nq)])
    scan=np.mean([len(c) for c in cands])/n
    return rec,scan
X=make();nq=500;src=rng.integers(len(X),size=nq)
Q=X[src]+rng.normal(size=(nq,X.shape[1])).astype(np.float32)*0.8
D=((Q**2).sum(1)[:,None]-2*Q@X.T+(X**2).sum(1)[None]);gt=D.argmin(1)
print('n',len(X),'dim',X.shape[1],'queries',nq)
print('A(bins/axis)  key space  probe  L  recall@1  scan%  (random-scan baseline recall = scan%)')
for A in(8,24):
    for probe in(0,1):
        for L in(1,2,4,8,16):
            r,s=run(X,Q,gt,A,L,probe)
            print(f'{A:>3} {A**3:>7} {"slide6" if probe else "exact ":>6} {L:>2}  {r:.3f}   {s*100:5.1f}')
# exact-address test: independent random shuffles per level, intersect candidates
print('\naddress test (random letters, exact match): mean candidates after m levels')
for n,K,name in((4096,512,'8^3'),(4096,12144,'24*23*22'),(115**3,12144,'24*23*22')):
    res=[]
    base=np.arange(n)
    keys=[rng.integers(K,size=n) for _ in range(4)]
    for m in(1,2,3,4):
        sizes=[]
        for t in rng.integers(n,size=300):
            mask=np.ones(n,bool)
            for l in range(m): mask&=keys[l]==keys[l][t]
            sizes.append(mask.sum())
        res.append((m,round(np.mean(sizes),2),round(1+(n-1)/K**m,3)))
    print(n,name,'(m, measured, predicted):',res)
