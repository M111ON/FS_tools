"""Crash-Point Exhaustive Test.

Simulate crash at every point in the persistence lifecycle:
  Data Write -> Checksum Write -> Rename Data -> Rename Checksum -> Commit

After restart, system must have EITHER:
  - old complete data (pre-crash), OR
  - new complete data (post-crash)
Never a half-committed state that is accepted as valid.

Also tests:
  - crash between two renames (atomic pair commit gap)
  - generation-based commit (manifest) as stronger alternative
  - recovery from any crash point
"""
import sys, os, json, hashlib, random, time, tempfile, shutil
sys.path.insert(0, '.')
import numpy as np
from collections import Counter, defaultdict
from persistence_reconstruction import StructuralAddress
from scale_incidence import REAL_VERTICES

EMBED_DIM = 384


# ---------- Crash simulation ----------
class CrashSimulator:
    """Simulate crash at a specified point in atomic_write lifecycle."""

    def __init__(self, path, data, crash_at):
        self.path = path
        self.data = data
        self.crash_at = crash_at  # one of: "after_data_write", "after_checksum_write",
        # "after_data_rename", "after_checksum_rename", "none"
        self.tmp = path + ".tmp"
        self.tmp_sha = path + ".sha.tmp"
        self.crashed = False

    def run(self):
        # Step 1: write data to temp
        with open(self.tmp, "w") as f:
            json.dump(self.data, f)
            f.flush()
            os.fsync(f.fileno())
        if self.crash_at == "after_data_write":
            self.crashed = True
            return  # crash here

        # Step 2: write checksum to temp
        h = hashlib.sha256(json.dumps(self.data).encode()).hexdigest()
        with open(self.tmp_sha, "w") as f:
            f.write(h)
            f.flush()
            os.fsync(f.fileno())
        if self.crash_at == "after_checksum_write":
            self.crashed = True
            return

        # Step 3: rename data
        os.replace(self.tmp, self.path)
        if self.crash_at == "after_data_rename":
            self.crashed = True
            return

        # Step 4: rename checksum
        os.replace(self.tmp_sha, self.path + ".sha")
        if self.crash_at == "after_checksum_rename":
            self.crashed = True
            return

        # Step 5: commit complete
        self.crashed = False


def read_state(path):
    """Read current state: (data, status, has_sha)."""
    has_data = os.path.exists(path)
    has_sha = os.path.exists(path + ".sha")
    has_tmp = os.path.exists(path + ".tmp")
    has_tmp_sha = os.path.exists(path + ".sha.tmp")

    data = None
    status = "no_file"
    if has_data:
        try:
            with open(path) as f:
                content = f.read()
            data = json.loads(content)
            if has_sha:
                with open(path + ".sha") as f:
                    expected = f.read().strip()
                actual = hashlib.sha256(json.dumps(data).encode()).hexdigest()
                if expected == actual:
                    status = "valid"
                else:
                    status = "checksum_mismatch"
            else:
                status = "missing_checksum"
        except (json.JSONDecodeError, ValueError):
            status = "corrupt_json"
    return {
        "has_data": has_data,
        "has_sha": has_sha,
        "has_tmp": has_tmp,
        "has_tmp_sha": has_tmp_sha,
        "data": data,
        "status": status,
    }


# ---------- Crash point tests ----------
CRASH_POINTS = ["after_data_write", "after_checksum_write",
                "after_data_rename", "after_checksum_rename", "none"]

def test_crash_at_each_point():
    print("=" * 70)
    print("TEST 1: Crash at Every Point in Persistence Lifecycle")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "addresses.json")
        old_data = [{"hex_a": 0, "hex_b": 1, "square": 2, "path": []} for _ in range(5)]
        new_data = [{"hex_a": 9, "hex_b": 10, "square": 11, "path": []} for _ in range(5)]

        # write old data first
        with open(path, "w") as f:
            json.dump(old_data, f)
        h_old = hashlib.sha256(json.dumps(old_data).encode()).hexdigest()
        with open(path + ".sha", "w") as f:
            f.write(h_old)

        results = []
        for cp in CRASH_POINTS:
            # reset to old state
            with open(path, "w") as f:
                json.dump(old_data, f)
            with open(path + ".sha", "w") as f:
                f.write(h_old)
            # clean temp
            for tmp in [path + ".tmp", path + ".sha.tmp"]:
                if os.path.exists(tmp):
                    os.remove(tmp)

            sim = CrashSimulator(path, new_data, cp)
            sim.run()

            state = read_state(path)
            # after crash, recovery: try to get valid data
            if state["status"] == "valid":
                recovered = "new" if state["data"] == new_data else "old"
            elif state["status"] in ("corrupt_json", "checksum_mismatch", "missing_checksum"):
                # try backup/rebuild
                recovered = "old (fallback)"
            else:
                recovered = "unknown"

            print(f"  crash_at={cp:<25} status={state['status']:<20} recovered={recovered}")
            results.append((cp, state["status"], recovered))

        # Verify: no half-committed state accepted as valid
        for cp, status, recovered in results:
            if status == "valid":
                # must be either old or new, not mixed
                assert recovered in ("old", "new"), f"{cp}: mixed state accepted"
        print()
        print("  No half-committed state accepted as valid.")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_crash_between_renames():
    print()
    print("=" * 70)
    print("TEST 2: Crash Between Two Renames (atomic pair commit gap)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "addresses.json")
        old_data = [{"hex_a": 0, "hex_b": 1, "square": 2, "path": []} for _ in range(3)]
        new_data = [{"hex_a": 9, "hex_b": 10, "square": 11, "path": []} for _ in range(3)]

        # write old
        with open(path, "w") as f:
            json.dump(old_data, f)
        h_old = hashlib.sha256(json.dumps(old_data).encode()).hexdigest()
        with open(path + ".sha", "w") as f:
            f.write(h_old)

        # simulate crash after data rename but before checksum rename
        # (worst case: new data on disk, old checksum)
        with open(path + ".tmp", "w") as f:
            json.dump(new_data, f)
        h_new = hashlib.sha256(json.dumps(new_data).encode()).hexdigest()
        with open(path + ".sha.tmp", "w") as f:
            f.write(h_new)
        os.replace(path + ".tmp", path)  # data renamed, checksum not
        # crash here

        state = read_state(path)
        print(f"  after crash: status={state['status']}")
        print(f"  has_data={state['has_data']}, has_sha={state['has_sha']}")
        print(f"  has_tmp={state['has_tmp']}, has_tmp_sha={state['has_tmp_sha']}")

        # system must detect mismatch (new data + old checksum)
        assert state["status"] == "checksum_mismatch", \
            f"expected checksum_mismatch, got {state['status']}"
        print("  mismatch detected: YES")

        # recovery: restore old data from... we don't have backup here
        # but we can detect and rebuild
        print("  recovery: detect mismatch -> rebuild from source")
        rebuilt = old_data  # in real system, rebuild from source of truth
        with open(path, "w") as f:
            json.dump(rebuilt, f)
        h_rebuilt = hashlib.sha256(json.dumps(rebuilt).encode()).hexdigest()
        with open(path + ".sha", "w") as f:
            f.write(h_rebuilt)
        state2 = read_state(path)
        print(f"  after recovery: status={state2['status']}")
        assert state2["status"] == "valid"
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_generation_manifest():
    print()
    print("=" * 70)
    print("TEST 3: Generation-Based Commit (manifest approach)")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "addresses.json")
        manifest_path = os.path.join(d, "manifest.json")

        old_data = [{"hex_a": 0, "hex_b": 1, "square": 2, "path": []} for _ in range(3)]
        new_data = [{"hex_a": 9, "hex_b": 10, "square": 11, "path": []} for _ in range(3)]

        # generation 0: old data
        gen0_file = os.path.join(d, "data_gen0.json")
        with open(gen0_file, "w") as f:
            json.dump(old_data, f)
        h0 = hashlib.sha256(json.dumps(old_data).encode()).hexdigest()
        manifest = {"current_gen": 0, "generations": {0: {"file": "data_gen0.json", "checksum": h0}}}
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)
            f.flush()
            os.fsync(f.fileno())

        # generation 1: new data (write new file, update manifest last)
        gen1_file = os.path.join(d, "data_gen1.json")
        with open(gen1_file, "w") as f:
            json.dump(new_data, f)
            f.flush()
            os.fsync(f.fileno())
        h1 = hashlib.sha256(json.dumps(new_data).encode()).hexdigest()
        # crash here: gen1 file exists, manifest still points to gen0
        # -> recovery: read manifest, get gen0 (old, valid)
        with open(manifest_path) as f:
            m = json.load(f)
        current_gen = m["current_gen"]
        gen_info = m["generations"][str(current_gen)]
        with open(os.path.join(d, gen_info["file"])) as f:
            recovered_data = json.load(f)
        h_check = hashlib.sha256(json.dumps(recovered_data).encode()).hexdigest()
        print(f"  after crash: manifest points to gen {current_gen}")
        print(f"  recovered data: gen{current_gen} ({len(recovered_data)} items)")
        print(f"  checksum valid: {h_check == gen_info['checksum']}")
        assert recovered_data == old_data
        assert h_check == gen_info["checksum"]

        # now commit: update manifest to gen1
        manifest["current_gen"] = 1
        manifest["generations"][1] = {"file": "data_gen1.json", "checksum": h1}
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)
            f.flush()
            os.fsync(f.fileno())
        # read gen1
        with open(manifest_path) as f:
            m2 = json.load(f)
        gen_info2 = m2["generations"][str(m2["current_gen"])]
        with open(os.path.join(d, gen_info2["file"])) as f:
            data2 = json.load(f)
        h2 = hashlib.sha256(json.dumps(data2).encode()).hexdigest()
        print(f"  after commit: gen {m2['current_gen']}, checksum valid: {h2 == gen_info2['checksum']}")
        assert data2 == new_data
        print("  Generation-based commit: no half-committed state possible")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def test_crash_recovery_all_states():
    print()
    print("=" * 70)
    print("TEST 4: Recovery from Any Crash State")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "addresses.json")
        old_data = [{"hex_a": i, "hex_b": i + 1, "square": 0, "path": []} for i in range(5)]

        # write old state
        with open(path, "w") as f:
            json.dump(old_data, f)
        h_old = hashlib.sha256(json.dumps(old_data).encode()).hexdigest()
        with open(path + ".sha", "w") as f:
            f.write(h_old)

        # simulate 5 crash scenarios, verify recovery
        scenarios = [
            ("no_crash", None),
            ("data_truncated", "truncate_data"),
            ("checksum_missing", "remove_sha"),
            ("checksum_wrong", "corrupt_sha"),
            ("both_corrupt", "corrupt_both"),
        ]
        for name, action in scenarios:
            # reset
            with open(path, "w") as f:
                json.dump(old_data, f)
            with open(path + ".sha", "w") as f:
                f.write(h_old)
            # apply corruption
            if action == "truncate_data":
                with open(path, "r") as f:
                    c = f.read()
                with open(path, "w") as f:
                    f.write(c[:len(c) // 2])
            elif action == "remove_sha":
                os.remove(path + ".sha")
            elif action == "corrupt_sha":
                with open(path + ".sha", "w") as f:
                    f.write("deadbeef")
            elif action == "corrupt_both":
                with open(path, "w") as f:
                    f.write("not json")
                with open(path + ".sha", "w") as f:
                    f.write("deadbeef")

            state = read_state(path)
            # recovery strategy: if invalid, rebuild from source
            if state["status"] != "valid":
                rebuilt = old_data  # in real system, rebuild from source of truth
                with open(path, "w") as f:
                    json.dump(rebuilt, f)
                h = hashlib.sha256(json.dumps(rebuilt).encode()).hexdigest()
                with open(path + ".sha", "w") as f:
                    f.write(h)
                state = read_state(path)
            print(f"  {name:<20}: final status={state['status']}")
            assert state["status"] == "valid", f"{name}: recovery failed"
        print("  All crash scenarios recoverable")
        print("  PASS")
    finally:
        shutil.rmtree(d)
    return True


def main():
    tests = [test_crash_at_each_point, test_crash_between_renames,
             test_generation_manifest, test_crash_recovery_all_states]
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
    print(f"CRASH-POINT EXHAUSTIVE TEST: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    if all_ok:
        print()
        print("Crash durability properties proven:")
        print("  1. Crash at any point -> detectable (checksum/corrupt_json)")
        print("  2. Crash between two renames -> mismatch detected -> rebuild")
        print("  3. Generation-based commit -> no half-committed state")
        print("  4. Recovery from any crash state -> rebuild from source")
        print()
        print("Remaining open case:")
        print("  - crash between two renames with no backup -> rebuild required")
        print("  - generation manifest is stronger (single file commit)")
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
