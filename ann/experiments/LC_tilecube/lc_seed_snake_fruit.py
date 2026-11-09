"""Task 4: Integrate LC Seed as Key into Snake/Fruit Rewind Buffer (972 slots).
Temporal Ring = 972 slots (18 x 54). Seed maps to slot via modulo + open addressing.
"""
TEMPORAL_RING = 972  # 18 * 54

class RewindBuffer:
    def __init__(self, size=TEMPORAL_RING):
        self.size = size
        self.slots = [None] * size   # each: (seed, value) or None
        self.collisions = 0
        self.inserts = 0

    def _probe(self, seed):
        idx = seed % self.size
        start = idx
        while self.slots[idx] is not None and self.slots[idx][0] != seed:
            idx = (idx + 1) % self.size
            self.collisions += 1
            if idx == start:
                raise RuntimeError("rewind buffer full")
        return idx

    def store(self, seed, value):
        idx = self._probe(seed)
        self.slots[idx] = (seed, value)
        self.inserts += 1
        return idx

    def load(self, seed):
        idx = seed % self.size
        start = idx
        while self.slots[idx] is not None:
            if self.slots[idx][0] == seed:
                return self.slots[idx][1]
            idx = (idx + 1) % self.size
            if idx == start:
                break
        return None

    def stats(self):
        used = sum(1 for s in self.slots if s is not None)
        return {"size": self.size, "used": used, "inserts": self.inserts,
                "collisions": self.collisions, "fill": used / self.size}


def integrate_seeds(seeds, values=None):
    buf = RewindBuffer()
    if values is None:
        values = list(range(len(seeds)))
    for sd, v in zip(seeds, values):
        buf.store(int(sd), v)
    return buf


if __name__ == "__main__":
    # demo: 864 base seeds across one dim
    seeds = [((t << 5) | b) for t in range(27) for b in range(32)]
    assert len(seeds) == 864
    buf = integrate_seeds(seeds)
    # determinism: load back all
    ok = all(buf.load(s) is not None for s in seeds)
    print(f"seeds={len(seeds)} buffer={buf.stats()}")
    print(f"all seeds retrievable: {ok}")
    # collision report
    print(f"collisions during insert: {buf.collisions}")
    # multi-dim: tag seeds for dim 1
    seeds2 = [s | (1 << 10) for s in seeds]
    buf2 = integrate_seeds(seeds2)
    print(f"dim1 buffer: {buf2.stats()}")
    # cross-dim no collision in slot mapping? check slot overlap
    slots1 = set(s % TEMPORAL_RING for s in seeds)
    slots2 = set(s % TEMPORAL_RING for s in seeds2)
    print(f"slot overlap across dims: {len(slots1 & slots2)}")
    assert ok
    print("SNAKE/FRUIT INTEGRATION PASS")
