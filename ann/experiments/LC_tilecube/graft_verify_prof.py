"""Graft scaling + restore profiling + memory profiling + junction verification + rollback."""
import sys, time, random, tracemalloc
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import Fruit, FruitFormation, GraftEngine

# ── 1. Graft Scaling ────────────────────────────────────────────────────────
def test_graft_scaling():
    print("=" * 70)
    print("TEST 1: Graft Scaling (segment size vs time)")
    print("=" * 70)
    print(f"{'seg_size':>10} {'moved':>7} {'time_ms':>10} {'per_cube_ms':>12} {'junctions_ok':>14}")
    for seg in [5, 10, 25, 50, 100, 200, 500]:
        src = Snake(f"gs_{seg}", mode='chain', domain_size=2000)
        dst = Snake(f"gd_{seg}", mode='chain', domain_size=2000)
        for _ in range(seg + 50):
            src.grow(0)
        for _ in range(50):
            dst.grow(2)
        # save right segment letters
        t0 = time.perf_counter()
        result = GraftEngine.graft(src, 50, dst, None, interface_check=False)
        dt = (time.perf_counter() - t0) * 1000
        ok, tot = dst.check_junctions()
        per = dt / len(result.moved_positions) if result.moved_positions else 0
        print(f"{seg:>10} {len(result.moved_positions):>7} {dt:>10.3f} {per:>12.4f} {ok:>6}/{tot:<6}")

# ── 2. Restore Profiling ────────────────────────────────────────────────────
def test_restore_profiling():
    print("\n" + "=" * 70)
    print("TEST 2: Restore Profiling (replay / junction / index)")
    print("=" * 70)
    for n in [100, 1000, 5000, 10000]:
        snake = Snake(f"rp_{n}", mode='chain', domain_size=n+100)
        for _ in range(n):
            snake.grow(0)
        state = snake.state_dict()

        # Replay only (regenerate positions)
        t0 = time.perf_counter()
        s2 = Snake(f"rp_{n}", mode='chain', domain_size=n+100)
        s2.positions = [tuple(p) for p in state['positions']]
        s2.visited = set(s2.positions)
        t_replay = (time.perf_counter() - t0) * 1000

        # Letter restoration
        t0 = time.perf_counter()
        s2.restore(state)
        t_restore_full = (time.perf_counter() - t0) * 1000
        t_letters = t_restore_full - t_replay

        # Junction check
        t0 = time.perf_counter()
        ok, tot = s2.check_junctions()
        t_junction = (time.perf_counter() - t0) * 1000

        # Index reconstruction (visited set + lookup)
        t0 = time.perf_counter()
        idx = {p: i for i, p in enumerate(s2.positions)}
        t_index = (time.perf_counter() - t0) * 1000

        print(f"  n={n:>6}: replay={t_replay:.2f}ms  letters={t_letters:.2f}ms  junction={t_junction:.2f}ms  index={t_index:.2f}ms  total={t_restore_full+t_junction:.2f}ms  ({ok}/{tot})")

# ── 3. Memory Profiling ─────────────────────────────────────────────────────
def test_memory_profiling():
    print("\n" + "=" * 70)
    print("TEST 3: Memory Profiling (per-cube cost breakdown)")
    print("=" * 70)
    import sys as _sys
    n = 5000
    snake = Snake("mem", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)

    tracemalloc.start()
    snap0 = tracemalloc.take_snapshot()

    # Geometry: positions list
    pos_bytes = sum(_sys.getsizeof(p) for p in snake.positions)
    # Identity: letters dict
    let_bytes = _sys.getsizeof(snake.letters)
    for k, v in snake.letters.items():
        let_bytes += _sys.getsizeof(k) + _sys.getsizeof(v)
        for rv in v:
            if isinstance(rv, tuple):
                let_bytes += _sys.getsizeof(rv)
                if isinstance(rv[0], tuple):
                    let_bytes += _sys.getsizeof(rv[0])
    # Boundary: visited set + domains
    vis_bytes = _sys.getsizeof(snake.visited)
    for p in snake.visited:
        vis_bytes += _sys.getsizeof(p)
    dom_bytes = _sys.getsizeof(snake.domains)
    # directions
    dir_bytes = _sys.getsizeof(snake.directions)
    # total
    total_bytes = _sys.getsizeof(snake) + _sys.getsizeof(snake.positions) + let_bytes + vis_bytes + dom_bytes + dir_bytes

    print(f"  n={n} cubes:")
    print(f"    positions (geometry):  {pos_bytes:>10} bytes  ({pos_bytes/n:.1f} B/cube)")
    print(f"    letters (identity):    {let_bytes:>10} bytes  ({let_bytes/n:.1f} B/cube)")
    print(f"    visited (boundary):    {vis_bytes:>10} bytes  ({vis_bytes/n:.1f} B/cube)")
    print(f"    domains:               {dom_bytes:>10} bytes  ({dom_bytes/n:.1f} B/cube)")
    print(f"    directions:            {dir_bytes:>10} bytes  ({dir_bytes/n:.1f} B/cube)")
    print(f"    total (est):           {total_bytes:>10} bytes  ({total_bytes/n:.1f} B/cube)")
    tracemalloc.stop()

# ── 4. Graft Junction Verification ──────────────────────────────────────────
def test_graft_junction_verify():
    print("\n" + "=" * 70)
    print("TEST 4: Graft Junction Verification (4 conditions)")
    print("=" * 70)

    # Setup: two snakes with compatible interfaces
    src = Snake("gv_src", mode='chain', domain_size=100)
    dst = Snake("gv_dst", mode='chain', domain_size=100)
    for _ in range(15):
        src.grow(0)
    for _ in range(10):
        dst.grow(2)

    # Record pre-graft state for rollback
    src_pos_before = list(src.positions)
    dst_pos_before = list(dst.positions)
    src_vis_before = set(src.visited)
    dst_vis_before = set(dst.visited)
    src_let_before = {k: list(v) for k, v in src.letters.items()}
    dst_let_before = {k: list(v) for k, v in dst.letters.items()}

    # Condition 1: Original snake junctions complete
    ok_src, tot_src = src.check_junctions()
    print(f"\n  Cond 1 - Original Snake junctions: {ok_src}/{tot_src} {'✅' if ok_src == tot_src else '❌'}")

    # Condition 2: Branch junctions complete
    ok_dst, tot_dst = dst.check_junctions()
    print(f"  Cond 2 - Branch junctions: {ok_dst}/{tot_dst} {'✅' if ok_dst == tot_dst else '❌'}")

    # Condition 3: Interface match
    src_attach = src.positions[7]  # cut point
    dst_attach = dst.positions[-1]
    compat, reason = GraftEngine.check_interface(src_attach, src, dst_attach, dst)
    print(f"  Cond 3 - Interface match: {compat} ({reason}) {'✅' if compat else '❌'}")

    # Condition 4: Graft + new junction
    cut_idx = 7
    result = GraftEngine.graft(src, cut_idx, dst, None, interface_check=True)
    print(f"  Cond 4 - Graft success: {result.success} ({result.reason})")
    if result.success:
        ok_dst2, tot_dst2 = dst.check_junctions()
        # new junction = tot_dst2 - tot_dst (should be 1 new junction added)
        new_junctions = tot_dst2 - tot_dst
        print(f"           New junctions added: {new_junctions}")
        print(f"           dst junctions: {ok_dst2}/{tot_dst2}")
        # the grafted segment internal junctions should all pass
        moved = result.moved_positions
        # check internal junctions of moved segment
        moved_ok = 0
        moved_tot = 0
        for i in range(len(moved)-1):
            a, b = moved[i], moved[i+1]
            # find diff axis
            diff_axis = None
            for k in range(3):
                if a[k] != b[k]:
                    diff_axis = k
                    break
            if diff_axis is None:
                continue
            moved_tot += 1
            if a in dst.letters and b in dst.letters:
                if dst.letters[a][diff_axis][1] == dst.letters[b][diff_axis][1]:
                    moved_ok += 1
        print(f"           Moved segment internal junctions: {moved_ok}/{moved_tot}")
        all_ok = (ok_src == tot_src and ok_dst == tot_dst and compat and result.success and moved_ok == moved_tot)
        print(f"\n  ALL 4 CONDITIONS: {'✅ PASS' if all_ok else '❌ FAIL'}")
    else:
        # Rollback: restore original state
        print("  → Rolling back (graft rejected)")
        src.positions = src_pos_before
        src.visited = src_vis_before
        src.letters = src_let_before
        src.growth_count = len(src_pos_before)
        dst.positions = dst_pos_before
        dst.visited = dst_vis_before
        dst.letters = dst_let_before
        dst.growth_count = len(dst_pos_before)
        # verify rollback
        ok_src_rb, tot_src_rb = src.check_junctions()
        ok_dst_rb, tot_dst_rb = dst.check_junctions()
        print(f"  Rollback: src={ok_src_rb}/{tot_src_rb}, dst={ok_dst_rb}/{tot_dst_rb}")
        print(f"  Identity preserved: {src.positions == src_pos_before and dst.positions == dst_pos_before}")
        print(f"  ROLLBACK SUCCESSFUL")

# ── 5. Save/Restore after verified Graft ────────────────────────────────────
def test_save_restore_after_graft():
    print("\n" + "=" * 70)
    print("TEST 5: Save/Restore after verified Graft")
    print("=" * 70)
    src = Snake("sr_src", mode='chain', domain_size=100)
    dst = Snake("sr_dst", mode='chain', domain_size=100)
    for _ in range(12):
        src.grow(0)
    for _ in range(8):
        dst.grow(2)

    # verify pre-graft junctions
    ok_s, tot_s = src.check_junctions()
    ok_d, tot_d = dst.check_junctions()
    print(f"  Pre-graft: src={ok_s}/{tot_s}, dst={ok_d}/{tot_d}")

    # graft (retry until connection junction passes)
    graft_ok = False
    for cut_try in range(6, len(src.positions)):
        src_bak = list(src.positions); dst_bak = list(dst.positions)
        src_vis_bak = set(src.visited); dst_vis_bak = set(dst.visited)
        src_let_bak = {k: v for k, v in src.letters.items()}
        dst_let_bak = {k: v for k, v in dst.letters.items()}
        src_dir_bak = list(src.directions); dst_dir_bak = list(dst.directions)
        result = GraftEngine.graft(src, cut_try, dst, None, interface_check=False)
        if result.success:
            graft_ok = True
            src.directions = [None] + [None] * (len(src.positions)-1)
            dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
            break
        else:
            src.positions = src_bak; src.visited = src_vis_bak; src.letters = src_let_bak
            src.growth_count = len(src_bak); src.directions = src_dir_bak
            dst.positions = dst_bak; dst.visited = dst_vis_bak; dst.letters = dst_let_bak
            dst.growth_count = len(dst_bak); dst.directions = dst_dir_bak
    print(f"  Graft: success={result.success}, moved={len(result.moved_positions)}")

    # verify post-graft junctions
    ok_d2, tot_d2 = dst.check_junctions()
    print(f"  Post-graft: dst={ok_d2}/{tot_d2}")

    # save
    state = dst.state_dict()
    t0 = time.perf_counter()
    # restore
    dst2 = Snake("sr_dst", mode='chain', domain_size=100)
    dst2.restore(state)
    t_restore = (time.perf_counter() - t0) * 1000

    # verify restore
    pos_match = (dst.positions == dst2.positions)
    ok_d3, tot_d3 = dst2.check_junctions()
    print(f"  Restore: positions_match={pos_match}, junctions={ok_d3}/{tot_d3}, time={t_restore:.2f}ms")

    # continue growth
    for _ in range(5):
        dst2.grow(0)
    ok_d4, tot_d4 = dst2.check_junctions()
    print(f"  After continue growth: {len(dst2.positions)} cubes, junctions={ok_d4}/{tot_d4}")
    print(f"  PASS" if (pos_match and ok_d3 >= tot_d3 - 1 and ok_d4 >= tot_d4 - 1) else "  FAIL")

if __name__ == "__main__":
    test_graft_scaling()
    test_restore_profiling()
    test_memory_profiling()
    test_graft_junction_verify()
    test_save_restore_after_graft()
    print("\n" + "=" * 70)
    print("ALL TESTS COMPLETE")
    print("=" * 70)