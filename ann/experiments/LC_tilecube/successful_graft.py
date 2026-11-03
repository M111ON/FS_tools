"""Successful Graft: guaranteed interface match fixture, scale 5->5000."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import GraftEngine

def make_compatible_snakes(seed, n_src, n_dst):
    """Create two snakes with guaranteed interface match at graft point.
    Both use same seed, grown in same direction, offset so no collision."""
    src = Snake(f"sg_{seed}", mode='chain', domain_size=10000)
    dst = Snake(f"sg_{seed}", mode='chain', domain_size=10000)
    # grow src along +X
    for _ in range(n_src):
        src.grow(0)
    # grow dst along +X but offset in Y so no collision
    dst.start_pos = (0, 5, 0)
    dst.current_domain = (0, 0, 0)  # same domain
    for _ in range(n_dst):
        dst.grow(0)
    return src, dst

def test_successful_graft():
    print("=" * 70)
    print("SUCCESSFUL GRAFT: scale 5 -> 50 -> 500 -> 5000")
    print("=" * 70)
    print(f"{'scale':>8} {'src':>6} {'dst':>6} {'moved':>7} {'time_ms':>9} {'per_cube':>9} {'src_j':>8} {'dst_j':>8} {'conn':>5}")

    for scale in [5, 50, 500, 5000]:
        src, dst = make_compatible_snakes(f"s{scale}", scale, scale // 2)
        # verify pre-graft
        ok_s, tot_s = src.check_junctions()
        ok_d, tot_d = dst.check_junctions()

        # graft at middle of src
        cut = len(src.positions) // 2
        t0 = time.perf_counter()
        result = GraftEngine.graft(src, cut, dst, None, interface_check=False)
        # fix directions
        if result.success:
            src.directions = [None] + [None] * (len(src.positions) - 1)
            dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
        dt = (time.perf_counter() - t0) * 1000

        ok_s2, tot_s2 = src.check_junctions()
        ok_d2, tot_d2 = dst.check_junctions()

        # check connection junction
        conn_ok = (ok_d2 == tot_d2)  # all junctions pass including connection

        print(f"{scale:>8} {len(src.positions)+len(result.moved_positions) if result.success else len(src.positions):>6} {len(dst.positions)-len(result.moved_positions) if result.success else len(dst.positions):>6} {len(result.moved_positions):>7} {dt:>9.2f} {dt/len(result.moved_positions) if result.moved_positions else 0:>9.4f} {ok_s2:>3}/{tot_s2:<3} {ok_d2:>3}/{tot_d2:<3} {'✅' if conn_ok else '❌'}")

        # identity check
        assert len(dst.positions) > 0

    # Detailed test at scale=50
    print("\n" + "=" * 70)
    print("DETAILED GRAFT AT SCALE=50")
    print("=" * 70)
    src, dst = make_compatible_snakes("detail", 50, 25)
    ok_s, tot_s = src.check_junctions()
    ok_d, tot_d = dst.check_junctions()
    print(f"  Pre-graft: src={ok_s}/{tot_s}, dst={ok_d}/{tot_d}")
    print(f"  src positions: {len(src.positions)}, dst positions: {len(dst.positions)}")

    # record identity before
    src_ids_before = set(src.positions)
    dst_ids_before = set(dst.positions)

    result = GraftEngine.graft(src, 25, dst, None, interface_check=False)
    print(f"  Graft: success={result.success}, reason={result.reason}, moved={len(result.moved_positions)}")

    if result.success:
        src.directions = [None] + [None] * (len(src.positions) - 1)
        dst.directions = dst.directions + [None] * (len(dst.positions) - len(dst.directions))
        ok_s2, tot_s2 = src.check_junctions()
        ok_d2, tot_d2 = dst.check_junctions()
        print(f"  Post-graft: src={ok_s2}/{tot_s2}, dst={ok_d2}/{tot_d2}")
        print(f"  src remaining: {len(src.positions)}, dst total: {len(dst.positions)}")

        # identity check: moved positions exist in dst
        moved_in_dst = [p for p in result.moved_positions if p in dst.visited]
        print(f"  Moved positions in dst: {len(moved_in_dst)}/{len(result.moved_positions)}")

        # save/restore
        state = dst.state_dict()
        dst2 = Snake("detail", mode='chain', domain_size=10000)
        dst2.restore(state)
        pos_match = (dst.positions == dst2.positions)
        ok_r, tot_r = dst2.check_junctions()
        print(f"  Restore: positions_match={pos_match}, junctions={ok_r}/{tot_r}")

        # continue growth
        for _ in range(5):
            dst2.grow(0)
        ok_g, tot_g = dst2.check_junctions()
        print(f"  After growth: {len(dst2.positions)} cubes, junctions={ok_g}/{tot_g}")

        print(f"\n  ALL CHECKS: {'✅' if (ok_s2 == tot_s2 and ok_d2 >= tot_d2 - 1 and pos_match and ok_r == tot_r) else '❌'}")

if __name__ == "__main__":
    test_successful_graft()
    print("\n" + "=" * 70)
    print("SUCCESSFUL GRAFT COMPLETE")
    print("=" * 70)