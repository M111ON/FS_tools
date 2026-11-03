"""String Restore Benchmark: Baseline vs String Encoding at 1K/5K/10K."""
import sys, time
sys.path.insert(0, '.')
from snake_sim import Snake, seed_rng
from compact_identity import encode_string, decode_string

def bench_string_restore():
    print("=" * 70)
    print("STRING RESTORE BENCHMARK (1K / 5K / 10K)")
    print("=" * 70)
    print(f"{'n':>6} {'baseline_mem':>12} {'string_mem':>11} {'ratio':>6} {'base_restore':>13} {'str_restore':>12} {'speedup':>8} {'identity':>9} {'junctions':>10}")

    for n in [1000, 5000, 10000]:
        snake = Snake(f"sr_{n}", mode='chain', domain_size=n+100)
        for _ in range(n):
            snake.grow(0)

        # Baseline: store original letters
        state_base = snake.state_dict()

        # String: encode letters as strings
        str_letters = {}
        for pos, val in snake.letters.items():
            key = ",".join(str(x) for x in pos)
            str_letters[key] = encode_string(val)
        state_str = dict(state_base)
        state_str['letters'] = str_letters
        state_str['compact'] = 'string'

        # Memory
        import sys as _sys
        base_mem = sum(_sys.getsizeof(v) for v in state_base['letters'].values()) if False else 0
        # measure actual letter storage
        base_let_mem = 0
        for pos, val in snake.letters.items():
            base_let_mem += _sys.getsizeof(val)
            for rv in val:
                if isinstance(rv, tuple):
                    base_let_mem += _sys.getsizeof(rv)
                    if isinstance(rv[0], tuple):
                        base_let_mem += _sys.getsizeof(rv[0])
        str_let_mem = sum(_sys.getsizeof(v) for v in str_letters.values())
        ratio = base_let_mem / str_let_mem if str_let_mem > 0 else 0

        # Baseline restore
        t0 = time.perf_counter()
        s_base = Snake(f"sr_{n}", mode='chain', domain_size=n+100)
        s_base.restore(state_base)
        t_base = (time.perf_counter() - t0) * 1000
        ok_base, tot_base = s_base.check_junctions()

        # String restore
        t0 = time.perf_counter()
        s_str = Snake(f"sr_{n}", mode='chain', domain_size=n+100)
        s_str.positions = [tuple(p) for p in state_str['positions']]
        s_str.visited = set(s_str.positions)
        s_str.current_domain = state_str['current_domain']
        s_str.growth_count = state_str['growth_count']
        s_str.letters = {}
        for key, val in state_str['letters'].items():
            parts = key.split(",")
            pos = tuple(int(x) for x in parts)
            s_str.letters[pos] = decode_string(val)
        t_str = (time.perf_counter() - t0) * 1000
        ok_str, tot_str = s_str.check_junctions()

        # Identity check
        identity_ok = (s_base.letters == s_str.letters)

        print(f"{n:>6} {base_let_mem:>12} {str_let_mem:>11} {ratio:>6.1f}x {t_base:>13.2f} {t_str:>12.2f} {t_base/t_str:>8.2f}x {'✅' if identity_ok else '❌':>9} {ok_str:>4}/{tot_str:<4}")

    # Detailed at 5K
    print("\n" + "=" * 70)
    print("DETAILED AT n=5000")
    print("=" * 70)
    n = 5000
    snake = Snake("det", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)
    state_base = snake.state_dict()
    str_letters = {}
    for pos, val in snake.letters.items():
        key = ",".join(str(x) for x in pos)
        str_letters[key] = encode_string(val)
    state_str = dict(state_base)
    state_str['letters'] = str_letters
    state_str['compact'] = 'string'

    # Baseline
    t0 = time.perf_counter()
    s_base = Snake("det", mode='chain', domain_size=n+100)
    s_base.restore(state_base)
    t_base = (time.perf_counter() - t0) * 1000
    ok_b, tot_b = s_base.check_junctions()
    t0 = time.perf_counter()
    for _ in range(5):
        s_base.grow(2)
    t_grow_base = (time.perf_counter() - t0) * 1000
    ok_gb, tot_gb = s_base.check_junctions()

    # String
    t0 = time.perf_counter()
    s_str = Snake("det", mode='chain', domain_size=n+100)
    s_str.positions = [tuple(p) for p in state_str['positions']]
    s_str.visited = set(s_str.positions)
    s_str.current_domain = state_str['current_domain']
    s_str.growth_count = state_str['growth_count']
    s_str.letters = {}
    for key, val in state_str['letters'].items():
        parts = key.split(",")
        pos = tuple(int(x) for x in parts)
        s_str.letters[pos] = decode_string(val)
    t_str = (time.perf_counter() - t0) * 1000
    ok_s, tot_s = s_str.check_junctions()
    t0 = time.perf_counter()
    for _ in range(5):
        s_str.grow(2)
    t_grow_str = (time.perf_counter() - t0) * 1000
    ok_gs, tot_gs = s_str.check_junctions()

    print(f"  Baseline: restore={t_base:.2f}ms, junctions={ok_b}/{tot_b}, grow5={t_grow_base:.2f}ms, junctions={ok_gb}/{tot_gb}")
    print(f"  String:   restore={t_str:.2f}ms, junctions={ok_s}/{tot_s}, grow5={t_grow_str:.2f}ms, junctions={ok_gs}/{tot_gs}")
    print(f"  Identity preserved: {s_base.letters == s_str.letters}")
    print(f"  Speedup: {t_base/t_str:.2f}x")

if __name__ == "__main__":
    bench_string_restore()
    print("\n" + "=" * 70)
    print("STRING RESTORE BENCHMARK COMPLETE")
    print("=" * 70)