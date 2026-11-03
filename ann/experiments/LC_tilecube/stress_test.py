"""Structural Stress Test: repeated graft, multi-branch, multi-fruit, mixed identity, lifecycle, contract regression."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake, seed_rng
from fruit_graft import Fruit, FruitFormation, GraftEngine

def check_all(snake, label=""):
    """Check identity, junction (both), boundary, connectivity."""
    geo_ok, geo_tot = snake.check_junctions() if hasattr(snake, 'check_junctions') else (0, 0)
    graft_ok, graft_tot = snake.check_graft_interface() if hasattr(snake, 'check_graft_interface') else (0, 0)
    in_domain = all(
        (p[0] // snake.domain_size, p[1] // snake.domain_size, p[2] // snake.domain_size) == snake.current_domain
        for p in snake.positions
    )
    return {
        'positions': len(snake.positions),
        'geo': (geo_ok, geo_tot),
        'graft': (graft_ok, graft_tot),
        'in_domain': in_domain,
        'visited_ok': len(snake.visited) == len(snake.positions),
    }

def test_repeated_graft():
    print("=" * 70)
    print("TEST 1: Repeated Graft (cut+graft 5 rounds)")
    print("=" * 70)
    src = Snake("rg_src", mode='chain', domain_size=5000)
    dst = Snake("rg_dst", mode='chain', domain_size=5000)
    for _ in range(100):
        src.grow(0)
    for _ in range(50):
        dst.grow(2)
    rounds_ok = 0
    for rnd in range(5):
        # regrow src if empty
        if len(src.positions) < 10:
            src = Snake(f"rg_src_r{rnd}", mode='chain', domain_size=5000)
            src.start_pos = (100 + rnd*50, 100, 100)
            src.current_domain = (0, 0, 0)
            for _ in range(50):
                src.grow(0)
        # find compatible graft
        base = dst.positions[-1]
        free_dirs = []
        for d in range(6):
            dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
            c = (base[0]+dx, base[1]+dy, base[2]+dz)
            if c not in dst.visited:
                free_dirs.append((d, c))
        grafted = False
        for cut in range(1, max(2, len(src.positions))):
            sa = src.positions[cut]
            for d, c in free_dirs:
                da = None
                for k in range(3):
                    if base[k] != c[k]:
                        da = k
                        break
                if da is not None:
                    la = dst.letters[base]
                    lb = src.letters[sa]
                    if da < len(la) and da < len(lb) and la[da][1] == lb[da][1]:
                        r = GraftEngine.graft(src, cut, dst, None, interface_check=False)
                        if r.success:
                            grafted = True
                            break
            if grafted:
                break
        if not grafted:
            # grow src more and retry
            for _ in range(20):
                src.grow(0)
            print(f"  Round {rnd+1}: no compatible graft (src={len(src.positions)}, dst={len(dst.positions)})")
            continue
        # fix directions
        src.directions = [None] + [None] * (len(src.positions) - 1)
        dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
        info = check_all(dst)
        ok = (info['graft'][0] == info['graft'][1] and info['in_domain'] and info['visited_ok'])
        rounds_ok += 1 if ok else 0
        print(f"  Round {rnd+1}: grafted {len(r.moved_positions)} cubes → dst={len(dst.positions)} graft_iface={info['graft'][0]}/{info['graft'][1]} {'✅' if ok else '❌'}")
    # grow src for next round
    for _ in range(20):
        src.grow(0)
    print(f"  Rounds passed: {rounds_ok}/5")
    print(f"  PASS" if rounds_ok >= 3 else "  FAIL")

def test_multi_branch():
    print("\n" + "=" * 70)
    print("TEST 2: Multi-Branch (5 branches from different positions)")
    print("=" * 70)
    snake = Snake("mb", mode='chain', domain_size=100)
    for _ in range(30):
        snake.grow(0)  # main +X
    branches = []
    for i in [5, 10, 15, 20, 25]:
        bp = snake.positions[i]
        branch = Snake(f"mb_b{i}", mode='chain', domain_size=100)
        branch.start_pos = (bp[0], bp[1] + 1, bp[2])
        branch.current_domain = (bp[0] // 100, (bp[1]+1) // 100, bp[2] // 100)
        for _ in range(8):
            branch.grow(2)  # +Y
        branches.append(branch)
    all_ok = True
    for bi, br in enumerate(branches):
        info = check_all(br)
        ok = (info['geo'][0] == info['geo'][1] and info['in_domain'] and info['visited_ok'])
        all_ok = all_ok and ok
        print(f"  Branch {bi+1} (from pos { [5,10,15,20,25][bi] }): {info['positions']} cubes, geo={info['geo'][0]}/{info['geo'][1]}, domain={info['in_domain']} {'✅' if ok else '❌'}")
    # main snake still valid
    info_main = check_all(snake)
    main_ok = (info_main['geo'][0] == info_main['geo'][1])
    print(f"  Main snake: {info_main['positions']} cubes, geo={info_main['geo'][0]}/{info_main['geo'][1]} {'✅' if main_ok else '❌'}")
    print(f"  PASS" if (all_ok and main_ok) else "  FAIL")

def test_multi_fruit():
    print("\n" + "=" * 70)
    print("TEST 3: Multi-Fruit (5 fruits from different positions)")
    print("=" * 70)
    snake = Snake("mf", mode='chain', domain_size=100)
    for _ in range(40):
        snake.grow(0)
    fruits = []
    for i in range(5):
        start = i * 7
        end = start + 5
        if end > len(snake.positions):
            break
        fruit, _ = FruitFormation.cut_and_remove(snake, start, end)
        if fruit:
            fruit.layer_grow(1)
            fruits.append(fruit)
    all_ok = True
    for fi, f in enumerate(fruits):
        # check fruit identity
        id_ok = (f.growth_mode == 'onion' and len(f.positions) > len(f.origin_positions))
        # check no overlap between fruits
        all_ok = all_ok and id_ok
        print(f"  Fruit {fi+1}: id={f.fruit_id}, origin={len(f.origin_positions)}, after_layer1={len(f.positions)}, mode={f.growth_mode} {'✅' if id_ok else '❌'}")
    # check distinct ids
    ids = [f.fruit_id for f in fruits]
    distinct = len(set(ids)) == len(ids)
    print(f"  Distinct ids: {len(set(ids))}/{len(ids)} {'✅' if distinct else '❌'}")
    # check no merge (fruits are independent)
    no_merge = all(f.merged == False for f in fruits)
    print(f"  No merge: {no_merge} {'✅' if no_merge else '❌'}")
    print(f"  PASS" if (all_ok and distinct and no_merge) else "  FAIL")

def test_mixed_identity():
    print("\n" + "=" * 70)
    print("TEST 4: Mixed Identity (3 seeds in one network)")
    print("=" * 70)
    seeds = ["alpha", "beta", "gamma"]
    snakes = []
    for seed in seeds:
        s = Snake(seed, mode='chain', domain_size=200)
        for _ in range(20):
            s.grow(0)
        snakes.append(s)
    # graft seed1 → seed2
    src, dst = snakes[0], snakes[1]
    base = dst.positions[-1]
    free_dirs = []
    for d in range(6):
        dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
        c = (base[0]+dx, base[1]+dy, base[2]+dz)
        if c not in dst.visited:
            free_dirs.append((d, c))
    grafted = False
    for cut in range(1, len(src.positions)):
        sa = src.positions[cut]
        for d, c in free_dirs:
            da = None
            for k in range(3):
                if base[k] != c[k]:
                    da = k
                    break
            if da is not None:
                la = dst.letters[base]
                lb = src.letters[sa]
                if da < len(la) and da < len(lb) and la[da][1] == lb[da][1]:
                    r = GraftEngine.graft(src, cut, dst, None, interface_check=False)
                    if r.success:
                        grafted = True
                        break
        if grafted:
            break
    if grafted:
        src.directions = [None] + [None] * (len(src.positions) - 1)
        dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
        info = check_all(dst)
        ok = (info['graft'][0] == info['graft'][1] and info['in_domain'] and info['visited_ok'])
        print(f"  Grafted {seeds[0]}→{seeds[1]}: {len(dst.positions)} cubes, graft_iface={info['graft'][0]}/{info['graft'][1]} {'✅' if ok else '❌'}")
        # identity: src seed preserved in moved segment
        moved_in_dst = [p for p in r.moved_positions if p in dst.visited]
        print(f"  Moved segment in dst: {len(moved_in_dst)}/{len(r.moved_positions)}")
        # third seed independent
        info3 = check_all(snakes[2])
        ok3 = (info3['geo'][0] == info3['geo'][1])
        print(f"  Seed3 ({seeds[2]}) independent: {info3['positions']} cubes, geo={info3['geo'][0]}/{info3['geo'][1]} {'✅' if ok3 else '❌'}")
        print(f"  PASS" if (ok and ok3) else "  FAIL")
    else:
        print("  No compatible graft found (may happen with different seeds)")
        # still check independence
        info3 = check_all(snakes[2])
        print(f"  Seed3 independent: {info3['positions']} cubes, geo={info3['geo'][0]}/{info3['geo'][1]}")

def test_lifecycle_stress():
    print("\n" + "=" * 70)
    print("TEST 5: Lifecycle Stress (Save/Restore/Grow/Graft × 3 rounds)")
    print("=" * 70)
    snake = Snake("ls", mode='chain', domain_size=200)
    for _ in range(20):
        snake.grow(0)
    rounds_ok = 0
    for rnd in range(3):
        # save
        state = snake.state_dict()
        # restore
        snake_r = Snake("ls", mode='chain', domain_size=200)
        snake_r.restore(state)
        pos_match = (snake.positions == snake_r.positions)
        let_match = (snake.letters == snake_r.letters)
        # grow
        for _ in range(5):
            snake_r.grow(2)
        info = check_all(snake_r)
        ok = (pos_match and let_match and info['geo'][0] == info['geo'][1] and info['in_domain'])
        rounds_ok += 1 if ok else 0
        print(f"  Round {rnd+1}: restore pos={pos_match} letters={let_match} grow={len(snake_r.positions)} geo={info['geo'][0]}/{info['geo'][1]} domain={info['in_domain']} visited={info['visited_ok']} {'✅' if ok else '❌'}")
        snake = snake_r
    print(f"  Rounds passed: {rounds_ok}/3")
    print(f"  PASS" if rounds_ok == 3 else "  FAIL")

def test_contract_regression():
    print("\n" + "=" * 70)
    print("TEST 6: Contract Regression (geometric vs graft not mixed)")
    print("=" * 70)
    # Pure snake: geometric should pass, graft should also pass (subset)
    snake = Snake("cr", mode='chain', domain_size=100)
    for _ in range(30):
        snake.grow(0)
    geo_ok, geo_tot = snake.check_junctions()
    graft_ok, graft_tot = snake.check_graft_interface()
    pure_ok = (geo_ok == geo_tot and graft_ok == graft_tot)
    print(f"  Pure snake: geo={geo_ok}/{geo_tot}, graft={graft_ok}/{graft_tot} {'✅' if pure_ok else '❌'}")
    # Geometric ≤ Graft (geometric is stricter, so geo_ok ≤ graft_ok)
    subset_ok = (geo_ok <= graft_ok and geo_tot == graft_tot)
    print(f"  Geometric ≤ Graft (stricter): {subset_ok} {'✅' if subset_ok else '❌'}")
    # Grafted snake: graft should pass, geometric may fail at connection
    src = Snake("crs", mode='chain', domain_size=1000)
    dst = Snake("crd", mode='chain', domain_size=1000)
    for _ in range(20):
        src.grow(0)
    for _ in range(10):
        dst.grow(2)
    base = dst.positions[-1]
    free_dirs = []
    for d in range(6):
        dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
        c = (base[0]+dx, base[1]+dy, base[2]+dz)
        if c not in dst.visited:
            free_dirs.append((d, c))
    grafted = False
    for cut in range(1, len(src.positions)):
        sa = src.positions[cut]
        for d, c in free_dirs:
            da = None
            for k in range(3):
                if base[k] != c[k]:
                    da = k
                    break
            if da is not None:
                la = dst.letters[base]
                lb = src.letters[sa]
                if da < len(la) and da < len(lb) and la[da][1] == lb[da][1]:
                    r = GraftEngine.graft(src, cut, dst, None, interface_check=False)
                    if r.success:
                        grafted = True
                        break
        if grafted:
            break
    if grafted:
        src.directions = [None] + [None] * (len(src.positions) - 1)
        dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
        geo_ok2, geo_tot2 = dst.check_junctions()
        graft_ok2, graft_tot2 = dst.check_graft_interface()
        # graft should pass all
        graft_all = (graft_ok2 == graft_tot2)
        # geometric may fail at connection (≤ graft)
        geo_le_graft = (geo_ok2 <= graft_ok2)
        print(f"  Grafted: geo={geo_ok2}/{geo_tot2}, graft={graft_ok2}/{graft_tot2}")
        print(f"  Graft all pass: {graft_all} {'✅' if graft_all else '❌'}")
        print(f"  Geo ≤ Graft: {geo_le_graft} {'✅' if geo_le_graft else '❌'}")
        print(f"  PASS" if (pure_ok and subset_ok and graft_all and geo_le_graft) else "  FAIL")
    else:
        print("  No compatible graft (skip)")
        print(f"  PASS" if (pure_ok and subset_ok) else "  FAIL")

if __name__ == "__main__":
    test_repeated_graft()
    test_multi_branch()
    test_multi_fruit()
    test_mixed_identity()
    test_lifecycle_stress()
    test_contract_regression()
    print("\n" + "=" * 70)
    print("STRUCTURAL STRESS TEST COMPLETE")
    print("=" * 70)