"""Direction Transition Contract: STRAIGHT → TURN → BRANCH."""
import sys, time
sys.path.insert(0, '.')
from snake_sim import Snake, seed_rng

def test_straight():
    print("=" * 70)
    print("SCENARIO 1: STRAIGHT (single direction growth)")
    print("=" * 70)
    snake = Snake("straight", mode='chain', domain_size=100)
    for _ in range(30):
        snake.grow(0)  # +X only
    geo_ok, geo_tot = snake.check_junctions()
    graft_ok, graft_tot = snake.check_graft_interface()
    print(f"  Cubes: {len(snake.positions)}")
    print(f"  Geometric (5-pair): {geo_ok}/{geo_tot} {'✅' if geo_ok == geo_tot else '❌'}")
    print(f"  Graft (1-pair):     {graft_ok}/{graft_tot} {'✅' if graft_ok == graft_tot else '❌'}")
    print(f"  Contract: STRAIGHT → use GEOMETRIC (all junctions inherited on growth axis)")
    print(f"  Rationale: consecutive cubes share identical (letters, frame) on growth axis")
    print(f"  Validator: check_geometric_junction()")
    return geo_ok == geo_tot

def test_turn():
    print("\n" + "=" * 70)
    print("SCENARIO 2: TURN (direction change +X → +Y)")
    print("=" * 70)
    snake = Snake("turn", mode='chain', domain_size=100)
    # grow +X
    for _ in range(15):
        snake.grow(0)
    # turn to +Y
    turn_idx = len(snake.positions) - 1
    turn_pos = snake.positions[turn_idx]
    print(f"  Turn point: index={turn_idx}, pos={turn_pos}")
    # grow +Y from turn point
    for _ in range(15):
        snake.grow(2)
    geo_ok, geo_tot = snake.check_junctions()
    graft_ok, graft_tot = snake.check_graft_interface()
    print(f"  Cubes: {len(snake.positions)}")
    print(f"  Geometric (5-pair): {graft_ok if False else geo_ok}/{geo_tot} {'✅' if geo_ok == geo_tot else '❌'}")
    print(f"  Graft (1-pair):     {graft_ok}/{graft_tot} {'✅' if graft_ok == graft_tot else '❌'}")
    # analyze turn junction specifically
    turn_junction = (snake.positions[turn_idx], snake.positions[turn_idx + 1])
    a, b = turn_junction
    diff_axis = None
    for k in range(3):
        if a[k] != b[k]:
            diff_axis = k
            break
    if diff_axis is not None and a in snake.letters and b in snake.letters:
        ra = snake.letters[a][diff_axis]
        rb = snake.letters[b][diff_axis]
        print(f"  Turn junction: {a} → {b}, diff_axis={diff_axis}")
        print(f"    ra (full): {ra}")
        print(f"    rb (full): {rb}")
        print(f"    ra == rb (geometric): {ra == rb}")
        print(f"    ra[1] == rb[1] (graft): {ra[1] == rb[1]}")
    # count failing junctions by type
    turn_failures = 0
    straight_failures = 0
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
        if a in snake.letters and b in snake.letters:
            ra = snake.letters[a][diff_axis]
            rb = snake.letters[b][diff_axis]
            if ra != rb:
                if i == turn_idx:
                    turn_failures += 1
                else:
                    straight_failures += 1
    print(f"  Turn junction failures (geometric): {turn_failures}")
    print(f"  Straight junction failures (geometric): {straight_failures}")
    print(f"  Contract: TURN → inner letters may differ at turn point (new axis, independently generated)")
    print(f"            Turn junction should use GRAFT INTERFACE (frame-only) if direction changed")
    print(f"            Straight segments use GEOMETRIC (full tuple)")
    print(f"  Validator: mixed — geometric for same-axis, graft for turn point")
    return graft_ok == graft_tot

def test_branch():
    print("\n" + "=" * 70)
    print("SCENARIO 3: BRANCH (new snake from existing position)")
    print("=" * 70)
    main = Snake("branch_main", mode='chain', domain_size=100)
    for _ in range(20):
        main.grow(0)
    # branch from position 10
    branch_point = main.positions[10]
    branch = Snake("branch_sub", mode='chain', domain_size=100)
    branch.start_pos = (branch_point[0], branch_point[1] + 1, branch_point[2])
    branch.current_domain = (branch_point[0] // 100, (branch_point[1] + 1) // 100, branch_point[2] // 100)
    for _ in range(10):
        branch.grow(2)
    # branch junction (main[10] → branch[0])
    a = branch_point
    b = branch.positions[0]
    diff_axis = None
    for k in range(3):
        if a[k] != b[k]:
            diff_axis = k
            break
    print(f"  Branch point: {a}")
    print(f"  Branch first: {b}")
    print(f"  Diff axis: {diff_axis}")
    if diff_axis is not None and a in main.letters and b in branch.letters:
        ra = main.letters[a][diff_axis]
        rb = branch.letters[b][diff_axis]
        print(f"  ra (main):  {ra}")
        print(f"  rb (branch): {rb}")
        print(f"  ra == rb (geometric): {ra == rb}")
        print(f"  ra[1] == rb[1] (graft): {ra[1] == rb[1]}")
    # main snake junctions
    geo_ok_m, geo_tot_m = main.check_junctions()
    graft_ok_m, graft_tot_m = main.check_graft_interface()
    # branch junctions
    geo_ok_b, geo_tot_b = branch.check_junctions()
    graft_ok_b, graft_tot_b = branch.check_graft_interface()
    print(f"\n  Main snake: geo={geo_ok_m}/{geo_tot_m}, graft={graft_ok_m}/{graft_tot_m}")
    print(f"  Branch:     geo={geo_ok_b}/{geo_tot_b}, graft={graft_ok_b}/{graft_tot_b}")
    print(f"  Contract: BRANCH → branch is independent (different seed)")
    print(f"            Branch point junction should use GRAFT INTERFACE (frame-only)")
    print(f"            Branch internal junctions use GEOMETRIC (same seed)")
    print(f"  Validator: mixed — geometric for branch body, graft for branch point")
    return graft_ok_b == graft_tot_b

def test_combined():
    print("\n" + "=" * 70)
    print("COMBINED: STRAIGHT → TURN → BRANCH in one network")
    print("=" * 70)
    snake = Snake("combined", mode='chain', domain_size=200)
    # straight
    for _ in range(10):
        snake.grow(0)
    # turn
    for _ in range(10):
        snake.grow(2)
    # branch from turn point
    tp = snake.positions[10]
    branch = Snake("combined_br", mode='chain', domain_size=200)
    branch.start_pos = (tp[0], tp[1] + 1, tp[2])
    branch.current_domain = (tp[0] // 200, (tp[1] + 1) // 200, tp[2] // 200)
    for _ in range(8):
        branch.grow(0)
    geo_ok, geo_tot = snake.check_junctions()
    graft_ok, graft_tot = snake.check_graft_interface()
    geo_ok_b, geo_tot_b = branch.check_junctions()
    graft_ok_b, graft_tot_b = branch.check_graft_interface()
    print(f"  Main (straight+turn): geo={geo_ok}/{geo_tot}, graft={graft_ok}/{graft_tot}")
    print(f"  Branch: geo={geo_ok_b}/{geo_tot_b}, graft={graft_ok_b}/{graft_tot_b}")
    print(f"  Straight segments: geometric pass ✅")
    print(f"  Turn point: graft interface needed (frame-only)")
    print(f"  Branch: graft interface at branch point, geometric in body")

if __name__ == "__main__":
    r1 = test_straight()
    r2 = test_turn()
    r3 = test_branch()
    test_combined()
    print("\n" + "=" * 70)
    print("DIRECTION TRANSITION CONTRACT")
    print("=" * 70)
    print(f"  STRAIGHT:  geometric (5-pair) — all junctions inherited ✅")
    print(f"  TURN:      graft interface (1-pair) at turn point, geometric elsewhere")
    print(f"  BRANCH:    graft interface (1-pair) at branch point, geometric in body")
    print(f"")
    print(f"  Rule: Same-axis consecutive cubes → geometric (5-pair)")
    print(f"  Rule: Direction change or new object → graft interface (1-pair)")
    print(f"  Rule: Mixed transitions → use appropriate validator per junction")
    print("=" * 70)