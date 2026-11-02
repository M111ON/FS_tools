import numpy as np, sys, time
from ann_bench import brute_nn, synthetic, run_ivf
rng=np.random.default_rng(11)
M=6; LV=(1,4,8); P=16
SC=[0.15,0.25,0.4,0.6,0.9,1.3,2.0,3.0]
HM=rng.integers(1,2**31,size=M+1).astype(np.int64)
def h(keys): return (keys*HM[:keys.shape[-1]]).sum(-1)

def permuto_verts(x):
    """x (n,d) -> verts (n,d+1,d+1) lattice points in elevated space (units of d+1), Adams et al."""
    n,d=x.shape
    sc=1/np.sqrt((np.arange(1,d+1))*(np.arange(1,d+1)+1))
    el=np.zeros((n,d+1)); sm=np.zeros(n)
    for i in range(d,0,-1):
        cf=x[:,i-1]*sc[i-1]; el[:,i]=sm-i*cf; sm+=cf
    el[:,0]=sm; el*= (d+1)*np.sqrt(2/3)
    v=el/(d+1); up=np.ceil(v)*(d+1); dn=np.floor(v)*(d+1)
    rem=np.where(up-el<el-dn,up,dn)
    s=(rem.sum(1)/(d+1)).astype(int)
    diff=el-rem
    rank=np.argsort(np.argsort(-diff,1,kind='stable'),1)
    rank=rank+s[:,None]
    lo=rank<0; hi=rank>d
    rank=np.where(lo,rank+d+1,np.where(hi,rank-d-1,rank))
    rem=np.where(lo,rem+d+1,np.where(hi,rem-d-1,rem))
    ks=np.arange(d+1)
    V=rem[:,None,:]+ks[None,:,None]
    V=np.where(rank[:,None,:]>d-ks[None,:,None],V-(d+1),V)
    dist=((el[:,None,:]-V)**2).sum(2)
    return V.astype(np.int64),dist

def level_points(X,Q):
    U=np.linalg.svd(X[:4000]-X.mean(0),full_matrices=False)[2][:P].T
    R=np.linalg.qr(rng.normal(size=(P,P)))[0][:,:M]
    W=U@R; Pz=(X-X.mean(0))@W; Pq=(Q-X.mean(0))@W
    sd=Pz.std(0); return Pz/sd,Pq/sd

def run(X,Q,gt,proj):
    n,nq=len(X),len(Q); st={}
    for lvl in range(1,max(LV)+1):
        if proj=='pca': Pz,Pq=level_points(X,Q)
        else:
            W=rng.normal(size=(X.shape[1],M)); Pz=X@W; Pq=Q@W; mu,sd=Pz.mean(0),Pz.std(0); Pz=(Pz-mu)/sd; Pq=(Pq-mu)/sd
        for s in SC:
            # permutohedral: store at nearest vertex, probe k nearest of the M+1 simplex vertices
            Vx,Dx=permuto_verts(Pz/s); kx=h(Vx[np.arange(n),Dx.argmin(1)])
            o=np.argsort(kx,kind='stable'); sk=kx[o]
            Vq,Dq=permuto_verts(Pq/s); rk=np.argsort(Dq,1)
            # cube: own + axis neighbours ranked by distance to face
            cx=np.floor(Pz/s).astype(np.int64); ck=h(cx); oc=np.argsort(ck,kind='stable'); sck=ck[oc]
            cq=np.floor(Pq/s).astype(np.int64); fr=Pq/s-cq
            nb=[(np.zeros(nq),np.zeros((nq,M),np.int64))]
            for a in range(M):
                for sg in (-1,1):
                    off=np.zeros((nq,M),np.int64); off[:,a]=sg
                    nb.append((fr[:,a] if sg<0 else 1-fr[:,a],off))
            cd=np.stack([d for d,_ in nb],1); co=np.stack([o_ for _,o_ in nb],1)
            cr=np.argsort(cd,1)
            for k in (1,3,5,7):
                for lat in ('permuto','cube'):
                    hit,wk,lk=st.setdefault((lat,k,s),[np.zeros(nq,bool),np.zeros(nq),0.0])
                    for qi in range(nq):
                        if lat=='permuto':
                            keys=[h(Vq[qi,j]) for j in rk[qi,:k]]; S,O=sk,o
                        else:
                            keys=[h(cq[qi]+co[qi,j]) for j in cr[qi,:k]]; S,O=sck,oc
                        idx=[]
                        for kk in keys:
                            a=np.searchsorted(S,kk,'left'); b=np.searchsorted(S,kk,'right')
                            if b>a: idx.append(O[a:b])
                        if idx:
                            idx=np.concatenate(idx); wk[qi]+=len(idx); hit[qi]|=(idx==gt[qi]).any()
        if lvl in LV:
            pass
        if lvl in LV:
            st_snap=({c:(v[0].mean(),v[1].mean()/n) for c,v in st.items()})
            yield lvl,st_snap

def report(name,X,Q,gt,proj):
    print(f'\n== {name} proj={proj} n={len(X)} m={M} (best recall@1 under work budget; lookups k = M+1 max for permuto)')
    for lvl,sn in run(X,Q,gt,proj):
        print(f' L={lvl}  '+'  '.join(f'<={b}%'.rjust(7) for b in (0.5,1,2,5)))
        for lat in ('permuto','cube'):
            for k in (1,3,5,7):
                row=[]
                for b in (0.5,1,2,5):
                    v=[r for (l,kk,s),(r,w) in sn.items() if l==lat and kk==k and w*100<=b]
                    row.append((f'{max(v):.3f}' if v else '-').rjust(7))
                print(f'  {lat:<7} k={k} '+'  '.join(row))

n,nq,d=12000,300,128
r=np.random.default_rng(3)
C=r.normal(size=(60,d))*3; D=(C[r.integers(60,size=n+nq+500)]+r.normal(size=(n+nq+500,d))).astype(np.float32)
# low-rank structure variant: clusters live in a 12-d subspace plus noise
B=r.normal(size=(12,d)); D2=((r.normal(size=(60,12))*3)[r.integers(60,size=n+nq+500)]@B/np.sqrt(12)*3+r.normal(size=(n+nq+500,d))*0.7).astype(np.float32)
for name,Dd in (('iso128',D),('lowrank128',D2)):
    X=Dd[:n];Q=Dd[n:n+nq];gt=brute_nn(Q,X)
    print(name,'IVF ref:',[(x['nprobe'],round(x['rec'],3),round(x['scan']*100,1)) for x in run_ivf(X,Q,gt,110,(1,4,8),np.random.default_rng(1))])
    for proj in ('rand','pca'):
        t=time.time(); report(name,X,Q,gt,proj); print('  time',round(time.time()-t))
