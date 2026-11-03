"""Compact Identity Integration: string encoding in Snake, Fruit, Graft, Save/Restore."""
import sys, time
sys.path.insert(0, '.')
from snake_sim import Snake, seed_rng
from fruit_graft import Fruit, FruitFormation, GraftEngine
from compact_identity import encode_string, decode_string

def compact_state_dict(snake):
    """Save snake state with string-encoded letters."""
    state = snake.state_dict()
    # encode letters as strings
    compact_letters = {}
    for pos, val in snake.letters.items():
        key = ",".join(str(x) for x in pos)
        compact_letters[key] = encode_string(val)
    state['letters'] = compact_letters
    state['compact'] = 'string'
    return state

def compact_restore(snake, state):
    """Restore snake from compact (string-encoded) state."""
    snake.positions = [tuple(p) for p in state['positions']]
    snake.visited = set(snake.positions)
    snake.current_domain = state['current_domain']
    snake.growth_count = state['growth_count']
    snake.directions = state['directions']
    snake.boundary_blocks = state.get('boundary_blocks', 0)
    snake.seed = state['seed']
    snake.mode = state['mode']
    snake.letters = {}
    if state.get('compact') == 'string':
        for key, val in state['letters'].items():
            parts = key.split(",")
            pos = tuple(int(x) for x in parts)
            snake.letters[pos] = decode_string(val)
    else:
        # fallback to tuple format
        for key, val in state['letters'].items():
            parts = key.split(",")
            pos = tuple(int(x) for x in parts)
            restored = []
            for rv in val:
                if isinstance(rv, list) and len(rv) == 2:
                    restored.append((tuple(rv[0]), rv[1]))
                else:
                    restored.append(rv)
            snake.letters[pos] = restored

def run_compact_e2e():
    print("=" * 70)
    print("COMPACT IDENTITY INTEGRATION — END-TO-END")
    print("=" * 70)

    # Step 1: Create snake, grow, compact save/restore
    print("\n[1] Snake: grow 20, compact save/restore")
    snake = Snake("ci", mode='chain', domain_size=50)
    for _ in range(20):
        snake.grow(0)
    ok, tot = snake.check_graft_interface()
    print(f"  Grew: {len(snake.positions)} cubes, graft_iface={ok}/{tot}")
    # compact save
    state = compact_state_dict(snake)
    mem_saved = sum(len(v) for v in state['letters'].values())
    print(f"  Compact state: {mem_saved} bytes for letters ({mem_saved/len(snake.positions):.1f} B/cube)")
    # restore
    snake_r = Snake("ci", mode='chain', domain_size=50)
    t0 = time.perf_counter()
    compact_restore(snake_r, state)
    t_restore = (time.perf_counter() - t0) * 1000
    pos_match = (snake.positions == snake_r.positions)
    let_match = (snake.letters == snake_r.letters)
    ok_r, tot_r = snake_r.check_graft_interface()
    print(f"  Restore: positions={pos_match}, letters={let_match}, time={t_restore:.2f}ms, graft_iface={ok_r}/{tot_r}")
    # continue growth
    for _ in range(5):
        snake_r.grow(2)
    ok_g, tot_g = snake_r.check_graft_interface()
    print(f"  After growth: {len(snake_r.positions)} cubes, graft_iface={ok_g}/{tot_g}")

    # Step 2: Fruit with compact encoding
    print("\n[2] Fruit: create from snake, compact save/restore")
    fruit_snake = Snake("fci", mode='chain', domain_size=50)
    for _ in range(15):
        fruit_snake.grow(0)
    fruit, _ = FruitFormation.cut_and_remove(fruit_snake, 5, 12)
    fruit.layer_grow(1)
    # compact fruit letters
    fruit_compact_letters = {}
    for pos, val in fruit.letters.items():
        key = ",".join(str(x) for x in pos)
        fruit_compact_letters[key] = encode_string(val)
    fruit_mem = sum(len(v) for v in fruit_compact_letters.values())
    print(f"  Fruit: {len(fruit.positions)} cubes, compact letters={fruit_mem} bytes ({fruit_mem/len(fruit.positions):.1f} B/cube)")
    # restore fruit
    fruit_r = Fruit(fruit.origin_snake_id, fruit.origin_positions, fruit.seed, fruit.domain_size)
    fruit_r.fruit_id = fruit.fruit_id
    fruit_r.positions = [tuple(p) for p in fruit.positions]
    fruit_r.visited = set(fruit_r.positions)
    fruit_r.current_layer = fruit.current_layer
    fruit_r.growth_mode = fruit.growth_mode
    fruit_r.letters = {}
    for key, val in fruit_compact_letters.items():
        parts = key.split(",")
        pos = tuple(int(x) for x in parts)
        fruit_r.letters[pos] = decode_string(val)
    pos_match_f = (fruit.positions == fruit_r.positions)
    let_match_f = (fruit.letters == fruit_r.letters)
    print(f"  Restore: positions={pos_match_f}, letters={let_match_f}")

    # Step 3: Graft with compact encoding
    print("\n[3] Graft: compact save/restore after graft")
    g_src = Snake("gci_src", mode='chain', domain_size=1000)
    g_dst = Snake("gci_dst", mode='chain', domain_size=1000)
    for _ in range(30):
        g_src.grow(0)
    for _ in range(15):
        g_dst.grow(2)
    # find compatible graft
    base = g_dst.positions[-1]
    free_dirs = []
    for d in range(6):
        dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
        candidate = (base[0]+dx, base[1]+dy, base[2]+dz)
        if candidate not in g_dst.visited:
            free_dirs.append((d, candidate))
    grafted = False
    for cut in range(1, len(g_src.positions)):
        src_attach = g_src.positions[cut]
        for d, candidate in free_dirs:
            diff_axis = None
            for k in range(3):
                if base[k] != candidate[k]:
                    diff_axis = k
                    break
            if diff_axis is not None:
                la = g_dst.letters[base]
                lb = g_src.letters[src_attach]
                if diff_axis < len(la) and diff_axis < len(lb) and la[diff_axis][1] == lb[diff_axis][1]:
                    result = GraftEngine.graft(g_src, cut, g_dst, None, interface_check=False)
                    if result.success:
                        grafted = True
                        break
        if grafted:
            break
    if grafted:
        ok_g, tot_g = g_dst.check_graft_interface()
        print(f"  Grafted: {len(g_dst.positions)} cubes, graft_iface={ok_g}/{tot_g}")
        # compact save
        state_g = compact_state_dict(g_dst)
        g_mem = sum(len(v) for v in state_g['letters'].values())
        print(f"  Compact state: {g_mem} bytes ({g_mem/len(g_dst.positions):.1f} B/cube)")
        # restore
        g_r = Snake("gci_dst", mode='chain', domain_size=1000)
        t0 = time.perf_counter()
        compact_restore(g_r, state_g)
        t_r = (time.perf_counter() - t0) * 1000
        pos_m = (g_dst.positions == g_r.positions)
        let_m = (g_dst.letters == g_r.letters)
        ok_r, tot_r = g_r.check_graft_interface()
        print(f"  Restore: positions={pos_m}, letters={let_m}, time={t_r:.2f}ms, graft_iface={ok_r}/{tot_r}")
        # continue growth
        for _ in range(5):
            g_r.grow(0)
        ok_g2, tot_g2 = g_r.check_graft_interface()
        print(f"  After growth: {len(g_r.positions)} cubes, graft_iface={ok_g2}/{tot_g2}")

    # Step 4: Full end-to-end with compact encoding
    print("\n[4] Full E2E: Snake → Fruit → Graft → Compact Save → Restore → Grow")
    s = Snake("e2e_ci", mode='chain', domain_size=80)
    for _ in range(25):
        s.grow(0)
    # fruit
    f, _ = FruitFormation.cut_and_remove(s, 8, 16)
    f.layer_grow(1)
    # graft (try)
    s2 = Snake("e2e_ci2", mode='chain', domain_size=80)
    for _ in range(10):
        s2.grow(2)
    base2 = s2.positions[-1]
    free2 = []
    for d in range(6):
        dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
        c = (base2[0]+dx, base2[1]+dy, base2[2]+dz)
        if c not in s2.visited:
            free2.append((d, c))
    grafted2 = False
    for cut in range(1, len(s.positions)):
        sa = s.positions[cut]
        for d, c in free2:
            da = None
            for k in range(3):
                if base2[k] != c[k]:
                    da = k
                    break
            if da is not None:
                la = s2.letters[base2]
                lb = s.letters[sa]
                if da < len(la) and da < len(lb) and la[da][1] == lb[da][1]:
                    r = GraftEngine.graft(s, cut, s2, None, interface_check=False)
                    if r.success:
                        grafted2 = True
                        break
        if grafted2:
            break
    if grafted2:
        # compact save all
        st_s = compact_state_dict(s)
        st_s2 = compact_state_dict(s2)
        st_f = {}
        for pos, val in f.letters.items():
            st_f[",".join(str(x) for x in pos)] = encode_string(val)
        # restore
        s_r = Snake("e2e_ci", mode='chain', domain_size=80)
        compact_restore(s_r, st_s)
        s2_r = Snake("e2e_ci2", mode='chain', domain_size=80)
        compact_restore(s2_r, st_s2)
        f_r = Fruit(f.origin_snake_id, f.origin_positions, f.seed, f.domain_size)
        f_r.fruit_id = f.fruit_id
        f_r.positions = [tuple(p) for p in f.positions]
        f_r.visited = set(f_r.positions)
        f_r.current_layer = f.current_layer
        f_r.growth_mode = f.growth_mode
        f_r.letters = {}
        for key, val in st_f.items():
            parts = key.split(",")
            pos = tuple(int(x) for x in parts)
            f_r.letters[pos] = decode_string(val)
        # verify
        ok_s, tot_s = s_r.check_graft_interface()
        ok_s2, tot_s2 = s2_r.check_graft_interface()
        pos_s = (s.positions == s_r.positions)
        pos_s2 = (s2.positions == s2_r.positions)
        pos_f = (f.positions == f_r.positions)
        let_s = (s.letters == s_r.letters)
        let_s2 = (s2.letters == s2_r.letters)
        let_f = (f.letters == f_r.letters)
        print(f"  Snake:  positions={pos_s}, letters={let_s}, graft_iface={ok_s}/{tot_s}")
        print(f"  Snake2: positions={pos_s2}, letters={let_s2}, graft_iface={ok_s2}/{tot_s2}")
        print(f"  Fruit:  positions={pos_f}, letters={let_f}")
        # continue growth
        for _ in range(3):
            s_r.grow(0)
            s2_r.grow(2)
        ok_sg, tot_sg = s_r.check_graft_interface()
        ok_s2g, tot_s2g = s2_r.check_graft_interface()
        print(f"  After growth: snake={len(s_r.positions)} ({ok_sg}/{tot_sg}), snake2={len(s2_r.positions)} ({ok_s2g}/{tot_s2g})")
        all_ok = (pos_s and pos_s2 and pos_f and let_s and let_s2 and let_f and
                  ok_s == tot_s and ok_s2 == tot_s2 and ok_sg == tot_sg and ok_s2g == tot_s2g)
        print(f"\n  ALL CHECKS: {'✅ PASS' if all_ok else '❌ FAIL'}")
    else:
        print("  No compatible graft found")

if __name__ == "__main__":
    run_compact_e2e()
    print("\n" + "=" * 70)
    print("COMPACT IDENTITY INTEGRATION COMPLETE")
    print("=" * 70)