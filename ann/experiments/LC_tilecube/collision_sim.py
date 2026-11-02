"""C2: finite signature space (13440) — letter as address vs hint."""
import random

SPACE = 13440  # per axis, from handoff (25! combinatorics)

def test_collision_address_vs_hint():
    print("=== C2: finite signature space — letter as address vs hint ===")
    for n_rows in [100, 1000, 5000, 13440, 20000]:
        # generate n_rows signatures from space of size SPACE
        sigs = [random.randrange(SPACE) for _ in range(n_rows)]
        distinct = len(set(sigs))
        collisions = n_rows - distinct
        # if used as address (key): collision -> ambiguity (multiple rows same key)
        from collections import Counter
        c = Counter(sigs)
        ambiguous = sum(1 for v in c.values() if v > 1)
        # if used as hint: collisions fine, just less discriminative
        print(f"  n_rows={n_rows:6d} distinct={distinct:6d} collisions={collisions:6d} ambiguous_keys={ambiguous:5d}  "
              f"{'address OK' if collisions==0 else 'address AMBIGUOUS (use as hint)'}")

if __name__ == "__main__":
    test_collision_address_vs_hint()
    print("\ndone")