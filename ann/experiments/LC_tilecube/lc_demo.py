"""LC_tilecube standalone demo — exercises the public API end to end."""
import sys, os, tempfile, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import LC_tilecube as lc

def main():
    print("LC_tilecube demo — version", lc.__version__)

    # 1. geometry
    v, f = lc.icosphere(2)
    print(f"[1] icosphere L2: {v.shape[0]} vertices, {f.shape[0]} faces")

    # 2. ingest + read path
    d = tempfile.mkdtemp()
    pipe = lc.IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                               storage_dir=d)
    def mv(s, dim=384):
        r = random.Random(s)
        return [r.gauss(0, 1) for _ in range(dim)]
    for i in range(5):
        pipe.ingest(mv(1000 + i))
    rp = lc.ReadPath(d)
    print(f"[2] branches: {rp._branch_count()}, branch0 addrs: {len(rp.list_addresses(0))}")
    res = rp.search(0, mv(1000), top_k=3)
    print(f"    search branch0: {len(res)} candidates"
          + (f", top={res[0]['item_id']}" if res else ""))

    # 3. rerank CPU
    X = np.random.default_rng(0).standard_normal((500, 8)).astype('float32')
    P = X[:5]; V = X[5:]
    idx, dist = lc.rerank_topk(P, V, k=3)
    print(f"[3] rerank_topk: idx shape {idx.shape}")

    # 4. rerank GPU (resident)
    rr = lc.ResidentReranker(V.astype('float32'))
    i = rr.query(P.astype('float32'))
    print(f"[4] ResidentReranker.query: {i.shape} indices")

    print("demo OK")

if __name__ == "__main__":
    main()
