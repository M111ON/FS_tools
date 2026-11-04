"""Tests for LC_tilecube Read Path (read-only layer).

Covers: read(address), search(branch, query), reconstruct(item_id),
list_addresses(branch), plus fresh-process exact equality.
"""
import sys, os, tempfile, shutil, random, subprocess, json
import numpy as np
sys.path.insert(0, '.')
from read_path import ReadPath, Address
from ingest_fingerprint import IngestionPipeline


def make_vec(seed=0, dim=384):
    r = random.Random(seed)
    return [r.gauss(0, 1) for _ in range(dim)]


def build_store(d, n=6):
    pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                            storage_dir=d)
    for i in range(n):
        pipe.ingest(make_vec(2000 + i))
    return pipe


def test_list_addresses():
    print("=" * 70)
    print("TEST 1: list_addresses(branch)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        build_store(d, 4)
        rp = ReadPath(d)
        total = 0
        for b in range(3):
            addrs = rp.list_addresses(b)
            print(f"  branch {b}: {len(addrs)} addresses")
            total += len(addrs)
            for a in addrs[:2]:
                assert isinstance(a, Address)
            # sorted/storage order preserved: positions match branch positions
            pos_list = [a.pos for a in addrs]
            assert pos_list == [p for p in (tuple(x) for x in rp._positions(b))]
        print(f"  total addresses: {total}")
        assert total > 0
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_read():
    print("=" * 70)
    print("TEST 2: read(address)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        build_store(d, 5)
        rp = ReadPath(d)
        addrs = rp.list_addresses(0)
        assert addrs, "no addresses in branch 0"
        a = addrs[0]
        info = rp.read(a)
        print(f"  read {a}: present={info['present']}, letters={info['letters'] is not None}, "
              f"items={info['item_ids']}")
        assert info is not None
        assert info["branch"] == 0
        assert info["pos"] == list(a.pos)
        assert info["present"] is True
        # letters may be None only if missing; here should exist
        assert info["letters"] is not None, "letters missing at stored address"
        # read by index
        info2 = rp.read((0, 0))
        assert info2["pos"] == info["pos"]
        # read by dict
        info3 = rp.read({"branch": 0, "pos": list(a.pos)})
        assert info3["pos"] == info["pos"]
        # read absent address -> present False
        absent = rp.read((0, (999, 999, 999)))
        assert absent is not None and absent["present"] is False
        print("  read variants (Address/tuple/dict) consistent: OK")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_reconstruct():
    print("=" * 70)
    print("TEST 3: reconstruct(item_id)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        pipe = build_store(d, 4)
        rp = ReadPath(d)
        iid = pipe.relations.load_all()[0]["item_id"] if hasattr(pipe, "relations") else None
        # fallback: read relations from disk
        recs = rp.relations
        assert recs
        iid = recs[0]["item_id"]
        rec = rp.reconstruct(iid)
        print(f"  item={rec['item_id']}, steps={rec['ok_steps']}/{rec['total_steps']}, "
              f"vecs={len(rec['vectors'])}")
        assert rec is not None
        assert rec["item_id"] == iid
        assert rec["ok_steps"] == rec["total_steps"] or rec["ok_steps"] <= rec["total_steps"]
        assert len(rec["branches"]) == rec["total_steps"]
        assert len(rec["positions"]) == rec["total_steps"]
        assert len(rec["addresses"]) == rec["total_steps"]
        # vectors keys are step indices
        for k in rec["vectors"]:
            assert isinstance(rec["vectors"][k], list)
        # unknown item
        assert rp.reconstruct("does_not_exist_0000") is None
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_search():
    print("=" * 70)
    print("TEST 4: search(branch, query)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        build_store(d, 8)
        rp = ReadPath(d)
        # vector query
        q = make_vec(2000)
        res = rp.search(0, q, top_k=3)
        print(f"  vector search branch0: {len(res)} candidates")
        if res:
            print(f"  top: {res[0]['key']} score={res[0]['score']:.4f}")
            scores = [r["score"] for r in res]
            assert scores == sorted(scores, reverse=True), "not sorted desc"
        # item_id query
        iid = rp.relations[0]["item_id"]
        res2 = rp.search(0, iid, top_k=3)
        print(f"  item_id search branch0: {len(res2)} candidates")
        # search returns list of dicts with required keys
        for r in res + res2:
            assert set(r.keys()) >= {"key", "item_id", "step", "score"}
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_fresh_process_exact_equality():
    print("=" * 70)
    print("TEST 5: Fresh-process exact equality")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        build_store(d, 5)
        # process A: capture a canonical dump via ReadPath
        rp1 = ReadPath(d)
        addrs0 = [a.to_dict() for a in rp1.list_addresses(0)]
        read0 = rp1.read(rp1.list_addresses(0)[0]) if rp1.list_addresses(0) else None
        rec0 = rp1.reconstruct(rp1.relations[0]["item_id"])
        search0 = rp1.search(0, make_vec(2000), top_k=3)

        # simulate fresh process: new ReadPath (reloads from disk)
        rp2 = ReadPath(d)
        addrs1 = [a.to_dict() for a in rp2.list_addresses(0)]
        read1 = rp2.read(rp2.list_addresses(0)[0]) if rp2.list_addresses(0) else None
        rec1 = rp2.reconstruct(rp2.relations[0]["item_id"])
        search1 = rp2.search(0, make_vec(2000), top_k=3)

        assert addrs0 == addrs1, "list_addresses differs across reload"
        assert read0 == read1, "read differs across reload"
        assert rec0 == rec1, "reconstruct differs across reload"
        assert search0 == search1, "search differs across reload"
        print("  in-process reload equality: OK")

        # true subprocess equality: run a helper that prints canonical JSON
        helper = os.path.join(os.path.dirname(__file__), "_rp_dump.py")
        with open(helper, "w", encoding="utf-8") as f:
            f.write(
                "import sys, json, os\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                "from read_path import ReadPath\n"
                "import random\n"
                "def mv(s,dim=384):\n"
                "    r=random.Random(s); return [r.gauss(0,1) for _ in range(dim)]\n"
                "d=sys.argv[1]\n"
                "rp=ReadPath(d)\n"
                "out={'addrs0':[a.to_dict() for a in rp.list_addresses(0)],\n"
                "     'nrel':len(rp.relations)}\n"
                "if rp.list_addresses(0):\n"
                "    out['read0']=rp.read(rp.list_addresses(0)[0])\n"
                "if rp.relations:\n"
                "    out['rec0']=rp.reconstruct(rp.relations[0]['item_id'])\n"
                "out['search0']=rp.search(0, mv(2000), top_k=3)\n"
                "print(json.dumps(out, sort_keys=True))\n"
            )
        proc = subprocess.run([sys.executable, helper, d],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        sub = json.loads(proc.stdout)
        assert sub["addrs0"] == addrs0, "subprocess list_addresses differs"
        assert sub["read0"] == read0, "subprocess read differs"
        assert sub["rec0"] == rec0, "subprocess reconstruct differs"
        assert sub["search0"] == search0, "subprocess search differs"
        os.remove(helper)
        print("  subprocess (fresh process) equality: OK")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_readonly_no_mutation():
    print("=" * 70)
    print("TEST 6: Read Path does not mutate storage")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        build_store(d, 4)
        # snapshot file hashes
        import hashlib
        def snap():
            h = {}
            for root, _, files in os.walk(d):
                for fn in files:
                    p = os.path.join(root, fn)
                    with open(p, "rb") as f:
                        h[os.path.relpath(p, d)] = hashlib.sha256(f.read()).hexdigest()
            return h
        before = snap()
        rp = ReadPath(d)
        _ = rp.list_addresses(0)
        if rp.list_addresses(0):
            _ = rp.read(rp.list_addresses(0)[0])
        if rp.relations:
            _ = rp.reconstruct(rp.relations[0]["item_id"])
        _ = rp.search(0, make_vec(2000), top_k=2)
        rp.reload()
        after = snap()
        assert before == after, "storage mutated by Read Path"
        print("  storage unchanged after all read ops: OK")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def main():
    tests = [test_list_addresses, test_read, test_reconstruct, test_search,
             test_fresh_process_exact_equality, test_readonly_no_mutation]
    all_ok = True
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            all_ok = False
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR: {e}")
            all_ok = False
    print()
    print("=" * 70)
    print(f"READ PATH TESTS: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
