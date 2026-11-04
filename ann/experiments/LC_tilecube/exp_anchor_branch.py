"""Experiment: anchor-style branch opening driven by usage (LC x DWGLS concept).

Concept borrowed (not code): deterministic, value-blind, append-only branches
that open only when used, and can regrow under a shifted capo (climate change).
Standalone Python prototype; does not touch DWGLS-native-fs.
"""
import sys, os, random, hashlib
sys.path.insert(0, '.')


class AnchorBranches:
    def __init__(self, seed, branch_count=8):
        self.seed = int(seed)
        self.branch_count = int(branch_count)
        self.branches = {}        # branch_id -> list of item_ids (append-only)
        self.usage = {}           # branch_id -> count
        self.opened_order = []    # branches in order first used

    def _route(self, item_id):
        # value-blind: depends on item_id + capo seed, never on vector values
        h = hashlib.sha256(f"{self.seed}|{item_id}".encode()).hexdigest()
        return int(h, 16) % self.branch_count

    def ingest(self, item_id):
        b = self._route(item_id)
        if b not in self.branches:
            self.branches[b] = []
            self.opened_order.append(b)
        self.branches[b].append(item_id)
        self.usage[b] = self.usage.get(b, 0) + 1
        return b

    def climate_shift(self, new_seed):
        # regrow: re-route all items under new capo seed; rebuild branches
        all_items = []
        for b in self.branches:
            all_items.extend(self.branches[b])
        self.seed = int(new_seed)
        self.branches = {}
        self.usage = {}
        self.opened_order = []
        for iid in all_items:
            self.ingest(iid)
        return len(all_items)

    def item_set(self):
        out = set()
        for b in self.branches:
            out.update(self.branches[b])
        return out


def test_usage_driven():
    print("=" * 60)
    print("TEST 1: usage-driven branch opening")
    ab = AnchorBranches(seed=42, branch_count=8)
    items = [f"item_{i}" for i in range(20)]
    for iid in items:
        ab.ingest(iid)
    print(f"  items={len(items)} opened_branches={len(ab.branches)}/{ab.branch_count}")
    print(f"  opened_order={ab.opened_order}")
    assert len(ab.branches) <= ab.branch_count
    # with 20 items likely not all 8 branches open? (could be; check <=)
    print("  PASS")


def test_append_only():
    print("=" * 60)
    print("TEST 2: append-only prefix preserved")
    ab = AnchorBranches(seed=7, branch_count=4)
    items = [f"i{i}" for i in range(12)]
    for iid in items:
        ab.ingest(iid)
    # snapshot
    snap = {b: list(v) for b, v in ab.branches.items()}
    # ingest more
    more = [f"j{i}" for i in range(5)]
    for iid in more:
        ab.ingest(iid)
    for b in snap:
        assert ab.branches[b][:len(snap[b])] == snap[b], f"prefix changed in branch {b}"
    print("  prefixes preserved after append: OK")
    print("  PASS")


def test_value_blind():
    print("=" * 60)
    print("TEST 3: value-blind (route depends on id, not vector)")
    ab = AnchorBranches(seed=99, branch_count=6)
    # same item_id -> same branch regardless of "value"
    b1 = ab._route("x")
    b2 = ab._route("x")
    assert b1 == b2
    # different id -> possibly different
    print(f"  route('x')={b1} deterministic: OK")
    print("  PASS")


def test_climate_shift():
    print("=" * 60)
    print("TEST 4: climate shift regrow (item set preserved)")
    ab = AnchorBranches(seed=1, branch_count=6)
    items = [f"it{i}" for i in range(40)]
    for iid in items:
        ab.ingest(iid)
    before = ab.item_set()
    n = ab.climate_shift(new_seed=2)
    after = ab.item_set()
    print(f"  items={n} before_set={len(before)} after_set={len(after)} identical={before==after}")
    assert before == after, "item set changed after climate shift"
    # determinism: shift to same seed twice -> identical branch structure
    ab2 = AnchorBranches(seed=1, branch_count=6)
    for iid in items:
        ab2.ingest(iid)
    ab2.climate_shift(2)
    # compare opened branch ids multiset
    assert sorted(ab.branches.keys()) == sorted(ab2.branches.keys())
    print("  regrow deterministic across instances: OK")
    print("  PASS")


def test_branch_count_growth():
    print("=" * 60)
    print("TEST 5: branch usage distribution")
    ab = AnchorBranches(seed=2024, branch_count=8)
    for i in range(100):
        ab.ingest(f"e{i}")
    print("  usage per branch:", {b: ab.usage[b] for b in sorted(ab.usage)})
    assert sum(ab.usage.values()) == 100
    print("  total usage = 100: OK")
    print("  PASS")


def main():
    for t in [test_usage_driven, test_append_only, test_value_blind,
              test_climate_shift, test_branch_count_growth]:
        t()
    print()
    print("EXPERIMENT: all checks PASS")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
