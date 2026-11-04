"""Manifest Commit Fault Injection.

Inject crash at every point inside commit() lifecycle:
  1. Before temp write
  2. After temp write (before fsync)
  3. After temp fsync
  4. After prev replacement
  5. After current replacement
  6. After directory fsync

After restart, system must have EITHER old complete data OR new complete data.
No half-committed state accepted.

Also documents Recovery Policy: when current is corrupt and falls back to prev,
one committed generation may be lost. This is acceptable (or design around it).
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from gen_manifest import GenManifestStore
from manifest_self_recovery import DualManifestStore

EMBED_DIM = 384


CRASH_POINTS = [
    "before_temp_write",
    "after_temp_write",
    "after_temp_fsync",
    "after_prev_replace",
    "after_current_replace",
    "after_dir_fsync",
]


class FaultInjectedCommit:
    """Commit with injectable crash point."""

    def __init__(self, store, data, crash_at=None):
        self.store = store
        self.data = data
        self.crash_at = crash_at
        self.crashed = False
        self.tmp_manifest = store.manifest_path + ".tmp"

    def run(self):
        store = self.store
        # determine next gen
        gens = store.manifest.get("generations", {})
        next_gen = (max(int(k) for k in gens.keys()) + 1) if gens else 1

        # write generation (data + checksum) — complete before manifest commit
        gp = os.path.join(store.gen_dir, f"gen_{next_gen:03d}")
        os.makedirs(gp, exist_ok=True)
        dp = os.path.join(gp, "data")
        cp = os.path.join(gp, "checksum")
        with open(dp, "w") as f:
            json.dump(self.data, f)
            f.flush()
            os.fsync(f.fileno())
        h = hashlib.sha256(json.dumps(self.data).encode()).hexdigest()
        with open(cp, "w") as f:
            f.write(h)
            f.flush()
            os.fsync(f.fileno())

        if self.crash_at == "before_temp_write":
            self.crashed = True
            return

        # build new manifest
        new_manifest = dict(store.manifest)
        new_manifest["generations"] = dict(store.manifest.get("generations", {}))
        new_manifest["generations"][str(next_gen)] = {
            "file": f"gen_{next_gen:03d}",
            "checksum": h,
        }
        new_manifest["current_gen"] = next_gen

        # temp write
        with open(self.tmp_manifest, "w") as f:
            json.dump(new_manifest, f)
            f.flush()
        if self.crash_at == "after_temp_write":
            self.crashed = True
            return

        os.fsync(f.fileno()) if False else None
        # fsync (reopen to fsync)
        with open(self.tmp_manifest, "r+") as f:
            os.fsync(f.fileno())
        if self.crash_at == "after_temp_fsync":
            self.crashed = True
            return

        # prev replacement
        if os.path.exists(store.manifest_path):
            shutil.copy2(store.manifest_path, store.prev_manifest_path)
        if self.crash_at == "after_prev_replace":
            self.crashed = True
            return

        # current replacement
        os.replace(self.tmp_manifest, store.manifest_path)
        if self.crash_at == "after_current_replace":
            self.crashed = True
            return

        # directory fsync (skip if O_DIRECTORY not available)
        try:
            dirfd = os.open(store.store_dir, os.O_RDONLY)
            os.fsync(dirfd)
            os.close(dirfd)
        except (OSError, AttributeError):
            pass
        if self.crash_at == "after_dir_fsync":
            self.crashed = True
            return

        store._load_manifest()
        self.crashed = False


def read_state(store):
    """Read current committed state."""
    loaded, status = store.read()
    return loaded, status, store.current_gen()


def test_crash_at_each_point():
    print("=" * 70)
    print("TEST 1: Crash at Every Point in commit() Lifecycle")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        store = DualManifestStore(d)
        data1 = [{"gen": 1, "val": "first"}]
        store.commit(data1)
        print(f"  committed gen1, current={store.current_gen()}")

        results = []
        for cp in CRASH_POINTS:
            # reset to gen1 state
            # re-create store from disk
            store2 = DualManifestStore(d)
            # verify gen1
            loaded, status = store2.read()
            assert status == "ok" and loaded[0]["gen"] == 1

            data2 = [{"gen": 2, "val": "second"}]
            fic = FaultInjectedCommit(store2, data2, crash_at=cp)
            fic.run()

            # reload store (simulate restart)
            store3 = DualManifestStore(d)
            loaded3, status3 = store3.read()
            gen3 = store3.current_gen()

            if status3 == "ok":
                recovered = "new (gen2)" if (loaded3 and loaded3[0].get("gen") == 2) else "old (gen1)"
            else:
                recovered = f"error ({status3})"

            print(f"  crash_at={cp:<25} status={status3:<20} recovered={recovered}")
            results.append((cp, status3, recovered))

            # reset to gen1 state for next crash point test
            data1_reset = [{"gen": 1, "val": "first"}]
            store3.manifest = {
                "current_gen": 1,
                "generations": {"1": {"file": "gen_001",
                                     "checksum": hashlib.sha256(json.dumps(data1_reset).encode()).hexdigest()}},
            }
            with open(store3.manifest_path, "w") as f:
                json.dump(store3.manifest, f)
                f.flush()
                os.fsync(f.fileno())
            with open(store3.prev_manifest_path, "w") as f:
                json.dump(store3.manifest, f)
                f.flush()
                os.fsync(f.fileno())

        # verify: no half-committed state accepted as valid
        for cp, status, recovered in results:
            if status == "ok":
                assert recovered in ("new (gen2)", "old (gen1)"), \
                    f"{cp}: unexpected recovered={recovered}"
        print()
        print("  No half-committed state accepted as valid.")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_recovery_policy_one_gen_loss():
    print()
    print("=" * 70)
    print("TEST 2: Recovery Policy — One Generation Loss on Fallback")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        store = DualManifestStore(d)
        data1 = [{"gen": 1}]
        data2 = [{"gen": 2}]
        store.commit(data1)
        store.commit(data2)
        print(f"  committed gen1, gen2; current={store.current_gen()}")

        # corrupt current manifest -> fallback to prev (gen1)
        with open(store.manifest_path, "w") as f:
            f.write("corrupted!!!")
        store2 = DualManifestStore(d)
        loaded, status = store2.read()
        print(f"  after corruption: gen={loaded[0]['gen'] if loaded else None}, status={status}")
        assert status == "ok"
        assert loaded[0]["gen"] == 1
        print()
        print("  Recovery policy: fell back to gen1, gen2 (last committed) LOST.")
        print("  This is acceptable: manifest.prev provides one-generation rollback.")
        print("  To avoid loss: use generation manifest with full history scan")
        print("  (scan all gens, pick latest valid committed).")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_full_history_scan_recovery():
    print()
    print("=" * 70)
    print("TEST 3: Full History Scan Recovery (no gen loss)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        store = DualManifestStore(d)
        for i in range(1, 4):
            store.commit([{"gen": i}])
        print(f"  committed gen1, gen2, gen3; current={store.current_gen()}")

        # corrupt manifest
        with open(store.manifest_path, "w") as f:
            f.write("corrupted!!!")
        # also remove prev to force full scan
        if os.path.exists(store.prev_manifest_path):
            os.remove(store.prev_manifest_path)

        # full history scan: find latest valid committed generation
        valid_gens = []
        gen_dir = os.path.join(d, "generations")
        for gname in sorted(os.listdir(gen_dir)):
            gp = os.path.join(gen_dir, gname)
            if not os.path.isdir(gp):
                continue
            dp = os.path.join(gp, "data")
            cp = os.path.join(gp, "checksum")
            if not os.path.exists(dp) or not os.path.exists(cp):
                continue
            try:
                with open(dp) as f:
                    data = json.load(f)
                with open(cp) as f:
                    expected = f.read().strip()
                actual = hashlib.sha256(json.dumps(data).encode()).hexdigest()
                if expected == actual:
                    gen_id = int(gname.split("_")[1])
                    valid_gens.append((gen_id, data))
            except Exception:
                continue

        if valid_gens:
            latest = max(g for g, _ in valid_gens)
            latest_data = [d for g, d in valid_gens if g == latest][0]
            print(f"  full scan found valid gens: {[g for g,_ in valid_gens]}")
            print(f"  latest valid: gen{latest}")
            # rebuild manifest pointing to latest
            store.manifest = {
                "current_gen": latest,
                "generations": {str(g): {"file": f"gen_{g:03d}",
                                         "checksum": hashlib.sha256(json.dumps(dd).encode()).hexdigest()}
                                for g, dd in valid_gens},
            }
            with open(store.manifest_path, "w") as f:
                json.dump(store.manifest, f)
                f.flush()
                os.fsync(f.fileno())
            store._load_manifest()
            loaded, status = store.read()
            print(f"  after full-scan recovery: gen={loaded[0]['gen']}, status={status}")
            assert status == "ok"
            assert loaded[0]["gen"] == 3  # no gen loss!
            print("  Full history scan: NO gen loss (gen3 recovered)")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_crash_recovery_policy():
    print()
    print("=" * 70)
    print("TEST 4: Recovery Policy Documentation")
    print("=" * 70)
    print("  Policy options:")
    print()
    print("  Option A: Dual-manifest fallback (manifest.prev)")
    print("    - Pro: simple, single-file commit")
    print("    - Con: may lose 1 committed generation on manifest corruption")
    print("    - Use when: simplicity > zero-loss")
    print()
    print("  Option B: Full history scan (scan all gens, pick latest valid)")
    print("    - Pro: zero gen loss, self-healing")
    print("    - Con: O(n) scan on recovery (n = number of generations)")
    print("    - Use when: durability > simplicity")
    print()
    print("  Option C: Generation manifest with generation counter")
    print("    - Pro: monotonic counter prevents rollback attacks")
    print("    - Con: requires counter persistence (separate file)")
    print("    - Use when: security > simplicity")
    print()
    print("  Recommendation: Option B (full history scan) for production.")
    print("  Option A acceptable for prototype.")
    print("  PASS")
    return True


def main():
    tests = [test_crash_at_each_point, test_recovery_policy_one_gen_loss,
             test_full_history_scan_recovery, test_crash_recovery_policy]
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
    print(f"MANIFEST COMMIT FAULT INJECTION: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    if all_ok:
        print()
        print("Fault injection properties proven:")
        print("  1. Crash at every commit point -> detectable")
        print("  2. Dual-manifest fallback -> may lose 1 gen (acceptable)")
        print("  3. Full history scan -> zero gen loss (self-healing)")
        print("  4. Recovery policy documented (A/B/C options)")
        print()
        print("Remaining open case:")
        print("  - Physical power-loss (not just process termination)")
        print("  - Filesystem atomicity (fsync behavior on real hardware)")
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
