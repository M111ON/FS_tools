import numpy as np, itertools
from ann_bench import brute_nn
rng=np.random.default_rng(5); M=4096; O=2048
def key(u): return ((u[...,0]+O)*M+(u[...,1]+O))*M+(u[...,2]+O)
MOORE=np.array([o for o in itertools.product((-1,0,1),repeat=3)])
MOORE=np.vstack([[0,0,0],[o for o in MOORE if any(o)]])
AX2=[tuple(2*s*np.eye(3,dtype=int)[i]) for i in range(3) for s in(-1,1)]
BOFF=np.array([(0,0,0)]+AX2+list(itertools.product((-1,1),repeat=3)))
FOFF=np.array([(0,0,0)]+[o for o in itertools.product((-1,0,1),repeat=3) if sorted(map(abs,o))==[0,1,1]]+AX2)  # 1+12+6
SG=[0.04,0.06,0.08,0.11,0.15,0.2,0.27,0.36,0.5,0.7,1.0]
KS=(1,2,3,4,6,8); CK=(1,4)
def bq(q):
    a=2*np.rint(q); b=2*np.floor(q)+1
    return np.where((((2*q-a)**2).sum(1)<=((2*q-b)**2).sum(1))[:,None],a,b).astype(np.int64)
def dq(q):  # closest point of D3
    r=np.rint(q); odd=(r.sum(1)%2!=0); e=q-r; i=np.abs(e).argmax(1)
    for j in np.where(odd)[0]: r[j,i[j]]+=np.sign(e[j,i[j]]) if e[j,i[j]]!=0 else 1
    return r.astype(np.int64)
def topk(d,k): return np.argsort(np.argsort(d,1,kind='stable'),1)<k
def run(X,Q,gt):
    n,nq=len(X),len(Q); st={}; out=[]
    for lvl in range(1,max(CK)+1):
        W=rng.normal(size=(3,3)); P=X@W; PQ=Q@W; mu,sd=P.mean(0),P.std(0); P=(P-mu)/sd; PQ=(PQ-mu)/sd
        for s in SG:
            q=PQ/s
            for name,quant,off,cen in (('cube',lambda a:np.floor(a).astype(np.int64),MOORE,.5),('bcc',bq,BOFF,0),('fcc',dq,FOFF,0)):
                u=quant(q); kx=key(quant(P/s)); o=np.argsort(kx,kind='stable'); sk=kx[o]
                K=key(u[:,None,:]+off[None]); cnt=np.searchsorted(sk,K,'right')-np.searchsorted(sk,K,'left'); hm=K==kx[gt][:,None]
                pos=(u[:,None,:]+off[None]+cen) if name=='cube' else (u[:,None,:]+off[None])/(2. if name=='bcc' else 1.)
                dist=np.linalg.norm(q[:,None,:]-pos,axis=2)
                for k in KS:
                    m=topk(dist,k); h,w=st.setdefault((name,k,s),[np.zeros(nq,bool),np.zeros(nq)]); h|=(m&hm).any(1); w+=(cnt*m).sum(1)
        if lvl in CK:
            for c,(h,w) in st.items(): out.append((c,lvl,h.mean(),w.mean()/n))
    return out
r3=np.random.default_rng(3)
for name,gen in (('gauss3d',lambda n:r3.normal(size=(n,3))*np.array([1,.6,.3])),('mix3d',lambda n:r3.normal(size=(30,3))[r3.integers(30,size=n)]*2+r3.normal(size=(n,3))*.5)):
    d=gen(32000).astype(np.float32); perm=rng.permutation(len(d)); n=30000; nq=2000
    X=d[perm[:n]]; Q=d[perm[n:n+nq]]; gt=brute_nn(Q,X); res=run(X,Q,gt)
    print(f'\n== {name} nq={nq}: best recall@1 with work<=budget, k nearest lattice points')
    for L in (1,4):
        print(f' L={L}  k  '+'  '.join(f'<={b}%'.rjust(7) for b in (0.5,1,2,4)))
        for lat in ('cube','bcc','fcc'):
            for k in (1,2,4,8):
                row=[]
                for b in (0.5,1,2,4):
                    v=[x[2] for x in res if x[1]==L and x[0][0]==lat and x[0][1]==k and x[3]*100<=b]
                    row.append((f'{max(v):.3f}' if v else '-').rjust(7))
                print(f'  {lat:<5} {k}  '+'  '.join(row))
