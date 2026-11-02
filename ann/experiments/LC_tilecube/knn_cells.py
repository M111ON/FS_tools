import numpy as np, sys, itertools, time
sys.path.insert(0,'.')
from ann_bench import brute_nn
rng=np.random.default_rng(5)
M=4096; O=2048
def key(u): return ((u[...,0]+O)*M+(u[...,1]+O))*M+(u[...,2]+O)
MOORE=np.array([(0,0,0)]+[o for o in itertools.product((-1,0,1),repeat=3) if any(o)])   # row0 = own cell
AX=[o for o in map(tuple,MOORE) if sum(map(abs,o))==1]
col={'exact':[0],'slide6':[0]+[i for i,o in enumerate(map(tuple,MOORE)) if sum(map(abs,o))==1],
     'cuboct19':[0]+[i for i,o in enumerate(map(tuple,MOORE)) if 0<sum(map(abs,o))<=2],'moore26':list(range(27))}
BO=np.array([(0,0,0)]+[tuple(2*np.array(e)) for e in itertools.product((-1,1),repeat=1) for e in []]+[],dtype=int) # placeholder
AX2=[tuple(v) for i in range(3) for s in(-1,1) for v in [2*s*np.eye(3,dtype=int)[i]]]
CORN=list(itertools.product((-1,1),repeat=3))
BOFF=np.array([(0,0,0)]+AX2+CORN)       # 15 rows
SG=[0.04,0.06,0.08,0.11,0.15,0.2,0.27,0.36,0.5,0.7]
RHO=[0.05,0.1,0.15,0.2,0.3,0.45,0.7,1.2]
def bquant(q):
    a=2*np.rint(q); b=2*np.floor(q)+1
    return np.where((((2*q-a)**2).sum(1)<=((2*q-b)**2).sum(1))[:,None],a,b).astype(np.int64)
CK=(1,4)
KS=(1,2,3,4,6,8)
def run(X,Q,gt):
    n,nq=len(X),len(Q); st={}; out=[]
    def acc(cfg,mask,cnt,hm):
        h,w=st.setdefault(cfg,[np.zeros(nq,bool),np.zeros(nq)])
        h|=(mask&hm).any(1); w+=(cnt*mask).sum(1)
    def topk(d,k):
        r=np.argsort(np.argsort(d,axis=1,kind='stable'),axis=1); return r<k
    for lvl in range(1,max(CK)+1):
        W=rng.normal(size=(X.shape[1],3)); P=X@W; PQ=Q@W; mu,sd=P.mean(0),P.std(0); P=(P-mu)/sd; PQ=(PQ-mu)/sd
        for s in SG:
            c=np.floor(PQ/s).astype(np.int64); q=PQ/s; kx=key(np.floor(P/s).astype(np.int64))
            o=np.argsort(kx,kind='stable'); sk=kx[o]
            K=key(c[:,None,:]+MOORE[None]); cnt=np.searchsorted(sk,K,'right')-np.searchsorted(sk,K,'left'); hm=K==kx[gt][:,None]
            dist=np.linalg.norm(q[:,None,:]-(c[:,None,:]+MOORE[None]+0.5),axis=2)
            for k in KS: acc(('cube',k,s),topk(dist,k),cnt,hm)
            u=bquant(q); kx=key(bquant(P/s)); o=np.argsort(kx,kind='stable'); sk=kx[o]
            K=key(u[:,None,:]+BOFF[None]); cnt=np.searchsorted(sk,K,'right')-np.searchsorted(sk,K,'left'); hm=K==kx[gt][:,None]
            dist=np.linalg.norm(q[:,None,:]-(u[:,None,:]+BOFF[None])/2.,axis=2)
            for k in KS: acc(('bcc',k,s),topk(dist,k),cnt,hm)
        if lvl in CK:
            for cfg,(h,w) in st.items(): out.append((cfg,lvl,h.mean(),w.mean()/n))
    return out
r3=np.random.default_rng(3)
for name,gen in (('gauss3d',lambda n:r3.normal(size=(n,3))*np.array([1,.6,.3])),('mix3d',lambda n:r3.normal(size=(30,3))[r3.integers(30,size=n)]*2+r3.normal(size=(n,3))*.5)):
    d=gen(32000).astype(np.float32); perm=rng.permutation(len(d)); n=30000; nq=2000
    X=d[perm[:n]]; Q=d[perm[n:n+nq]]; gt=brute_nn(Q,X); res=run(X,Q,gt)
    print(f'\n== {name} d=3 nq={nq}: probe the k nearest lattice cells; best recall@1 with work<=budget')
    for L in (1,4):
        print(f' L={L}   lookups k   '+'   '.join(f'work<={b}%'.rjust(10) for b in (0.5,1,2,4)))
        for lat in ('cube','bcc'):
            for k in KS:
                row=[]
                for b in (0.5,1,2,4):
                    v=[x[2] for x in res if x[1]==L and x[0][0]==lat and x[0][1]==k and x[3]*100<=b]
                    row.append((f'{max(v):.3f}' if v else '-').rjust(10))
                print(f'  {lat:<5}        {k}      '+'   '.join(row))
