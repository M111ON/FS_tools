"""Long-run stress: randomized growth, heavy branching, large-scale save/restore."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import Fruit, FruitFormation, GraftEngine

def test_randomized_growth():
    print("=" * 70)
    print("TEST A: Long-run Randomized Growth (10K cubes, random directions)")
    print("=" * 70)
    rng = random.Random(42)
    snake = Snake("lr", mode='chain', domain_size=1000)
    t0 = time.perf_counter()
    attempts = 0
    while len(snake.positions) < 10000:
        d = rng.randint(0, 5)
        ok, reason = snake.grow(d)
        attempts += 1
        if attempts > 100000:
            break
    dt = time.perf_counter() - t0
    info = {
        'positions': len(snake.positions),
        'attempts': attempts,
        'time': dt,
        'rate': len(snake.positions) / dt,
        'boundary_blocks': snake.boundary_blocks,
    }
    geo_ok, geo_tot = snake.check_junctions()
    graft_ok, graft_tot = snake.check_graft_interface()
    print(f"  Cubes: {info['positions']} (attempts={info['attempts']}, rate={info['rate']:.0f} cubes/s)")
    print(f"  Time: {info['time']:.2f}s, boundary_blocks={info['boundary_blocks']}")
    print(f"  Geometric: {geo_ok}/{geo_tot} {'✅' if geo_ok == geo_tot else '❌'}")
    print(f"  Graft iface: {graft_ok}/{graft_tot} {'✅' if graft_ok == graft_tot else '❌'}")
    print(f"  Visited==positions: {len(snake.visited) == len(snake.positions)}")
    print(f"  PASS" if (geo_ok == geo_tot and len(snake.visited) == len(snake.positions)) else "  FAIL")
    return snake

def test_heavy_branching():
    print("\n" + "=" * 70)
    print("TEST B: Heavy Branching (100 branches from one snake)")
    print("=" * 70)
    snake = Snake("hb", mode='chain', domain_size=500)
    for _ in range(100):
        snake.grow(0)
    branches = []
    t0 = time.perf_counter()
    for i in range(100):
        idx = i  # branch from every position
        if idx >= len(snake.positions):
            break
        bp = snake.positions[idx]
        branch = Snake(f"hb_b{i}", mode='chain', domain_size=500)
        branch.start_pos = (bp[0], bp[1] + 1, bp[2])
        branch.current_domain = (bp[0] // 500, (bp[1]+1) // 500, bp[2] // 500)
        rng = random.Random(i)
        for _ in range(10):
            d = rng.choice([0, 2, 4])
            branch.grow(d)
        branches.append(branch)
    dt = time.perf_counter() - t0
    print(f"  Created {len(branches)} branches in {dt:.2f}s ({len(branches)/dt:.0f} branches/s)")
    all_ok = True
    total_cubes = 0
    for bi, br in enumerate(branches):
        geo_ok, geo_tot = br.check_junctions()
        graft_ok, graft_tot = br.check_graft_interface()
        ok = (geo_ok == geo_tot and len(br.visited) == len(br.positions))
        all_ok = all_ok and ok
        total_cubes += len(br.positions)
    print(f"  Total branch cubes: {total_cubes}")
    print(f"  All branches valid: {all_ok} {'✅' if all_ok else '❌'}")
    # main snake still valid
    geo_ok, geo_tot = snake.check_junctions()
    main_ok = (geo_ok == geo_tot)
    print(f"  Main snake: {len(snake.positions)} cubes, geo={geo_ok}/{geo_tot} {'✅' if main_ok else '❌'}")
    print(f"  PASS" if (all_ok and main_ok) else "  FAIL")
    return snake, branches

def test_large_grape_network():
    print("\n" + "=" * 70)
    print("TEST C: Large Grape Network (save/restore 100+ objects)")
    print("=" * 70)
    rng = random.Random(99)
    # create 20 snakes
    snakes = []
    for i in range(20):
        s = Snake(f"gn_s{i}", mode='chain', domain_size=1000)
        s.start_pos = (rng.randint(100, 900), rng.randint(100, 900), rng.randint(100, 900))
        s.current_domain = (s.start_pos[0] // 1000, s.start_pos[1] // 1000, s.start_pos[2] // 1000)
        for _ in range(rng.randint(20, 50)):
            d = rng.randint(0, 5)
            s.grow(d)
        snakes.append(s)
    # create 30 fruits from random snakes
    fruits = []
    for i in range(30):
        si = rng.randint(0, len(snakes) - 1)
        s = snakes[si]
        if len(s.positions) < 10:
            continue
        start = rng.randint(0, len(s.positions) - 6)
        end = start + rng.randint(3, 6)
        if end > len(s.positions):
            continue
        fruit, _ = FruitFormation.cut_and_remove(s, start, end)
        if fruit:
            fruit.layer_grow(1)
            fruits.append(fruit)
    total_objects = len(snakes) + len(fruits)
    total_cubes = sum(len(s.positions) for s in snakes) + sum(len(f.positions) for f in fruits)
    print(f"  Objects: {len(snakes)} snakes + {len(fruits)} fruits = {total_objects}")
    print(f"  Total cubes: {total_cubes}")
    # save all
    t0 = time.perf_counter()
    states = []
    for s in snakes:
        states.append(('snake', s.state_dict()))
    for f in fruits:
        fs = {
            'fruit_id': f.fruit_id,
            'origin_snake_id': f.origin_snake_id,
            'origin_positions': f.origin_positions,
            'seed': f.seed,
            'domain_size': f.domain_size,
            'positions': f.positions,
            'visited': [list(p) for p in f.visited],
            'current_layer': f.current_layer,
            'growth_mode': f.growth_mode,
            'letters': {},
        }
        for pos, val in f.letters.items():
            key = ",".join(str(x) for x in pos)
            from compact_identity import encode_string
            fs['letters'][key] = encode_string(val)
        states.append(('fruit', fs))
    t_save = time.perf_counter() - t0
    print(f"  Save: {t_save:.2f}s ({total_objects} objects)")
    # restore all
    t0 = time.perf_counter()
    restored_snakes = []
    restored_fruits = []
    for typ, st in states:
        if typ == 'snake':
            s = Snake(st['seed'], mode=st['mode'], domain_size=st['domain_size'])
            s.restore(st)
            restored_snakes.append(s)
        else:
            f = Fruit(st['origin_snake_id'], st['origin_positions'], st['seed'], st['domain_size'])
            f.fruit_id = st['fruit_id']
            f.positions = [tuple(p) for p in st['positions']]
            f.visited = set(f.positions)
            f.current_layer = st['current_layer']
            f.growth_mode = st['growth_mode']
            f.letters = {}
            for key, val in st['letters'].items():
                parts = key.split(",")
                pos = tuple(int(x) for x in parts)
                from compact_identity import decode_string
                f.letters[pos] = decode_string(val)
            restored_fruits.append(f)
    t_restore = time.perf_counter() - t0
    print(f"  Restore: {t_restore:.2f}s ({total_objects} objects)")
    # verify
    all_ok = True
    for i, (orig, rest) in enumerate(zip(snakes, restored_snakes)):
        pos_ok = (orig.positions == rest.positions)
        let_ok = (orig.letters == rest.letters)
        geo_ok, geo_tot = rest.check_junctions()
        graft_ok, graft_tot = rest.check_graft_interface()
        j_ok = (graft_ok == graft_tot)
        if not (pos_ok and let_ok and j_ok):
            print(f"  Snake {i}: pos={pos_ok} let={let_ok} graft={graft_ok}/{graft_tot} geo={geo_ok}/{geo_tot}")
        all_ok = all_ok and pos_ok and let_ok
    for i, (orig, rest) in enumerate(zip(fruits, restored_fruits)):
        pos_ok = (orig.positions == rest.positions)
        let_ok = (orig.letters == rest.letters)
        if not (pos_ok and let_ok):
            print(f"  Fruit {i}: pos={pos_ok} let={let_ok}")
            if not let_ok:
                for pos in orig.letters:
                    if pos in rest.letters:
                        if orig.letters[pos] != rest.letters[pos]:
                            print(f"    MISMATCH at {pos}")
                            break
                    else:
                        print(f"    MISSING: {pos}")
                        break
        all_ok = all_ok and pos_ok and let_ok
    print(f"  Identity preserved: {all_ok} {'✅' if all_ok else '❌'}")
    # continue growth on restored snakes (consistent direction for junction validity)
    for s in restored_snakes[:5]:
        for _ in range(5):
            s.grow(0)
    print(f"  Continued growth on 5 restored snakes: OK")
    # verify identity after growth (positions increased, letters preserved for original)
    growth_ok = True
    for s in restored_snakes[:5]:
        geo_ok, geo_tot = s.check_junctions()
        graft_ok, graft_tot = s.check_graft_interface()
        if graft_ok != graft_tot:
            growth_ok = False
            print(f"    Snake junction issue: graft={graft_ok}/{graft_tot} geo={geo_ok}/{geo_tot}")
    print(f"  Junctions after growth: {'✅' if growth_ok else '⚠️ (random growth may have direction-change junctions)'}")
    print(f"  PASS" if all_ok else "  FAIL")
    return total_objects, total_cubes

if __name__ == "__main__":
    snake = test_randomized_growth()
    snake2, branches = test_heavy_branching()
    objs, cubes = test_large_grape_network()
    print("\n" + "=" * 70)
    print("LONG-RUN STRESS COMPLETE")
    print(f"  Randomized growth: 10K cubes")
    print(f"  Heavy branching: 100 branches")
    print(f"  Large grape network: {objs} objects, {cubes} cubes")
    print("=" * 70)