"""Junction Contract Verification: geometric (5-pair) vs graft interface (1-pair)."""
import sys, time
sys.path.insert(0, '.')
from snake_sim import Snake

def check_geometric_junction(snake):
    """Original LetterCube rule: 5 pairs (4 mini-cube + 1 frame). Full tuple equality."""
    ok = 0; tot = 0
    for i in range(len(snake.positions) - 1):
        a = snake.positions[i]
        b = snake.positions[i + 1]
        diff_axis = None
        for k in range(3):
            if a[k] != b[k]:
                diff_axis = k
                break
        if diff_axis is None:
            continue
        tot += 1
        if a in snake.letters and b in snake.letters:
            ra = snake.letters[a][diff_axis]
            rb = snake.letters[b][diff_axis]
            if ra == rb:  # full tuple: 4 letters + frame
                ok += 1
    return ok, tot

def check_graft_interface(snake):
    """Graft-specific: frame compatibility only (1 pair)."""
    ok = 0; tot = 0
    for i in range(len(snake.positions) - 1):
        a = snake.positions[i]
        b = snake.positions[i + 1]
        diff_axis = None
        for k in range(3):
            if a[k] != b[k]:
                diff_axis = k
                break
        if diff_axis is None:
            continue
        tot += 1
        if a in snake.letters and b in snake.letters:
            ra = snake.letters[a][diff_axis]
            rb = snake.letters[b][diff_axis]
            if ra[1] == rb[1]:  # frame only
                ok += 1
    return ok, tot

def test_junction_contract():
    print("=" * 70)
    print("JUNCTION CONTRACT VERIFICATION")
    print("=" * 70)

    # Test 1: Pure snake (same seed, grown) — both should pass
    print("\n--- Test 1: Pure snake (same seed) ---")
    snake = Snake("jc", mode='chain', domain_size=100)
    for _ in range(30):
        snake.grow(0)
    geo_ok, geo_tot = check_geometric_junction(snake)
    graft_ok, graft_tot = check_graft_interface(snake)
    print(f"  Geometric (5-pair): {geo_ok}/{geo_tot}")
    print(f"  Graft (1-pair):     {graft_ok}/{graft_tot}")
    print(f"  Both pass: {geo_ok == geo_tot and graft_ok == graft_tot}")

    # Test 2: Snake with different seeds on adjacent cubes (frame match, inner differ)
    print("\n--- Test 2: Frame match, inner letters differ ---")
    snake2 = Snake("jc2", mode='chain', domain_size=100)
    for _ in range(15):
        snake2.grow(0)
    # manually change inner letters of cube 7 (keep frame)
    pos7 = snake2.positions[7]
    original = snake2.letters[pos7]
    # change 4-letter tuple but keep frame
    new_letters = []
    for rv in original:
        new_letters.append((tuple('Z' for _ in range(4)), rv[1]))
    snake2.letters[pos7] = new_letters
    geo_ok2, geo_tot2 = check_geometric_junction(snake2)
    graft_ok2, graft_tot2 = check_graft_interface(snake2)
    print(f"  Geometric (5-pair): {geo_ok2}/{geo_tot2}  (expected: {geo_tot2-1}/{geo_tot2})")
    print(f"  Graft (1-pair):     {graft_ok2}/{graft_tot2}  (expected: {graft_tot2}/{graft_tot2})")
    print(f"  Geometric rejects, Graft accepts: {geo_ok2 < geo_tot2 and graft_ok2 == graft_tot2}")

    # Test 3: Graft scenario (two different snakes grafted)
    print("\n--- Test 3: Graft scenario (different seeds) ---")
    src = Snake("g_src", mode='chain', domain_size=10000)
    dst = Snake("g_dst", mode='chain', domain_size=10000)
    for _ in range(20):
        src.grow(0)
    for _ in range(10):
        dst.grow(2)
    # graft
    from fruit_graft import GraftEngine
    base = dst.positions[-1]
    # find compatible cut
    free_dirs = []
    for d in range(6):
        dx,dy,dz = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)][d]
        candidate = (base[0]+dx, base[1]+dy, base[2]+dz)
        if candidate not in dst.visited:
            free_dirs.append((d, candidate))
    grafted = False
    for cut in range(1, len(src.positions)):
        src_attach = src.positions[cut]
        for d, candidate in free_dirs:
            diff_axis = None
            for k in range(3):
                if base[k] != candidate[k]:
                    diff_axis = k
                    break
            if diff_axis is not None:
                la = dst.letters[base]
                lb = src.letters[src_attach]
                if diff_axis < len(la) and diff_axis < len(lb) and la[diff_axis][1] == lb[diff_axis][1]:
                    result = GraftEngine.graft(src, cut, dst, None, interface_check=False)
                    if result.success:
                        grafted = True
                        break
        if grafted:
            break
    if grafted:
        geo_ok3, geo_tot3 = check_geometric_junction(dst)
        graft_ok3, graft_tot3 = check_graft_interface(dst)
        print(f"  Grafted dst: {len(dst.positions)} cubes")
        print(f"  Geometric (5-pair): {geo_ok3}/{geo_tot3}  ({'ALL PASS' if geo_ok3 == geo_tot3 else 'HAS FAILURES'})")
        print(f"  Graft (1-pair):     {graft_ok3}/{graft_tot3}  ({'ALL PASS' if graft_ok3 == graft_tot3 else 'HAS FAILURES'})")
        # connection junction specifically
        # find the connection point
        moved_set = set(result.moved_positions)
        conn_fail_geo = 0
        conn_fail_graft = 0
        for i in range(len(dst.positions) - 1):
            a = dst.positions[i]
            b = dst.positions[i + 1]
            if a in moved_set or b in moved_set:
                if a not in moved_set or b not in moved_set:
                    # this is the connection junction (one moved, one original)
                    diff_axis = None
                    for k in range(3):
                        if a[k] != b[k]:
                            diff_axis = k
                            break
                    if diff_axis is not None and a in dst.letters and b in dst.letters:
                        ra = dst.letters[a][diff_axis]
                        rb = dst.letters[b][diff_axis]
                        if ra != rb:
                            conn_fail_geo += 1
                        if ra[1] != rb[1]:
                            conn_fail_graft += 1
        print(f"  Connection junctions failing geometric: {conn_fail_geo}")
        print(f"  Connection junctions failing graft:     {conn_fail_graft}")
        print(f"  → Inner letters differ at connection: {conn_fail_geo > 0}")
        print(f"  → Frames match at connection: {conn_fail_graft == 0}")
    else:
        print("  No compatible graft found")

    # Summary
    print("\n" + "=" * 70)
    print("CONTRACT SUMMARY")
    print("=" * 70)
    print("  Geometric junction (5-pair): full tuple equality — requires inner letters + frame to match")
    print("  Graft interface (1-pair): frame equality only — inner letters may differ")
    print("")
    print("  Decision point:")
    print("  - If graft uses geometric check: connection junction ALWAYS fails (different seeds)")
    print("  - If graft uses graft interface: connection junction passes (frame match)")
    print("  - Inner letters at connection point come from DIFFERENT snakes → always differ")
    print("  - Therefore: frame-only is NECESSARY for graft to succeed")
    print("  - But: this changes the junction definition from 5-pair to 1-pair at graft boundaries")
    print("")
    print("  Recommendation: Keep both validators. Use geometric for pure snake growth.")
    print("  Use graft interface for graft boundaries. Document the boundary explicitly.")

if __name__ == "__main__":
    test_junction_contract()
    print("\n" + "=" * 70)
    print("JUNCTION CONTRACT VERIFICATION COMPLETE")
    print("=" * 70)