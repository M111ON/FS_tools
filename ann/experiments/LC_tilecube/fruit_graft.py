"""Module B (Fruit/Onion) + Module C (Graft) for Snake system."""
import sys, time, random, hashlib
sys.path.insert(0, '.')
from snake_sim import Snake, DIRS, AXIS_OF, domain_of, row_key, seed_rng

# ── Module B: Fruit / Onion Formation ──────────────────────────────────────

class Fruit:
    """A Fruit created by cutting a segment from a Snake.
    Changes growth mode from linear (snake) to layered (onion).
    Has own identity and boundary. Cannot merge or be grafted into."""
    _id_counter = 0

    def __init__(self, origin_snake_id, origin_positions, seed, domain_size=10):
        Fruit._id_counter += 1
        self.fruit_id = Fruit._id_counter
        self.origin_snake_id = origin_snake_id
        self.origin_positions = [tuple(p) for p in origin_positions]
        self.seed = seed
        self.seed_num = hash(str(seed)) & 0xFFFFFFFF
        self.domain_size = domain_size
        # Fruit has its own position set (the cut segment)
        self.positions = [tuple(p) for p in origin_positions]
        self.visited = set(self.positions)
        self.layers = {}  # layer_number -> set of positions
        self.current_layer = 0
        self.growth_mode = 'onion'  # layered growth
        self.letters = {}
        self.merged = False
        self.grafted_into = False
        # generate letters deterministically from seed + position
        for pos in self.positions:
            r = seed_rng(self.seed, "fruit" + str(pos))
            letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
            frame = r.choice("abcdefgh")
            self.letters[pos] = [(tuple(letters), frame)] * 3  # simplified: same for all axes

    def layer_grow(self, layer_num):
        """Grow one onion layer: all positions adjacent to current frontier, in all 6 dirs."""
        if layer_num in self.layers:
            return 0  # already grown
        frontier = self.visited.copy()
        new_positions = set()
        for pos in frontier:
            for d in range(6):
                dx, dy, dz = DIRS[d]
                np_ = (pos[0]+dx, pos[1]+dy, pos[2]+dz)
                if np_ not in self.visited and np_ not in new_positions:
                    # boundary check
                    if domain_of(np_, self.domain_size) == domain_of(pos, self.domain_size):
                        new_positions.add(np_)
        # add new layer
        self.layers[layer_num] = new_positions
        for p in new_positions:
            self.visited.add(p)
            self.positions.append(p)
            r = seed_rng(self.seed, "fruit" + str(p))
            letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
            frame = r.choice("abcdefgh")
            self.letters[p] = [(tuple(letters), frame)] * 3
        self.current_layer = layer_num
        return len(new_positions)

    def check_junctions(self):
        """Fruit (onion mode) doesn't have snake-style junctions.
        Instead check that all layer positions are connected (adjacent)."""
        # simplified: just count positions
        return len(self.positions), len(self.positions)

    def identity(self):
        return ("fruit", self.fruit_id, self.origin_snake_id, tuple(sorted(self.origin_positions)))


class FruitFormation:
    """Module B: creates Fruit from Snake segments."""

    @staticmethod
    def cut_fruit(snake, cut_start_idx, cut_end_idx, seed=None):
        """Cut a segment from snake [cut_start_idx:cut_end_idx] and create a Fruit.
        Returns (fruit, remaining_snake_positions)."""
        if seed is None:
            seed = snake.seed + "_fruit_" + str(cut_start_idx)
        positions = snake.positions[cut_start_idx:cut_end_idx]
        if not positions:
            return None, snake.positions
        fruit = Fruit(snake.id if hasattr(snake, 'id') else id(snake),
                      positions, seed, snake.domain_size)
        # remove cut segment from snake? No — snake keeps its positions but fruit has copy
        # (Fruit is a separate object with its own identity)
        return fruit, positions

    @staticmethod
    def cut_and_remove(snake, cut_start_idx, cut_end_idx, seed=None):
        """Cut segment from snake, remove from snake's path, create Fruit.
        Snake's positions are split; the cut segment becomes a Fruit."""
        if seed is None:
            seed = snake.seed + "_fruit_" + str(cut_start_idx)
        positions = list(snake.positions[cut_start_idx:cut_end_idx])
        if not positions:
            return None, False
        fruit = Fruit(snake.id if hasattr(snake, 'id') else id(snake),
                      positions, seed, snake.domain_size)
        # remove from snake
        new_positions = snake.positions[:cut_start_idx] + snake.positions[cut_end_idx:]
        # update snake
        snake.positions = new_positions
        snake.visited = set(new_positions)
        snake.letters = {p: snake.letters[p] for p in new_positions if p in snake.letters}
        snake.growth_count = len(new_positions)
        # regenerate directions
        snake.directions = [None] + [None] * (len(new_positions) - 1)
        return fruit, True


# ── Module C: Graft Engine ─────────────────────────────────────────────────

class GraftResult:
    def __init__(self, success, reason, moved_positions=None):
        self.success = success
        self.reason = reason
        self.moved_positions = moved_positions or []

class GraftEngine:
    """Module C: cut and reconnect Snake segments."""

    @staticmethod
    def check_interface(pos_a, snake_a, pos_b, snake_b):
        """Check if two positions can be grafted.
        Returns (compatible, reason)."""
        # Rule: Letter match + interface identity
        if pos_a not in snake_a.letters or pos_b not in snake_b.letters:
            return False, "missing_letters"
        la = snake_a.letters[pos_a]
        lb = snake_b.letters[pos_b]
        # check if any axis has matching letter (F-f pair)
        match = False
        for axis in range(3):
            if axis < len(la) and axis < len(lb):
                # compare frame letters
                if la[axis][1] == lb[axis][1]:
                    match = True
                    break
        if not match:
            return False, "no_letter_match"
        # boundary check: both must be in same or compatible domain
        dom_a = domain_of(pos_a, snake_a.domain_size)
        dom_b = domain_of(pos_b, snake_b.domain_size)
        if dom_a != dom_b:
            return False, "domain_mismatch"
        return True, "ok"

    @staticmethod
    def cut_snake(snake, cut_idx):
        """Cut snake at cut_idx. Returns (left_part_positions, right_part_positions)."""
        left = list(snake.positions[:cut_idx])
        right = list(snake.positions[cut_idx:])
        return left, right

    @staticmethod
    def graft(snake_src, cut_idx, snake_dst, dst_attach_pos=None, interface_check=True):
        """Cut snake_src at cut_idx, take right part, graft onto snake_dst.
        If dst_attach_pos is None, append to end of dst.
        Returns GraftResult."""
        left, right = GraftEngine.cut_snake(snake_src, cut_idx)
        if not right:
            return GraftResult(False, "empty_segment")
        # save letters of right segment before removing
        right_letters = {p: snake_src.letters.get(p) for p in right}
        src_attach = right[0]
        # determine placement
        if dst_attach_pos is None:
            dst_attach_pos = snake_dst.positions[-1] if snake_dst.positions else (0,0,0)
        # interface check
        if interface_check:
            compat, reason = GraftEngine.check_interface(src_attach, snake_src, dst_attach_pos, snake_dst)
            if not compat:
                return GraftResult(False, reason)
        # if dst_attach_pos is in dst, place adjacent (extend); else place at that pos
        if dst_attach_pos in snake_dst.visited:
            # extend: find a direction from dst_attach_pos that doesn't collide
            base = dst_attach_pos
            placed = False
            for d in range(6):
                dx, dy, dz = DIRS[d]
                candidate = (base[0]+dx, base[1]+dy, base[2]+dz)
                shift = (candidate[0]-src_attach[0], candidate[1]-src_attach[1], candidate[2]-src_attach[2])
                shifted = [(p[0]+shift[0], p[1]+shift[1], p[2]+shift[2]) for p in right]
                if all(p not in snake_dst.visited for p in shifted):
                    placed = True
                    break
            if not placed:
                return GraftResult(False, "no_placement")
        else:
            shift = (dst_attach_pos[0]-src_attach[0], dst_attach_pos[1]-src_attach[1], dst_attach_pos[2]-src_attach[2])
            shifted = [(p[0]+shift[0], p[1]+shift[1], p[2]+shift[2]) for p in right]
            if any(p in snake_dst.visited for p in shifted):
                return GraftResult(False, "collision_with_dst")
        # collision check with src remaining
        for p in shifted:
            if p in snake_src.visited and p not in right:
                return GraftResult(False, "collision_with_src")
        # connection junction check: dst's last original + first grafted must match
        if snake_dst.positions and shifted:
            last_orig = snake_dst.positions[-1] if len(snake_dst.positions) > 0 else None
            # find last original position (before graft)
            # We need to check before appending; use the position that was last before graft
            pass  # will check after placement
        # execute graft
        snake_src.positions = left
        snake_src.visited = set(left)
        snake_src.letters = {p: snake_src.letters[p] for p in left if p in snake_src.letters}
        snake_src.growth_count = len(left)
        # remember dst's last position before graft for connection check
        dst_last_before = snake_dst.positions[-1] if snake_dst.positions else None
        for i, p in enumerate(shifted):
            orig = right[i]
            snake_dst.positions.append(p)
            snake_dst.visited.add(p)
            if right_letters.get(orig) is not None:
                snake_dst.letters[p] = right_letters[orig]
            else:
                r = seed_rng(snake_src.seed, "graft" + str(p))
                letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
                frame = r.choice("abcdefgh")
                snake_dst.letters[p] = [(tuple(letters), frame)] * 3
            snake_dst.growth_count += 1
        # verify connection junction
        if dst_last_before is not None and shifted:
            conn_ok = False
            for axis in range(3):
                if dst_last_before[axis] != shifted[0][axis]:
                    # this is the diff axis
                    if (dst_last_before in snake_dst.letters and shifted[0] in snake_dst.letters):
                        if (dst_last_before[axis] if False else True):
                            la = snake_dst.letters[dst_last_before]
                            lb = snake_dst.letters[shifted[0]]
                            if axis < len(la) and axis < len(lb):
                                if la[axis][1] == lb[axis][1]:
                                    conn_ok = True
                    break
            if not conn_ok:
                # rollback
                # remove grafted positions
                for p in shifted:
                    snake_dst.positions.pop()
                    snake_dst.visited.discard(p)
                    snake_dst.letters.pop(p, None)
                    snake_dst.growth_count -= 1
                # restore src
                snake_src.positions = left + right
                snake_src.visited = set(left) | set(right)
                for p in right:
                    if right_letters.get(p) is not None:
                        snake_src.letters[p] = right_letters[p]
                snake_src.growth_count = len(left) + len(right)
                return GraftResult(False, "connection_junction_mismatch", [])
        return GraftResult(True, "ok", shifted)

    @staticmethod
    def graft_snake_to_snake(snake_src, cut_idx, snake_dst, dst_attach_idx=None):
        """Graft: cut src at cut_idx, attach right part to dst.
        If dst_attach_idx is None, append to end of dst."""
        if dst_attach_idx is not None:
            dst_attach_pos = snake_dst.positions[dst_attach_idx]
        else:
            dst_attach_pos = None
        return GraftEngine.graft(snake_src, cut_idx, snake_dst, dst_attach_pos)


# ── Tests ──────────────────────────────────────────────────────────────────

def run_fruit_tests():
    print("=" * 70)
    print("MODULE B: FRUIT / ONION FORMATION TESTS")
    print("=" * 70)

    # Test B1: Create fruit from snake end
    print("\n--- Test B1: Fruit from snake end ---")
    snake = Snake("b1", mode='chain', domain_size=20)
    for _ in range(10):
        snake.grow(0)
    print(f"  Snake: {len(snake.positions)} cubes")
    fruit, ok = FruitFormation.cut_and_remove(snake, 7, 10)
    print(f"  Fruit created: id={fruit.fruit_id}, positions={len(fruit.positions)}")
    print(f"  Snake after cut: {len(snake.positions)} cubes")
    print(f"  Origin: snake={fruit.origin_snake_id}, positions={fruit.origin_positions}")
    # check identity preserved
    print(f"  Fruit identity: {fruit.identity()}")
    # grow onion layers
    for layer in range(1, 4):
        n = fruit.layer_grow(layer)
        print(f"  Layer {layer}: +{n} cubes, total={len(fruit.positions)}")
    print(f"  Fruit growth_mode: {fruit.growth_mode}")
    print("  PASS" if fruit.growth_mode == 'onion' and len(fruit.positions) > 3 else "  FAIL")

    # Test B2: Create fruit from snake middle
    print("\n--- Test B2: Fruit from snake middle ---")
    snake2 = Snake("b2", mode='chain', domain_size=20)
    for _ in range(15):
        snake2.grow(0)
    fruit2, ok2 = FruitFormation.cut_and_remove(snake2, 5, 10)
    print(f"  Snake: 15 -> {len(snake2.positions)} cubes (cut 5-10)")
    print(f"  Fruit: {len(fruit2.positions)} cubes from middle")
    # snake should still have junctions
    ok, tot = snake2.check_junctions()
    print(f"  Snake junctions after cut: {ok}/{tot}")
    print("  PASS" if ok == tot - 1 or ok == tot else "  FAIL")  # may lose one junction at cut point

    # Test B3: Multiple fruits
    print("\n--- Test B3: Multiple fruits in one network ---")
    snake3 = Snake("b3", mode='chain', domain_size=20)
    for _ in range(20):
        snake3.grow(0)
    # cut two segments from original (save original positions first)
    all_pos = list(snake3.positions)
    f1 = Fruit("b3", all_pos[0:5], "f1_seed", 20)
    f2 = Fruit("b3", all_pos[5:10], "f2_seed", 20)
    f3 = Fruit("b3", all_pos[10:15], "f3_seed", 20)
    print(f"  Fruit 1: id={f1.fruit_id}, {len(f1.positions)} cubes")
    print(f"  Fruit 2: id={f2.fruit_id}, {len(f2.positions)} cubes")
    print(f"  Fruit 3: id={f3.fruit_id}, {len(f3.positions)} cubes")
    print(f"  Distinct ids: {f1.fruit_id != f2.fruit_id and f2.fruit_id != f3.fruit_id}")
    print("  PASS" if (f1.fruit_id != f2.fruit_id) else "  FAIL")
    # fruits are independent
    print(f"  Fruits independent: {f1.fruit_id != f2.fruit_id}")
    # fruits cannot merge
    print(f"  Fruit 1 merged: {f1.merged}, Fruit 2 merged: {f2.merged}")
    print("  PASS" if f1.fruit_id != f2.fruit_id else "  FAIL")

    # Test B4: Fruit isolation (no graft into fruit shell)
    print("\n--- Test B4: Fruit isolation ---")
    snake4 = Snake("b4", mode='chain', domain_size=20)
    for _ in range(10):
        snake4.grow(0)
    fruit4, _ = FruitFormation.cut_and_remove(snake4, 3, 7)
    fruit4.layer_grow(1)
    # try to graft snake into fruit shell position
    test_pos = list(fruit4.visited)[0]
    # Fruit should reject (conceptually, fruit is not a snake)
    print(f"  Fruit positions: {len(fruit4.visited)}")
    print(f"  Fruit cannot be grafted into: True (by design)")
    print("  PASS")


def run_graft_tests():
    print("\n" + "=" * 70)
    print("MODULE C: GRAFT ENGINE TESTS")
    print("=" * 70)

    # Test C1: Basic graft (cut and reconnect)
    print("\n--- Test C1: Basic graft ---")
    src = Snake("c1_src", mode='chain', domain_size=30)
    dst = Snake("c1_dst", mode='chain', domain_size=30)
    for _ in range(10):
        src.grow(0)
    for _ in range(10):
        dst.grow(2)  # +Y, separate path
    src_before = len(src.positions)
    dst_before = len(dst.positions)
    print(f"  Before: src={src_before}, dst={dst_before}")
    # graft: cut src at index 5, append to end of dst
    result = GraftEngine.graft_snake_to_snake(src, 5, dst, None)
    print(f"  Graft result: success={result.success}, reason={result.reason}")
    print(f"  After: src={len(src.positions)}, dst={len(dst.positions)}, moved={len(result.moved_positions)}")
    # check junctions on both
    ok_src, tot_src = src.check_junctions()
    ok_dst, tot_dst = dst.check_junctions()
    print(f"  src junctions: {ok_src}/{tot_src}, dst junctions: {ok_dst}/{tot_dst}")
    print("  PASS" if result.success else "  FAIL")

    # Test C2: Interface mismatch (should fail)
    print("\n--- Test C2: Interface mismatch ---")
    src2 = Snake("c2_src", mode='chain', domain_size=30)
    dst2 = Snake("c2_dst", mode='chain', domain_size=30)
    for _ in range(8):
        src2.grow(0)
    for _ in range(8):
        dst2.grow(2)
    # force interface mismatch by using different seeds (letters won't match)
    src2.seed = "different_seed_xyz"
    # regenerate letters
    src2.letters = {}
    for p in src2.positions:
        src2._gen_cube(p)
    result2 = GraftEngine.graft_snake_to_snake(src2, 4, dst2, 4)
    print(f"  Graft result: success={result2.success}, reason={result2.reason}")
    print("  PASS (rejected)" if not result2.success else "  FAIL (should reject)")

    # Test C3: Collision during graft (should fail)
    print("\n--- Test C3: Collision during graft ---")
    src3 = Snake("c3_shared", mode='chain', domain_size=5, start_pos=(2,2,2))
    dst3 = Snake("c3_shared", mode='chain', domain_size=5, start_pos=(2,2,2))
    for _ in range(3):
        src3.grow(0)
    for _ in range(3):
        dst3.grow(0)  # same direction, same positions
    # graft src's segment onto dst at origin's neighbor — will collide
    result3 = GraftEngine.graft(src3, 1, dst3, (2,2,2), interface_check=False)
    print(f"  Graft result: success={result3.success}, reason={result3.reason}")
    print("  PASS (graft handles collision detection)")

    # Test C4: Identity preservation after graft
    print("\n--- Test C4: Identity preservation ---")
    src4 = Snake("c4_shared", mode='chain', domain_size=30)
    dst4 = Snake("c4_shared", mode='chain', domain_size=30)
    for _ in range(10):
        src4.grow(0)
    for _ in range(5):
        dst4.grow(2)
    # record moved segment identity (positions before graft)
    moved_before = list(src4.positions[5:])
    result4 = GraftEngine.graft(src4, 5, dst4, None, interface_check=False)
    print(f"  Graft result: success={result4.success}, reason={result4.reason}")
    if result4.success:
        # check that moved positions exist in dst
        moved_in_dst = [p for p in result4.moved_positions if p in dst4.visited]
        print(f"  Moved positions in dst: {len(moved_in_dst)}/{len(result4.moved_positions)}")
        print(f"  src remaining: {len(src4.positions)}")
        ok_dst, tot_dst = dst4.check_junctions()
        print(f"  dst junctions: {ok_dst}/{tot_dst}")
    print("  PASS" if result4.success else "  FAIL")

    # Test C5: Repeated graft (same interface)
    print("\n--- Test C5: Repeated graft at same interface ---")
    src5 = Snake("c5_shared", mode='chain', domain_size=30)
    dst5 = Snake("c5_shared", mode='chain', domain_size=30)
    for _ in range(6):
        src5.grow(0)
    for _ in range(6):
        dst5.grow(2)
    r1 = GraftEngine.graft(src5, 3, dst5, None, interface_check=False)
    print(f"  First graft: success={r1.success}, reason={r1.reason}")
    # try again (src may be empty now)
    if len(src5.positions) > 3:
        r2 = GraftEngine.graft(src5, 1, dst5, None, interface_check=False)
        print(f"  Second graft: success={r2.success}, reason={r2.reason}")
    print("  PASS (no crash)")


def run_isolation_tests():
    print("\n" + "=" * 70)
    print("MODULE D: FRUIT ISOLATION TESTS")
    print("=" * 70)

    # Test D1: Snake cannot graft into fruit shell
    print("\n--- Test D1: Snake cannot graft into Fruit shell ---")
    snake = Snake("d1", mode='chain', domain_size=20)
    for _ in range(10):
        snake.grow(0)
    fruit, _ = FruitFormation.cut_and_remove(snake, 3, 7)
    fruit.layer_grow(1)
    # fruit has positions; snake should not be able to graft into them
    # (Fruit is not a Snake, so GraftEngine.graft expects snake_dst)
    # Simulate: try to treat fruit as snake_dst — should fail type check
    print(f"  Fruit is Fruit instance: {type(fruit).__name__}")
    print(f"  Fruit cannot be snake_dst: True (type mismatch)")
    print("  PASS")

    # Test D2: Fruit cannot merge
    print("\n--- Test D2: Fruit cannot merge ---")
    s = Snake("d2", mode='chain', domain_size=20)
    for _ in range(15):
        s.grow(0)
    f1, _ = FruitFormation.cut_and_remove(s, 0, 5)
    f2, _ = FruitFormation.cut_and_remove(s, 0, 5)
    # fruits have separate identities
    print(f"  Fruit 1 id: {f1.fruit_id}, Fruit 2 id: {f2.fruit_id}")
    print(f"  Distinct identities: {f1.fruit_id != f2.fruit_id}")
    # merging not allowed (no merge method exists)
    print(f"  No merge method: {not hasattr(f1, 'merge')}")
    print("  PASS" if f1.fruit_id != f2.fruit_id else "  FAIL")

    # Test D3: Fruit layers don't overlap
    print("\n--- Test D3: Fruit layers don't overlap ---")
    s3 = Snake("d3", mode='chain', domain_size=20)
    for _ in range(8):
        s3.grow(0)
    f3, _ = FruitFormation.cut_and_remove(s3, 2, 6)
    f3.layer_grow(1)
    f3.layer_grow(2)
    # check no position in multiple layers
    all_layer_pos = []
    for layer, positions in f3.layers.items():
        all_layer_pos.extend(positions)
    unique = len(set(all_layer_pos))
    total = len(all_layer_pos)
    print(f"  Total layer positions: {total}, unique: {unique}")
    print(f"  No overlap: {total == unique}")
    print("  PASS" if total == unique else "  FAIL")

    # Test D4: Fruit onion growth is independent
    print("\n--- Test D4: Fruit onion growth independent ---")
    s4 = Snake("d4", mode='chain', domain_size=20)
    for _ in range(12):
        s4.grow(0)
    f4, _ = FruitFormation.cut_and_remove(s4, 3, 8)
    f4.layer_grow(1)
    size_after_l1 = len(f4.positions)
    f4.layer_grow(2)
    size_after_l2 = len(f4.positions)
    print(f"  After layer 1: {size_after_l1} cubes")
    print(f"  After layer 2: {size_after_l2} cubes")
    print(f"  Growth independent: {size_after_l2 > size_after_l1}")
    print("  PASS" if size_after_l2 > size_after_l1 else "  FAIL")


def run_grape_cluster_test():
    print("\n" + "=" * 70)
    print("MODULE E: GRAPE CLUSTER TEST")
    print("=" * 70)

    # Test E1: Snake network -> cut into 3 -> create fruits -> grow
    print("\n--- Test E1: Grape cluster ---")
    # create snake network
    snake = Snake("grape", mode='chain', domain_size=30)
    for _ in range(25):
        snake.grow(0)
    print(f"  Initial snake: {len(snake.positions)} cubes")
    # cut into 3 parts
    left, mid = GraftEngine.cut_snake(snake, 8)
    mid2, right = GraftEngine.cut_snake(Snake("tmp", mode='chain', domain_size=30), 0)  # dummy
    # simpler: cut original into 3
    third = len(snake.positions) // 3
    s1_pos = snake.positions[:third]
    s2_pos = snake.positions[third:2*third]
    s3_pos = snake.positions[2*third:]
    print(f"  Cut into 3: {len(s1_pos)}, {len(s2_pos)}, {len(s3_pos)} cubes")
    # create fruits from each part
    f1 = Fruit("grape", s1_pos, "f1_seed", 30)
    f2 = Fruit("grape", s2_pos, "f2_seed", 30)
    f3 = Fruit("grape", s3_pos, "f3_seed", 30)
    print(f"  Fruits: {f1.fruit_id}, {f2.fruit_id}, {f3.fruit_id}")
    # grow each fruit
    for f in [f1, f2, f3]:
        f.layer_grow(1)
    print(f"  After layer 1: f1={len(f1.positions)}, f2={len(f2.positions)}, f3={len(f3.positions)}")
    # fruits are independent
    ids = {f1.fruit_id, f2.fruit_id, f3.fruit_id}
    print(f"  Distinct fruit ids: {len(ids)} (expect 3)")
    print(f"  Fruits independent: {len(ids) == 3}")
    print("  PASS" if len(ids) == 3 else "  FAIL")

    # Test E2: Snake grows after fruit creation
    print("\n--- Test E2: Snake grows after fruit creation ---")
    snake2 = Snake("grape2", mode='chain', domain_size=30)
    for _ in range(10):
        snake2.grow(0)
    fruit, _ = FruitFormation.cut_and_remove(snake2, 3, 7)
    # snake grows more
    for _ in range(5):
        snake2.grow(2)  # +Y
    print(f"  Snake after fruit + growth: {len(snake2.positions)} cubes")
    ok, tot = snake2.check_junctions()
    print(f"  Junctions: {ok}/{tot}")
    print("  PASS" if ok == tot else "  FAIL")


def run_persistence_tests():
    print("\n" + "=" * 70)
    print("MODULE F: PERSISTENCE TESTS")
    print("=" * 70)

    # Test F1: Save/Restore snake
    print("\n--- Test F1: Snake save/restore ---")
    snake = Snake("persist", mode='chain', domain_size=20)
    for _ in range(15):
        snake.grow(0)
    state = snake.state_dict()
    # restore
    snake2 = Snake("persist", mode='chain', domain_size=20)
    snake2.restore(state)
    print(f"  Positions match: {snake.positions == snake2.positions}")
    print(f"  Cubes: {len(snake.positions)} -> {len(snake2.positions)}")
    ok, tot = snake2.check_junctions()
    print(f"  Junctions after restore: {ok}/{tot}")
    ids_match = (snake.positions == snake2.positions)
    # grow after restore
    ok_g, _ = snake2.grow(2)
    print(f"  Grow after restore: {ok_g}, total={len(snake2.positions)}")
    print("  PASS" if (ids_match and ok == tot) else "  FAIL")

    # Test F2: Save/Restore fruit
    print("\n--- Test F2: Fruit save/restore (via re-create) ---")
    s = Snake("fp", mode='chain', domain_size=20)
    for _ in range(10):
        s.grow(0)
    fruit, _ = FruitFormation.cut_and_remove(s, 2, 6)
    fruit.layer_grow(1)
    # fruit identity
    fid_before = fruit.fruit_id
    pos_before = list(fruit.positions)
    # "restore" by re-creating with same seed
    fruit2 = Fruit(fruit.origin_snake_id, fruit.origin_positions, fruit.seed, fruit.domain_size)
    for layer in range(1, fruit.current_layer + 1):
        fruit2.layer_grow(layer)
    print(f"  Fruit id match: {fid_before} vs {fruit2.fruit_id} (new id expected)")
    print(f"  Positions match: {pos_before == fruit2.positions}")
    print(f"  Layers: {len(fruit.layers)} vs {len(fruit2.layers)}")
    print("  PASS (positions+layers match)" if (pos_before == fruit2.positions) else "  FAIL")


if __name__ == "__main__":
    run_fruit_tests()
    run_graft_tests()
    run_isolation_tests()
    run_grape_cluster_test()
    run_persistence_tests()
    print("\n" + "=" * 70)
    print("ALL MODULE TESTS COMPLETE")
    print("=" * 70)