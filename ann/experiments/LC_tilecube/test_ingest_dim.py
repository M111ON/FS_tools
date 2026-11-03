"""Integration test: ingestion with per-Fruit (per-branch) embedding dims."""
import sys, os, tempfile, shutil, random
import numpy as np
sys.path.insert(0, '.')
from ingest_fingerprint import IngestionPipeline
from fruit_dim import FruitConfig

def make_vec(seed, dim=384):
    r = random.Random(seed)
    return [r.gauss(0, 1) for _ in range(dim)]

def main():
    # 4 branches with different dims
    cfgs = [
        FruitConfig(fruit_id=0, embedding_dim=384, projection_id="truncate"),
        FruitConfig(fruit_id=1, embedding_dim=128, projection_id="truncate"),
        FruitConfig(fruit_id=2, embedding_dim=768, projection_id="pad_zero"),
        FruitConfig(fruit_id=3, embedding_dim=64,  projection_id="random_ortho"),
    ]
    d = tempfile.mkdtemp()
    try:
        pipe = IngestionPipeline(branch_count=4, steps_per_item=3, domain_size=100,
                                storage_dir=d, fruit_configs=cfgs, common_dim=16)
        print("fruit_dims:", pipe.status()["fruit_dims"])
        assert pipe.status()["fruit_dims"] == [384, 128, 768, 64]
        # ingest several
        for i in range(6):
            r = pipe.ingest(make_vec(1000 + i))
            assert r.ok, r.reason
            # each step record carries fruit_id + fruit_dim
            for sr in r.step_records:
                if sr["ok"]:
                    assert "fruit_id" in sr and "fruit_dim" in sr
                    assert sr["fruit_dim"] in (384, 128, 768, 64)
        print("ingested 6 items OK")
        # relation records carry fruit dims
        recs = pipe.relations.load_all()
        print("relations:", len(recs))
        for rec in recs:
            assert "fruit_dims" in rec and "fruit_ids" in rec
            assert len(rec["fruit_dims"]) == rec["ok_steps"]
            assert all(d in (384, 128, 768, 64) for d in rec["fruit_dims"])
        print("relation fruit_dims OK")
        # fruit store has vectors in per-fruit dims
        fs = pipe.fruit_store
        print("fruit store dims:", fs.dims())
        assert fs.dims() == {0: 384, 1: 128, 2: 768, 3: 64}
        # check a stored vector shape
        sample_key = list(fs.fruits[2]["vectors"].keys())[0]
        v = fs.fruits[2]["vectors"][sample_key]
        print("fruit2 (768D) stored vec shape:", v.shape)
        assert v.shape == (768,)
        # persistence reload
        pipe2 = IngestionPipeline(branch_count=4, steps_per_item=3, domain_size=100,
                                 storage_dir=d, fruit_configs=cfgs, common_dim=16)
        assert pipe2.status()["fruit_dims"] == [384, 128, 768, 64]
        assert pipe2.fruit_store.dims() == {0: 384, 1: 128, 2: 768, 3: 64}
        print("reload OK, dims preserved")
        # geometry junctions preserved
        for i, b in enumerate(pipe2.branch_mgr.branches):
            ok, tot = b.check_junctions()
            print(f"  branch {i}: {ok}/{tot} junctions")
            assert tot == 0 or ok == tot
        # cross-fruit common space works
        cs = pipe2.common_space
        assert cs is not None
        # take one vector from fruit0 and fruit1, compare in common space
        k0 = list(pipe2.fruit_store.fruits[0]["vectors"].keys())[0]
        k1 = list(pipe2.fruit_store.fruits[1]["vectors"].keys())[0]
        v0 = pipe2.fruit_store.fruits[0]["vectors"][k0]
        v1 = pipe2.fruit_store.fruits[1]["vectors"][k1]
        d_common = cs.distance(cfgs[0], v0, cfgs[1], v1)
        print(f"common-space distance fruit0<->fruit1: {d_common:.4f}")
        assert np.isfinite(d_common)
        print("ALL PASS")
    finally:
        shutil.rmtree(d)

if __name__ == "__main__":
    main()
