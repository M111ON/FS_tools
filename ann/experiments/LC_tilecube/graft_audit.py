"""Graft Junction Audit: guaranteed compatible fixture, scale 5/50/250/500/2500."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import GraftEngine

def make_fixture(n_src, n_dst, seed):
    """Create src and dst with guaranteed interface-compatible graft point.
    Strategy: grow both from same seed, same direction, offset Y.
    Then search for cut point where connection junction passes."""
    src = Snake(f"audit_{seed}", mode='chain', domain_size=100000)
    dst = Snake(f"audit_{seed}", mode='chain', domain_size=100000)
    for _ in range(n_src):
        src.grow(0)  # +X
    dst.start_pos = (0, 100, 0)  # offset in Y to avoid collision
    dst.current_domain = (0, 0, 0)
    for _ in range(n_dst):
        dst.grow(0)  # +X
    return src, dst

def find_compatible_graft(src, dst):
    """Try all cut points, return first that succeeds (connection junction passes)."""
    for cut in range(1, len(src.positions)):
        # save state
        src_bak = list(src.positions); dst_bak = list(dst.positions)
        src_vis_bak = set(src.visited); dst_vis_bak = set(dst.visited)
        src_let_bak = {k: v for k, v in src.letters.items()}
        dst_let_bak = {k: v for k, v in dst.letters.items()}
        src_dir_bak = list(src.directions); dst_dir_bak = list(dst.directions)

        result = GraftEngine.graft(src, cut, dst, None, interface_check=False)
        if result.success:
            src.directions = [None] + [None] * (len(src.positions) - 1)
            dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
            return cut, result, True
        else:
            # rollback
            src.positions = src_bak; src.visited = src_vis_bak; src.letters = src_let_bak
            src.growth_count = len(src_bak); src.directions = src_dir_bak
            dst.positions = dst_bak; dst.visited = dst_vis_bak; dst.letters = dst_let_bak
            dst.growth_count = len(dst_bak); dst.directions = dst_dir_bak
    return None, None, False

def audit_scale(n_src, n_dst):
    src, dst = make_fixture(n_src, n_dst, f"scale_{n_src}")
    ok_s, tot_s = src.check_junctions()
    ok_d, tot_d = dst.check_junctions()

    t0 = time.perf_counter()
    cut, result, success = find_compatible_graft(src, dst)
    dt = (time.perf_counter() - t0) * 1000

    if success:
        ok_s2, tot_s2 = src.check_junctions()
        ok_d2, tot_d2 = dst.check_junctions()
        # connection junction check
        moved = result.moved_positions
        conn_junction_ok = (ok_d2 == tot_d2)  # all pass including connection
        # verify rollback not needed
        print(f"  {n_src:>6}->{n_dst:<6} cut={cut:>5} moved={len(moved):>6} time={dt:>8.2f}ms  src={ok_s2}/{tot_s2}  dst={ok_d2}/{tot_d2}  conn={'✅' if conn_junction_ok else '❌'}  {'✅ GRAFT OK' if conn_junction_ok else '❌ CONN FAIL'}")
        return True, conn_junction_ok
    else:
        # all cut points failed → verify rollback
        ok_s2, tot_s2 = src.check_junctions()
        ok_d2, tot_d2 = dst.check_junctions()
        print(f"  {n_src:>6}->{n_dst:<6} NO VALID GRAFT  time={dt:>8.2f}ms  src={ok_s2}/{tot_s2}  dst={ok_d2}/{tot_d2}  rollback={'✅' if (ok_s2==tot_s2 and ok_d2==tot_d2) else '❌'}")
        return False, False

if __name__ == "__main__":
    print("=" * 70)
    print("GRAFT JUNCTION AUDIT")
    print("=" * 70)
    print(f"{'scale':>14} {'cut':>5} {'moved':>6} {'time_ms':>8} {'src_j':>8} {'dst_j':>8} {'conn':>5} {'status':>12}")

    success_count = 0
    total_count = 0
    for n in [5, 50, 250, 500, 2500]:
        s, conn = audit_scale(n, n // 2)
        total_count += 1
        if s:
            success_count += 1

    print(f"\n  Successful grafts: {success_count}/{total_count}")

    print("\n" + "=" * 70)
    print("GRAFT JUNCTION AUDIT COMPLETE")
    print("=" * 70)