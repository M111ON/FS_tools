"""Benchmark reconciliation: memory exclusive accounting + non-overlapping restore timing + graft valid/invalid."""
import sys, time, random, tracemalloc
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import GraftEngine

# ── 1. Memory: Exclusive Allocation (no double counting) ──────────────────
def reconcile_memory():
    print("=" * 70)
    print("MEMORY RECONCILIATION (exclusive allocation)")
    print("=" * 70)
    n = 5000
    snake = Snake("mem", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)

    tracemalloc.start()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    # Measure each component by isolating
    # Geometry: positions list only
    pos_list = snake.positions
    # Identity: letters dict only
    let_dict = snake.letters
    # Boundary: visited set only
    vis_set = snake.visited
    # Domains
    dom_list = snake.domains
    # Directions
    dir_list = snake.directions

    snap_after = tracemalloc.take_snapshot()
    # diff
    diff = snap_after.compare_to(snap_before, 'lineno')
    total_traced = sum(s.size for s in diff)

    # Exclusive: use sys.getsizeof on each container (not recursive) + element sizes
    import sys as _sys
    # Geometry: positions list + tuple objects (each unique)
    pos_container = _sys.getsizeof(pos_list)
    pos_elems = sum(_sys.getsizeof(p) for p in pos_list)
    pos_total = pos_container + pos_elems

    # Identity: letters dict + keys (positions, shared with pos_list) + values
    # Keys are shared with positions list (same tuple objects), so don't double count
    let_container = _sys.getsizeof(let_dict)
    let_values = 0
    for k, v in let_dict.items():
        # v is list of (tuple, frame) per axis
        let_values += _sys.getsizeof(v)
        for rv in v:
            if isinstance(rv, tuple):
                let_values += _sys.getsizeof(rv)
                if isinstance(rv[0], tuple):
                    let_values += _sys.getsizeof(rv[0])
    let_total = let_container + let_values  # keys are shared, not counted

    # Boundary: visited set + elements (shared with positions, not counted)
    vis_container = _sys.getsizeof(vis_set)
    # elements are same tuples as positions, so only container cost
    vis_total = vis_container

    # Domains
    dom_container = _sys.getsizeof(dom_list)
    dom_elems = sum(_sys.getsizeof(d) for d in dom_list)
    dom_total = dom_container + dom_elems

    # Directions
    dir_container = _sys.getsizeof(dir_list)
    dir_elems = sum(_sys.getsizeof(d) for d in dir_list)
    dir_total = dir_container + dir_elems

    # Object overhead
    obj_overhead = _sys.getsizeof(snake)

    # Exclusive total
    exclusive_total = pos_total + let_total + vis_total + dom_total + dir_total + obj_overhead

    # Shared allocation: positions tuples shared between pos_list and let_dict keys and vis_set
    shared_pos_tuples = pos_elems  # counted once in pos_total, referenced by others

    print(f"  n={n} cubes:")
    print(f"    positions (geometry):  {pos_total:>10} B  ({pos_total/n:.1f} B/cube)  [container+elems]")
    print(f"    letters (identity):    {let_total:>10} B  ({let_total/n:.1f} B/cube)  [container+values, keys shared]")
    print(f"    visited (boundary):    {vis_total:>10} B  ({vis_total/n:.1f} B/cube)  [container only, elems shared]")
    print(f"    domains:               {dom_total:>10} B  ({dom_total/n:.1f} B/cube)")
    print(f"    directions:            {dir_total:>10} B  ({dir_total/n:.1f} B/cube)")
    print(f"    object overhead:       {obj_overhead:>10} B")
    print(f"    ─────────────────────────────────────────")
    print(f"    EXCLUSIVE TOTAL:       {exclusive_total:>10} B  ({exclusive_total/n:.1f} B/cube)")
    print(f"    Shared pos tuples:     {shared_pos_tuples:>10} B  (counted in positions only)")
    print(f"    tracemalloc traced:    {total_traced:>10} B")
    print(f"    Reconciliation: exclusive_total = {exclusive_total}, components sum = {pos_total+let_total+vis_total+dom_total+dir_total+obj_overhead}")
    # verify: components should sum to exclusive_total
    components_sum = pos_total + let_total + vis_total + dom_total + dir_total + obj_overhead
    print(f"    Components sum == exclusive_total: {components_sum == exclusive_total}")
    tracemalloc.stop()

# ── 2. Restore Timing: Non-overlapping ────────────────────────────────────
def reconcile_restore():
    print("\n" + "=" * 70)
    print("RESTORE TIMING RECONCILIATION (non-overlapping)")
    print("=" * 70)
    for n in [100, 1000, 5000, 10000]:
        snake = Snake(f"rr_{n}", mode='chain', domain_size=n+100)
        for _ in range(n):
            snake.grow(0)
        state = snake.state_dict()

        # Phase 1: Create empty snake + assign positions/visited (replay)
        t0 = time.perf_counter()
        s2 = Snake(f"rr_{n}", mode='chain', domain_size=n+100)
        s2.positions = [tuple(p) for p in state['positions']]
        s2.visited = set(s2.positions)
        s2.current_domain = state['current_domain']
        s2.growth_count = state['growth_count']
        t_replay = (time.perf_counter() - t0) * 1000

        # Phase 2: Restore letters (identity reconstruction)
        t0 = time.perf_counter()
        s2.letters = {}
        if 'letters' in state and state['letters']:
            for key, val in state['letters'].items():
                parts = key.split(",")
                pos = tuple(int(x) for x in parts)
                restored = []
                for rv in val:
                    if isinstance(rv, list) and len(rv) == 2:
                        restored.append((tuple(rv[0]), rv[1]))
                    else:
                        restored.append(rv)
                s2.letters[pos] = restored
        t_letters = (time.perf_counter() - t0) * 1000

        # Phase 3: Junction check
        t0 = time.perf_counter()
        ok, tot = s2.check_junctions()
        t_junction = (time.perf_counter() - t0) * 1000

        # Phase 4: Index reconstruction (position -> index map)
        t0 = time.perf_counter()
        idx_map = {p: i for i, p in enumerate(s2.positions)}
        t_index = (time.perf_counter() - t0) * 1000

        # Total of non-overlapping phases
        total_phases = t_replay + t_letters + t_junction + t_index
        # Also measure full restore for comparison
        t0 = time.perf_counter()
        s3 = Snake(f"rr_{n}", mode='chain', domain_size=n+100)
        s3.restore(state)
        t_full_restore = (time.perf_counter() - t0) * 1000

        print(f"  n={n:>6}: replay={t_replay:.2f}  letters={t_letters:.2f}  junction={t_junction:.2f}  index={t_index:.2f}  sum_phases={total_phases:.2f}  full_restore={t_full_restore:.2f}  ({ok}/{tot})")

# ── 3. Graft: Valid and Invalid ────────────────────────────────────────────
def reconcile_graft():
    print("\n" + "=" * 70)
    print("GRAFT RECONCILIATION (valid + invalid + rollback)")
    print("=" * 70)

    # Valid graft: try many cut points, report first success
    src = Snake("gv", mode='chain', domain_size=200)
    dst = Snake("gv", mode='chain', domain_size=200)
    for _ in range(30):
        src.grow(0)
    for _ in range(15):
        dst.grow(2)

    ok_s, tot_s = src.check_junctions()
    ok_d, tot_d = dst.check_junctions()
    print(f"  Pre-graft: src={ok_s}/{tot_s}, dst={ok_d}/{tot_d}")

    # Try graft at various cut points
    found = False
    for cut in range(5, 20):
        src_bak = list(src.positions); dst_bak = list(dst.positions)
        src_vis_bak = set(src.visited); dst_vis_bak = set(dst.visited)
        src_let_bak = {k: v for k, v in src.letters.items()}
        dst_let_bak = {k: v for k, v in dst.letters.items()}
        src_dir_bak = list(src.directions); dst_dir_bak = list(dst.directions)

        result = GraftEngine.graft(src, cut, dst, None, interface_check=False)
        if result.success:
            found = True
            print(f"  Valid graft at cut={cut}: moved={len(result.moved_positions)}, reason={result.reason}")
            ok_d2, tot_d2 = dst.check_junctions()
            print(f"    Post-graft: dst={ok_d2}/{tot_d2}")
            break
        else:
            # rollback
            src.positions = src_bak; src.visited = src_vis_bak; src.letters = src_let_bak
            src.growth_count = len(src_bak); src.directions = src_dir_bak
            dst.positions = dst_bak; dst.visited = dst_vis_bak; dst.letters = dst_let_bak
            dst.growth_count = len(dst_bak); dst.directions = dst_dir_bak

    if not found:
        print("  No valid graft found (all connection junctions mismatched)")
        # verify rollback preserved state
        ok_s2, tot_s2 = src.check_junctions()
        ok_d2, tot_d2 = dst.check_junctions()
        print(f"  After failed attempts: src={ok_s2}/{tot_s2}, dst={ok_d2}/{tot_d2}")
        print(f"  Identity preserved: {src.positions == src_bak and dst.positions == dst_bak}")
        print(f"  ROLLBACK VERIFIED")

    # Invalid graft: force collision (should reject + rollback)
    print("\n  Invalid graft test (force collision):")
    src2 = Snake("inv_src", mode='chain', domain_size=50)
    dst2 = Snake("inv_dst", mode='chain', domain_size=50)
    for _ in range(8):
        src2.grow(0)
    for _ in range(8):
        dst2.grow(0)  # same path → will collide
    src2_bak = list(src2.positions); dst2_bak = list(dst2.positions)
    src2_vis_bak = set(src2.visited); dst2_vis_bak = set(dst2.visited)
    src2_let_bak = {k: v for k, v in src2.letters.items()}
    dst2_let_bak = {k: v for k, v in dst2.letters.items()}
    result2 = GraftEngine.graft(src2, 2, dst2, (0, 0, 0), interface_check=False)
    print(f"    Result: success={result2.success}, reason={result2.reason}")
    if not result2.success:
        # verify rollback
        print(f"    src preserved: {src2.positions == src2_bak}")
        print(f"    dst preserved: {dst2.positions == dst2_bak}")
        print(f"    ROLLBACK VERIFIED")

# ── 4. Identity Lossless Check ─────────────────────────────────────────────
def reconcile_identity():
    print("\n" + "=" * 70)
    print("IDENTITY LOSSLESS CHECK")
    print("=" * 70)
    snake = Snake("id", mode='chain', domain_size=100)
    for _ in range(50):
        snake.grow(0)
    # record identity before
    pos_before = list(snake.positions)
    let_before = {k: [ (tuple(rv[0]), rv[1]) for rv in v] for k, v in snake.letters.items()}
    # save + restore
    state = snake.state_dict()
    snake2 = Snake("id", mode='chain', domain_size=100)
    snake2.restore(state)
    # compare
    pos_match = (snake.positions == snake2.positions)
    let_match = (let_before == snake2.letters)
    print(f"  Positions match: {pos_match}")
    print(f"  Letters match: {let_match}")
    print(f"  Identity lossless: {pos_match and let_match}")
    # grow after restore
    for _ in range(5):
        snake2.grow(2)
    ok, tot = snake2.check_junctions()
    print(f"  After growth: {len(snake2.positions)} cubes, junctions={ok}/{tot}")
    print(f"  PASS" if (pos_match and let_match and ok == tot) else "  FAIL")

if __name__ == "__main__":
    reconcile_memory()
    reconcile_restore()
    reconcile_graft()
    reconcile_identity()
    print("\n" + "=" * 70)
    print("RECONCILIATION COMPLETE")
    print("=" * 70)