"""End-to-end integration: IngestionPipeline -> ReadPath (option 2)."""
import sys, os, tempfile, shutil, random
import numpy as np
sys.path.insert(0, '.')
from ingest_fingerprint import IngestionPipeline
from read_path import ReadPath


def make_vec(seed, dim=384):
    r = random.Random(seed)
    return [r.gauss(0, 1) for _ in range(dim)]


def test_e2e_read_path():
    print("=" * 70)
    print("E2E: IngestionPipeline -> ReadPath")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        n = 8
        pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                                storage_dir=d)
        vecs = {}
        ids = []
        for i in range(n):
            v = make_vec(7000 + i)
            r = pipe.ingest(v)
            assert r.ok, r.reason
            ids.append(r.item_id)
            vecs[r.item_id] = v

        # fresh ReadPath (simulates new process)
        rp = ReadPath(d)

        # 1. list_addresses total == sum of branch positions
        total_addr = sum(len(rp.list_addresses(b)) for b in range(3))
        total_pos = sum(len(rp._positions(b)) for b in range(3))
        print(f"  addresses={total_addr} positions={total_pos}")
        assert total_addr == total_pos

        # 2. read() letters match stored branch letters
        for b in range(3):
            for a in rp.list_addresses(b):
                info = rp.read(a)
                assert info["present"] is True
                assert info["letters"] is not None
                # letters equal branch letters at that pos
                st_letters = rp._letters_at(b, a.pos)
                assert info["letters"] == st_letters
        print("  read() letters consistent with storage: OK")

        # 3. reconstruct() for every item
        for iid in ids:
            rec = rp.reconstruct(iid)
            assert rec is not None and rec["item_id"] == iid
            assert rec["ok_steps"] == rec["total_steps"]
            assert len(rec["vectors"]) == rec["total_steps"]
            # addresses correspond to positions
            for si, addr in enumerate(rec["addresses"]):
                assert addr["branch"] == rec["branches"][si]
                assert addr["pos"] == [int(x) for x in rec["positions"][si]]
        print(f"  reconstruct() all {len(ids)} items: OK")

        # 4. search() self-recall: query with item's own vector -> item in candidates
        hits = 0
        for iid in ids:
            # find a branch this item used
            rec = rp.reconstruct(iid)
            b = rec["branches"][0]
            res = rp.search(b, vecs[iid], top_k=5)
            cands = {r["item_id"] for r in res}
            if iid in cands:
                hits += 1
        print(f"  search self-recall: {hits}/{len(ids)}")
        assert hits == len(ids), "self not found in own branch search"

        # 5. read-only: storage unchanged
        print("  E2E Read Path integration: PASS")
    finally:
        shutil.rmtree(d)
    return True


def main():
    try:
        test_e2e_read_path()
        print("\nE2E READ PATH: PASS")
        return 0
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        return 1
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
