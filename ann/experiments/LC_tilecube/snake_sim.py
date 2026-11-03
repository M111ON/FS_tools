"""Snake simulation core — geometric growth with junction verification."""
import random, json

DIRS = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
AXIS_OF = {0:0, 1:0, 2:1, 3:1, 4:2, 5:2}

def domain_of(pos, domain_size):
    return (pos[0] // domain_size, pos[1] // domain_size, pos[2] // domain_size)

def row_key(axis, pos):
    if axis == 0:
        return (pos[1], pos[2])
    elif axis == 1:
        return (pos[0], pos[2])
    else:
        return (pos[0], pos[1])

def seed_rng(seed, salt):
    return random.Random(hash((seed, salt)) & 0xFFFFFFFF)

class Snake:
    def __init__(self, seed, mode='chain', domain_size=20, start_pos=None):
        self.seed = seed
        self.mode = mode
        self.domain_size = domain_size
        if start_pos is None:
            start_pos = (domain_size // 2, domain_size // 2, domain_size // 2)
        self.start_pos = start_pos
        self.positions = [start_pos]
        self.visited = {start_pos}
        self.letters = {}
        self.rows = {}
        self.current_domain = domain_of(start_pos, domain_size)
        self.growth_count = 1
        self.directions = [None]
        self.boundary_blocks = 0
        self._gen_cube(start_pos)

    def _gen_cube(self, pos, prev_pos=None, dir_idx=None):
        row_vals = [None, None, None]
        if prev_pos is not None and dir_idx is not None:
            axis = AXIS_OF[dir_idx]
            if prev_pos in self.letters:
                prev_rv = self.letters[prev_pos][axis]
                row_vals[axis] = prev_rv
        for k in range(3):
            if row_vals[k] is None:
                r = seed_rng(self.seed, f"c{pos}|{k}")
                letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
                frame = r.choice("abcdefgh")
                row_vals[k] = (tuple(letters), frame)
        self.letters[pos] = row_vals

    def grow(self, direction):
        if not self.positions:
            return False, "empty"
        last = self.positions[-1]
        dx, dy, dz = DIRS[direction]
        new_pos = (last[0] + dx, last[1] + dy, last[2] + dz)
        new_domain = domain_of(new_pos, self.domain_size)
        if new_domain != self.current_domain:
            self.boundary_blocks += 1
            return False, "boundary"
        if new_pos in self.visited:
            return False, "collision"
        # generate letters
        self._gen_cube(new_pos, last, direction)
        self.positions.append(new_pos)
        self.visited.add(new_pos)
        self.growth_count += 1
        self.directions.append(direction)
        return True, "ok"

    def check_junctions(self):
        """Geometric junction (5-pair): full tuple equality. Original LetterCube rule."""
        return self._check_junctions(mode='geometric')

    def check_graft_interface(self):
        """Graft interface (1-pair): frame equality only. For graft boundaries."""
        return self._check_junctions(mode='graft')

    def _check_junctions(self, mode='geometric'):
        ok = 0; tot = 0
        for i in range(len(self.positions) - 1):
            a = self.positions[i]
            b = self.positions[i + 1]
            diff_axis = None
            for k in range(3):
                if a[k] != b[k]:
                    diff_axis = k
                    break
            if diff_axis is None:
                continue
            tot += 1
            if a in self.letters and b in self.letters:
                ra = self.letters[a][diff_axis]
                rb = self.letters[b][diff_axis]
                if mode == 'geometric':
                    if ra == rb:
                        ok += 1
                else:
                    if ra[1] == rb[1]:
                        ok += 1
        return ok, tot

    def state_dict(self):
        letters_ser = {}
        for pos, val in self.letters.items():
            key = ",".join(str(x) for x in pos)
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
            'domain_size': self.domain_size,
            'positions': self.positions,
            'domains': [],
            'letters': letters_ser,
            'visited': [list(p) for p in self.visited],
            'current_domain': self.current_domain,
            'growth_count': self.growth_count,
            'directions': self.directions,
            'boundary_blocks': self.boundary_blocks,
        }

    def restore(self, state):
        self.seed = state['seed']
        self.mode = state['mode']
        self.positions = [tuple(p) for p in state['positions']]
        self.visited = set(tuple(p) for p in state['visited'])
        self.current_domain = state['current_domain']
        self.growth_count = state['growth_count']
        self.directions = state['directions']
        self.boundary_blocks = state.get('boundary_blocks', 0)
        self.letters = {}
        self.rows = {}
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
        # domain_size
        if 'domain_size' in state:
            self.domain_size = state['domain_size']
        else:
            self.domain_size = 20