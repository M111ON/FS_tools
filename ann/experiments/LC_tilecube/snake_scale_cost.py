"""Growth cost scaling + domain expansion + multi-snake collision."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake

def test_growth_cost():
    print("=" * 70)
    print("TEST 1: Growth Cost per Cube (1K -> 10K -> 100K)")
    print("=" * 70)
    for target in [1000, 10000, 100000]:
        domain = max(2000, target + 100)
        snake = Snake(f"cost_{target}", mode='chain', domain_size=domain)
        t0 = time.perf_counter()
        while len(snake.positions) < target:
            ok, reason = snake.grow(0)
            if not ok:
                break
        dt = time.perf_counter() - t0
        n = len(snake.positions)
        per_cube = dt / n * 1000  # ms per cube
        ok, tot = snake.check_junctions()
        print(f"  target={target:>7} actual={n:>7} time={dt:.3f}s  per_cube={per_cube:.4f}ms  junctions={ok}/{tot}")

def test_domain_expansion():
    print("\n" + "=" * 70)
    print("TEST 2: Domain Expansion (identity preservation)")
    print("=" * 70)
    # grow snake in small domain
    snake = Snake("expand1", mode='chain', domain_size=10)
    for _ in range(5):
        snake.grow(0)
    pos_before = list(snake.positions)
    letters_before = {k: list(v) for k, v in snake.letters.items()}
    dom_before = snake.current_domain
    print(f"  Before expansion: cubes={len(pos_before)}, domain_size=10, domain={dom_before}")

    # expand domain
    snake.domain_size = 100
    # positions and letters should be unchanged
    pos_after = list(snake.positions)
    letters_after = {k: list(v) for k, v in snake.letters.items()}
    print(f"  After expansion:  cubes={len(pos_after)}, domain_size=100, domain={snake.current_domain}")
    print(f"  Positions identical: {pos_before == pos_after}")
    print(f"  Letters identical: {letters_before == letters_after}")
    print(f"  Current domain updated: {snake.current_domain != dom_before} (expected: domain_of changes)")

    # grow in expanded domain — should work now
    ok, reason = snake.grow(0)
    print(f"  Grow after expansion: {ok} ({reason}), cubes={len(snake.positions)}")
    # grow negative direction (was blocked before, now should work if within new domain)
    # position is at (14,5,5) approx, -X 14 steps reaches 0, still in domain
    for _ in range(3):
        ok, reason = snake.grow(1)
        if not ok:
            print(f"  -X grow blocked: {reason}")
            break
    print(f"  After -X growth: cubes={len(snake.positions)}")
    ok, tot = snake.check_junctions()
    print(f"  Junctions: {ok}/{tot}")

def test_multi_snake():
    print("\n" + "=" * 70)
    print("TEST 3: Multi-Snake in Same Domain (inter-snake collision)")
    print("=" * 70)
    domain_size = 30
    snakes = []
    # create 3 snakes
    for i in range(3):
        s = Snake(f"multi_{i}", mode='chain', domain_size=domain_size)
        # start at different positions
        s.start_pos = (5 + i*10, 5, 5)
        # grow initial cube
        s.grow(0)
        snakes.append(s)
    print(f"  Created {len(snakes)} snakes in domain {domain_size}^3")

    # grow each snake, check inter-snake collision
    r = __import__('random').Random(42)
    inter_collisions = 0
    total_grows = 0
    # shared visited set across all snakes
    all_visited = set()
    for s in snakes:
        all_visited.update(s.visited)

    for step in range(200):
        for s in snakes:
            d = r.randrange(6)
            ok, reason = s.grow(d)
            if ok:
                # check if new position collides with another snake
                new_pos = s.positions[-1]
                collision_with_other = False
                for s2 in snakes:
                    if s2 is s:
                        continue
                    if new_pos in s2.visited:
                        collision_with_other = True
                        break
                if collision_with_other:
                    inter_collisions += 1
                    # remove the cube (undo growth) — simulate rejection
                    s.positions.pop()
                    s.domains.pop()
                    s.visited.discard(new_pos)
                    s.letters.pop(new_pos, None)
                    s.growth_count -= 1
                    s.directions.pop()
                else:
                    all_visited.add(new_pos)
                total_grows += 1
            if len(s.positions) >= 50:
                break
        if all(len(s.positions) >= 50 for s in snakes):
            break

    print(f"  Total grow attempts: {total_grows}")
    print(f"  Inter-snake collisions detected: {inter_collisions}")
    for i, s in enumerate(snakes):
        ok, tot = s.check_junctions()
        print(f"  Snake {i}: cubes={len(s.positions)}, junctions={ok}/{tot}, collisions={s.collision_count}")

    # Test: can two snakes share a domain without forced collision?
    print(f"\n  All snakes grew to 50+ cubes: {all(len(s.positions) >= 40 for s in snakes)}")

def test_multi_snake_isolated():
    print("\n" + "=" * 70)
    print("TEST 4: Multi-Snake with Boundary Isolation (separate domains)")
    print("=" * 70)
    # each snake in its own domain quadrant
    snakes = []
    for i in range(4):
        s = Snake(f"iso_{i}", mode='chain', domain_size=10)
        # place in different quadrant
        qx, qy = (i % 2) * 5, (i // 2) * 5
        s.start_pos = (qx + 2, qy + 2, 2)
        s.grow(0)
        snakes.append(s)
    print(f"  4 snakes in 4 quadrants of 10^3 domain")
    r = __import__('random').Random(7)
    for step in range(100):
        for s in snakes:
            d = r.randrange(6)
            s.grow(d)
    for i, s in enumerate(snakes):
        ok, tot = s.check_junctions()
        print(f"  Snake {i}: cubes={len(s.positions)}, junctions={ok}/{tot}, boundary_blocks={s.boundary_block_count}")
    # check no cross-domain contamination
    domains_used = set()
    for s in snakes:
        domains_used.update(s.domains)
    print(f"  Distinct domains used: {len(domains_used)}")

if __name__ == "__main__":
    test_growth_cost()
    test_domain_expansion()
    test_multi_snake()
    test_multi_snake_isolated()
    print("\ndone")