"""3D Snake mode for LetterCube — field, snake, boundary, jump, regrow, scale."""
import sys, time, random, hashlib, json
sys.path.insert(0, '.')
from lettercube_py import seed_rng, row_key, grid

# 6 directions
DIRS = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
AXIS_OF = {0:0,1:0,2:1,3:1,4:2,5:2}  # dir index -> axis
SIGN_OF = {0:1,1:-1,2:1,3:-1,4:1,5:-1}

def domain_of(pos, domain_size=10):
    """Assign domain by spatial region (box)."""
    x,y,z = pos
    return (x//domain_size, y//domain_size, z//domain_size)

class Snake:
    def __init__(self, seed, mode='chain', domain_size=10, start_pos=None):
        self.seed = seed
        self.seed_num = hash(str(seed)) & 0xFFFFFFFF
        self.mode = mode  # 'row' or 'chain'
        self.domain_size = domain_size
        self.start_pos = start_pos or (domain_size//2, domain_size//2, domain_size//2)
        self.positions = []       # ordered list of cube positions
        self.domains = []         # domain per cube
        self.letters = {}         # pos -> {axis: (f_letters, frame)}
        self.rows = {}            # row_key -> assignment (row mode)
        self.visited = set()      # set of positions
        self.current_domain = None
        self.growth_count = 0
        self.collision_count = 0
        self.boundary_block_count = 0
        self.jump_count = 0
        self.directions = []      # growth direction per cube (None for first)

    def _row_assign(self, rk):
        if rk in self.rows:
            return self.rows[rk]
        r = seed_rng(self.seed, "r" + str(rk))
        letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
        frame = r.choice("abcdefgh")
        val = (tuple(letters), frame)
        self.rows[rk] = val
        return val

    def _gen_cube(self, pos, prev_pos=None, dir_idx=None):
        """Generate letter assignment for a cube at pos."""
        pos = tuple(pos)
        dom = domain_of(pos, self.domain_size)
        # determine axis of growth
        if prev_pos is not None and dir_idx is not None:
            axis = AXIS_OF[dir_idx]
        else:
            axis = 0
        # row mode: assign rows by row_key
        if self.mode == 'row':
            row_vals = []
            for k in range(3):
                rk = row_key(k, pos)
                rv = self._row_assign(rk)
                row_vals.append(rv)
        else:  # chain mode
            row_vals = [None, None, None]
            # inherit from previous cube along growth axis
            if prev_pos is not None and dir_idx is not None:
                axis = AXIS_OF[dir_idx]
                # previous cube's row for this axis
                if prev_pos in self.letters:
                    prev_rv = self.letters[prev_pos][axis]
                    # inherit F-f + frame
                    row_vals[axis] = prev_rv
            # fill remaining axes with deterministic assignment
            for k in range(3):
                if row_vals[k] is None:
                    r = seed_rng(self.seed, f"c{pos}|{k}")
                    letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
                    frame = r.choice("abcdefgh")
                    row_vals[k] = (tuple(letters), frame)
        # inner letters (per-cube)
        r = seed_rng(self.seed, "inner" + str(pos))
        inner = tuple(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(12))
        self.letters[pos] = row_vals
        return row_vals, inner, dom

    def grow(self, dir_idx):
        """Grow one cube in direction dir_idx from head. Returns (success, reason)."""
        if not self.positions:
            # first cube
            pos = self.start_pos
            rv, inner, dom = self._gen_cube(pos)
            self.positions.append(pos)
            self.domains.append(dom)
            self.visited.add(pos)
            self.current_domain = dom
            self.growth_count += 1
            self.directions.append(None)
            return True, "ok"
        head = self.positions[-1]
        d = DIRS[dir_idx]
        new_pos = (head[0]+d[0], head[1]+d[1], head[2]+d[2])
        # boundary check
        new_dom = domain_of(new_pos, self.domain_size)
        if new_dom != self.current_domain:
            self.boundary_block_count += 1
            return False, "boundary"
        # collision check
        if new_pos in self.visited:
            self.collision_count += 1
            return False, "collision"
        # generate
        rv, inner, dom = self._gen_cube(new_pos, head, dir_idx)
        self.positions.append(new_pos)
        self.domains.append(dom)
        self.visited.add(new_pos)
        self.growth_count += 1
        self.directions.append(dir_idx)
        return True, "ok"

    def jump(self, target_pos):
        """Jump to a position in another domain (teleport)."""
        target_pos = tuple(target_pos)
        target_dom = domain_of(target_pos, self.domain_size)
        # generate cube at target if not exists
        if target_pos not in self.visited:
            rv, inner, dom = self._gen_cube(target_pos)
            self.positions.append(target_pos)
            self.domains.append(dom)
            self.visited.add(target_pos)
            self.growth_count += 1
            self.directions.append(None)
        self.current_domain = target_dom
        self.jump_count += 1
        return True

    def check_junctions(self):
        """Check all adjacent pairs in snake path for letter continuity."""
        ok = 0; tot = 0
        for i in range(len(self.positions)-1):
            a = self.positions[i]
            b = self.positions[i+1]
            # find which axis they differ
            diff_axis = None
            for k in range(3):
                if a[k] != b[k]:
                    diff_axis = k
                    break
            if diff_axis is None:
                continue
            tot += 1
            # check: letters should match on diff_axis
            if a in self.letters and b in self.letters:
                ra = self.letters[a][diff_axis]
                rb = self.letters[b][diff_axis]
                # in chain mode, rb should equal ra (inherited)
                # in row mode, row_key should be same
                if self.mode == 'chain':
                    if ra[1] == rb[1]:  # frame match (junction key), not full tuple
                        ok += 1
                else:
                    rka = row_key(diff_axis, a)
                    rkb = row_key(diff_axis, b)
                    if rka == rkb:
                        ok += 1
        return ok, tot

    def state_dict(self):
        # serialize letters as JSON-friendly
        letters_ser = {}
        for pos, val in self.letters.items():
            key = ",".join(str(x) for x in pos)
            # val is list of (tuple, frame) per axis
            ser = []
            for rv in val:
                if isinstance(rv, tuple) and len(rv) == 2:
                    ser.append([list(rv[0]), rv[1]])
                else:
                    ser.append(str(rv))
            letters_ser[key] = ser
        return {
            'seed': self.seed,
            'mode': self.mode,
            'positions': self.positions,
            'domains': self.domains,
            'letters': letters_ser,
            'visited': [list(p) for p in self.visited],
            'current_domain': self.current_domain,
            'growth_count': self.growth_count,
            'directions': self.directions,
        }

    def restore(self, state):
        self.seed = state['seed']
        self.seed_num = hash(str(state['seed'])) & 0xFFFFFFFF
        self.mode = state['mode']
        self.positions = [tuple(p) for p in state['positions']]
        self.domains = state['domains']
        self.directions = state.get('directions', [None]*len(self.positions))
        self.visited = set(tuple(p) for p in state['visited'])
        cd = state['current_domain']
        self.current_domain = tuple(cd) if isinstance(cd, list) else cd
        self.growth_count = state['growth_count']
        # replay growth to regenerate letters with inheritance
        self.letters = {}
        self.rows = {}
        # restore letters from state if available
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
                self.letters[pos] = restored
        else:
            # fallback: regenerate
            if self.positions:
                self._gen_cube(self.positions[0])
            for i in range(1, len(self.positions)):
                prev = self.positions[i-1]
                pos = self.positions[i]
                d = self.directions[i] if i < len(self.directions) else None
                if d is not None:
                    self._gen_cube(pos, prev, d)
                else:
                    self._gen_cube(pos)


def run_snake_tests():
    results = {}
    print("=" * 70)
    print("SNAKE MODE TESTS")
    print("=" * 70)

    # Test 1: Snake Growth (6 directions)
    print("\n--- Test 1: Snake Growth (6 directions) ---")
    snake = Snake("snake1", mode='chain', domain_size=20)
    # grow in all 6 directions from center
    seq = [0, 2, 4, 1, 3, 5]  # +X, +Y, +Z, -X, -Y, -Z
    for d in seq:
        ok, reason = snake.grow(d)
        print(f"  grow dir {d} ({DIRS[d]}): {ok} ({reason}) pos={snake.positions[-1]}")
    ok, tot = snake.check_junctions()
    print(f"  Junctions: {ok}/{tot}")
    results['growth'] = (ok, tot)

    # Test 2: Continuity — longer snake
    print("\n--- Test 2: Continuity (50-step random walk) ---")
    snake2 = Snake("snake2", mode='chain')
    r = random.Random(42)
    for _ in range(50):
        d = r.randrange(6)
        ok, reason = snake2.grow(d)
        if not ok and reason == 'collision':
            # try other directions
            for d2 in range(6):
                ok2, _ = snake2.grow(d2)
                if ok2:
                    break
    ok, tot = snake2.check_junctions()
    print(f"  Steps attempted: 50, actual cubes: {len(snake2.positions)}")
    print(f"  Junctions: {ok}/{tot}  collisions: {snake2.collision_count}")
    results['continuity'] = (ok, tot, snake2.collision_count)

    # Test 3: Collision detection
    print("\n--- Test 3: Collision Detection ---")
    snake3 = Snake("snake3", mode='chain', domain_size=20)
    # grow a square loop that returns near start
    for _ in range(4):
        snake3.grow(0)  # +X x4
    snake3.grow(2)  # +Y
    for _ in range(4):
        snake3.grow(1)  # -X x4 (back to x=0, y=1)
    snake3.grow(3)  # -Y (back to y=0) -> should collide with (0,0,...)
    # check if we got a collision
    print(f"  After loop attempt: cubes={len(snake3.positions)}, collisions={snake3.collision_count}")
    # force collision: grow into a known occupied position
    # current head should be at (0,0,z); grow +X to (1,0,z) which may be occupied
    # Instead, directly test: try growing into origin's neighbor
    ok, reason = snake3.grow(0)
    print(f"  Grow attempt: success={ok}, reason={reason}")
    results['collision'] = (snake3.collision_count > 0 or reason == 'collision')

    # Test 4: Boundary Isolation
    print("\n--- Test 4: Boundary Isolation ---")
    snake4 = Snake("snake4", mode='chain', domain_size=5)
    # grow +X until boundary (domain size 5, start at center 2)
    for _ in range(10):
        ok, reason = snake4.grow(0)
        if not ok:
            print(f"  Blocked after {len(snake4.positions)} cubes: reason={reason}")
            break
    print(f"  boundary_block_count: {snake4.boundary_block_count}")
    print(f"  current_domain: {snake4.current_domain}, cubes: {len(snake4.positions)}")
    results['boundary'] = (snake4.boundary_block_count > 0)

    # Test 5: Jump
    print("\n--- Test 5: Jump (domain switch) ---")
    snake5 = Snake("snake5", mode='chain', domain_size=5)
    snake5.grow(0); snake5.grow(0)  # in domain (0,0,0)
    print(f"  Before jump: domain={snake5.current_domain}, cubes={len(snake5.positions)}")
    snake5.jump((10, 0, 0))  # jump to domain (2,0,0) (different domain)
    print(f"  After jump: domain={snake5.current_domain}, cubes={len(snake5.positions)}")
    # grow in new domain
    ok, reason = snake5.grow(0)
    print(f"  Grow after jump: {ok} ({reason})")
    results['jump'] = (snake5.current_domain, len(snake5.positions))

    # Test 6: Regrow
    print("\n--- Test 6: Regrow (save + restore + continue) ---")
    snake6 = Snake("snake6", mode='chain')
    for _ in range(20):
        snake6.grow(0)
    state = snake6.state_dict()
    # restore
    snake6b = Snake("snake6", mode='chain')
    snake6b.restore(state)
    # check identity
    ids_match = (snake6.positions == snake6b.positions)
    print(f"  Positions match after restore: {ids_match} ({len(snake6.positions)} cubes)")
    # continue growing
    ok, reason = snake6b.grow(2)  # +Z
    print(f"  Continue after restore: {ok} ({reason}), total cubes: {len(snake6b.positions)}")
    ok2, tot2 = snake6b.check_junctions()
    print(f"  Junctions after regrow: {ok2}/{tot2}")
    results['regrow'] = (ids_match, ok2, tot2)

    # Test 7: Scale — straight line (guaranteed no collision)
    print("\n--- Test 7: Scale (straight line, 1000 cubes) ---")
    snake7 = Snake("snake7", mode='chain', domain_size=2000)
    t0 = time.perf_counter()
    while len(snake7.positions) < 1000:
        ok, reason = snake7.grow(0)  # +X
        if not ok:
            break
    dt = time.perf_counter() - t0
    ok, tot = snake7.check_junctions()
    print(f"  Cubes: {len(snake7.positions)}, junctions: {ok}/{tot}")
    print(f"  Collisions: {snake7.collision_count}, boundary blocks: {snake7.boundary_block_count}")
    print(f"  Time: {dt:.2f}s, cubes/s: {len(snake7.positions)/dt:.0f}")
    results['scale'] = (len(snake7.positions), ok, tot, dt)

    # Test 8: Mode comparison (row vs chain)
    print("\n--- Test 8: Row vs Chain mode ---")
    for mode in ['row', 'chain']:
        s = Snake("m_" + mode, mode=mode)
        r = random.Random(7)
        for _ in range(30):
            d = r.randrange(6)
            s.grow(d)
        ok, tot = s.check_junctions()
        print(f"  mode={mode}: cubes={len(s.positions)}, junctions={ok}/{tot}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for k, v in results.items():
        print(f"  {k}: {v}")
    print("\ndone")
    return results

if __name__ == "__main__":
    run_snake_tests()