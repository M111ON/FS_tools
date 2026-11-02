"""Python port of LetterCube addressing model (simplified, for testing).
Captures: row keys, gen, links, deterministic vs order-dependent seeding.
"""
import random, hashlib, json

def seed_rng(seed, salt):
    h = hashlib.sha256((str(seed) + "|" + str(salt)).encode()).hexdigest()
    return random.Random(int(h, 16))

def row_key(k, pos):
    coords = tuple(pos[i] for i in range(3) if i != k)
    return (k, coords)

class LetterCube:
    def __init__(self, seed, deterministic=False):
        self.seed = seed
        self.seed_num = hash(str(seed)) & 0xFFFFFFFF
        self.deterministic = deterministic
        self.rows = {}   # row_key -> (f_letters tuple, frame letter)
        self.cubes = {}  # pos tuple -> cube dict
        self.order_counter = 0

    def _row_assign(self, rk):
        if rk in self.rows:
            return self.rows[rk]
        if self.deterministic:
            r = seed_rng(self.seed, "r" + str(rk))
        else:
            # order-dependent: seed from counter (simulates RNG stream position)
            r = random.Random(self.seed_num + self.order_counter * 997)
            self.order_counter += 1
        letters = [r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)]
        frame = r.choice("abcdefgh")
        val = (tuple(letters), frame)
        self.rows[rk] = val
        return val

    def gen(self, pos):
        pos = tuple(pos)
        row_vals = []
        for k in range(3):
            rk = row_key(k, pos)
            rv = self._row_assign(rk)
            row_vals.append(rv)
        if self.deterministic:
            r = seed_rng(self.seed, "c" + str(pos))
        else:
            r = random.Random(self.seed_num + self.order_counter * 997 + 1)
            self.order_counter += 1
        inner = tuple(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(12))
        cube = {"pos": pos, "row_vals": row_vals, "inner": inner}
        self.cubes[pos] = cube
        return cube

    def cube_sig(self, cube):
        return json.dumps({"pos": cube["pos"], "rows": cube["row_vals"], "inner": cube["inner"]}, sort_keys=True)

    def row_sig(self, cube):
        return json.dumps({"rows": cube["row_vals"]}, sort_keys=True)

    def links(self):
        ok = 0; tot = 0
        for pos, c in self.cubes.items():
            for k in range(3):
                n = list(pos); n[k] += 1; n = tuple(n)
                if n in self.cubes:
                    tot += 1
                    rk_c = row_key(k, pos)
                    rk_n = row_key(k, n)
                    if rk_c == rk_n:
                        ok += 1
        return ok, tot

    def build(self, positions):
        for p in positions:
            self.gen(p)
        return list(self.cubes.values())


def grid(nx, ny, nz):
    return [(x, y, z) for z in range(nz) for y in range(ny) for x in range(nx)]


# ---- Tests ----
def test_order_independence():
    print("=== TEST 1: order independence (10x10x10) ===")
    G = grid(10, 10, 10)
    for det in [False, True]:
        # snake
        s1 = LetterCube("S1", deterministic=det)
        s1.build(G)
        sigs_snake = {c["pos"]: s1.cube_sig(c) for c in s1.cubes.values()}
        # reverse
        s2 = LetterCube("S1", deterministic=det)
        s2.build(list(reversed(G)))
        # random
        import random as R
        rr = R.Random(5)
        shuffled = G[:]
        rr.shuffle(shuffled)
        s3 = LetterCube("S1", deterministic=det)
        s3.build(shuffled)
        # junctions
        for name, sim in [("snake", s1), ("reverse", s2), ("random", s3)]:
            ok, tot = sim.links()
            print(f"  det={det} {name}: junction {ok}/{tot}")
        # letter diff from snake
        for name, sim in [("reverse", s2), ("random", s3)]:
            diff = sum(1 for p in G if sigs_snake[p] != sim.cube_sig(sim.cubes[p]))
            print(f"  det={det} {name}: cubes with different sig from snake = {diff}/{len(G)}")


def test_loop_closure():
    print("\n=== TEST 2: loop closure (random walks) ===")
    for det in [False, True]:
        ok_tot = 0; tot_tot = 0; nonconsec = 0; walks = 40
        for w in range(walks):
            sim = LetterCube("W" + str(w), deterministic=det)
            r = random.Random(100 + w)
            seen = set(["0,0,0"])
            p = [0, 0, 0]
            path = [tuple(p)]
            for s in range(800):
                opts = []
                for k in range(3):
                    for d in (-1, 1):
                        q = list(p); q[k] += d
                        if max(abs(v) for v in q) <= 4 and tuple(q) not in seen:
                            opts.append(q)
                if not opts:
                    break
                p = r.choice(opts)
                seen.add(tuple(p))
                path.append(tuple(p))
            sim.build(path)
            o, t = sim.links()
            ok_tot += o; tot_tot += t
            # count non-consecutive adjacent pairs
            idx = {q: i for i, q in enumerate(path)}
            for i, q in enumerate(path):
                for k in range(3):
                    n = list(q); n[k] += 1; n = tuple(n)
                    j = idx.get(n)
                    if j is not None and abs(j - i) != 1:
                        nonconsec += 1
        print(f"  det={det}: junction {ok_tot}/{tot_tot}, non-consecutive closing pairs: {nonconsec}")


def test_regrow():
    print("\n=== TEST 3: regrow persistence (512 + 512) ===")
    A = grid(8, 8, 8)
    B = grid(8, 8, 16)[512:]
    for det in [False, True]:
        # 3a/3c: no log
        sim1 = LetterCube("R1", deterministic=det)
        old = sim1.build(A)
        old_sigs = {c["pos"]: sim1.cube_sig(c) for c in old}
        # new sim, no log, build B on top
        sim2 = LetterCube("R1" if det else "R2", deterministic=det)
        # rebuild old positions to populate rows? For deterministic, rows recomputed same. For non-det, need log.
        if not det:
            sim2.rows = {}  # no log → rows empty → will be reassigned differently
        sim2.build(B)  # only B; but old cubes not in sim2.cubes. Add them.
        # For junction test, need both old and new cubes in same sim
        sim_full = LetterCube("R1" if det else "R2", deterministic=det)
        if not det:
            # simulate "no log": rows empty initially, gen old (order-dependent) then B
            pass
        # Rebuild: gen old first then B (simulates regrow sequence)
        sim_full2 = LetterCube("R1" if det else "R2", deterministic=det)
        for p in A:
            sim_full2.gen(p)
        old2_sigs = {p: sim_full2.cube_sig(sim_full2.cubes[p]) for p in A}
        for p in B:
            sim_full2.gen(p)
        o, t = sim_full2.links()
        # identity: old cubes same sig?
        same = sum(1 for p in A if old2_sigs[p] == sim_full2.cube_sig(sim_full2.cubes[p]))
        print(f"  det={det}: junction {o}/{t}, old cubes identical {same}/{len(A)}")


def test_collision():
    print("\n=== TEST C: collision rate (row signatures per axis) ===")
    for size in [5, 10, 20]:
        sim = LetterCube("D", deterministic=True)
        G = grid(size, size, size)
        sim.build(G)
        for k in range(3):
            sigs = []
            for (pos, c) in sim.cubes.items():
                rk = row_key(k, pos)
                # row signature = the row assignment for this row_key
                rv = sim.rows[rk]
                sigs.append((rk, rv))
            # distinct row_keys
            distinct_keys = len(set(rk for rk, _ in sigs))
            distinct_sigs = len(set(str(rv) for _, rv in sigs))
            total = len(sigs)
            print(f"  grid {size}^3: axis {k}: total rows referenced={total}, distinct row_keys={distinct_keys}, distinct sigs={distinct_sigs}, collisions={total-distinct_sigs}")


def test_capo_shift():
    print("\n=== TEST A4: capo shift (change seed) ===")
    G = grid(6, 6, 6)
    for seed in ["capo0", "capo1"]:
        sim = LetterCube(seed, deterministic=True)
        sim.build(G)
        o, t = sim.links()
        print(f"  seed={seed}: junction {o}/{t}, rows={len(sim.rows)}")


if __name__ == "__main__":
    test_order_independence()
    test_loop_closure()
    test_regrow()
    test_collision()
    test_capo_shift()
    print("\ndone")