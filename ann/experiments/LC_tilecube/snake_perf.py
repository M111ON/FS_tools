"""Performance benchmark: Snake, Graft, Fruit, Grape Cluster."""
import sys, time, random, tracemalloc
sys.path.insert(0, '.')
from snake_sim import Snake
from fruit_graft import Fruit, FruitFormation, GraftEngine

def bench_growth():
    print("=" * 60)
    print("BENCH 1: Snake Growth (straight line)")
    print("=" * 60)
    for n in [1000, 10000, 100000]:
        snake = Snake(f"g_{n}", mode='chain', domain_size=n+100)
        t0 = time.perf_counter()
        while len(snake.positions) < n:
            ok, _ = snake.grow(0)
            if not ok:
                break
        dt = time.perf_counter() - t0
        ok, tot = snake.check_junctions()
        print(f"  n={n:>7}: time={dt:.3f}s  per_cube={dt/len(snake.positions)*1000:.4f}ms  junctions={ok}/{tot}")
    # memory
    snake = Snake("mem", mode='chain', domain_size=100000)
    for _ in range(10000):
        snake.grow(0)
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    for _ in range(1000):
        snake.grow(0)
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    print(f"  Memory per 1000 cubes: {(after-before)/1000:.0f} bytes/cube")

def bench_junction_check():
    print("\n" + "=" * 60)
    print("BENCH 2: Junction Check Cost")
    print("=" * 60)
    for n in [1000, 10000, 50000]:
        snake = Snake(f"j_{n}", mode='chain', domain_size=n+100)
        for _ in range(n):
            snake.grow(0)
        t0 = time.perf_counter()
        ok, tot = snake.check_junctions()
        dt = (time.perf_counter() - t0) * 1000
        print(f"  n={n:>6}: check={dt:.3f}ms  per_junction={dt/tot:.4f}ms  ({ok}/{tot})")

def bench_fruit():
    print("\n" + "=" * 60)
    print("BENCH 3: Fruit Onion Layer Growth")
    print("=" * 60)
    snake = Snake("f", mode='chain', domain_size=100)
    for _ in range(20):
        snake.grow(0)
    fruit, _ = FruitFormation.cut_and_remove(snake, 5, 15)
    for layer in range(1, 6):
        t0 = time.perf_counter()
        n = fruit.layer_grow(layer)
        dt = (time.perf_counter() - t0) * 1000
        print(f"  Layer {layer}: +{n:>4} cubes  time={dt:.3f}ms  per_cube={dt/n if n>0 else 0:.4f}ms  total={len(fruit.positions)}")

def bench_graft():
    print("\n" + "=" * 60)
    print("BENCH 4: Graft Cost")
    print("=" * 60)
    for seg_size in [5, 20, 50]:
        src = Snake("gs", mode='chain', domain_size=200)
        dst = Snake("gd", mode='chain', domain_size=200)
        for _ in range(50):
            src.grow(0)
        for _ in range(20):
            dst.grow(2)
        t0 = time.perf_counter()
        result = GraftEngine.graft(src, 25, dst, None, interface_check=False)
        dt = (time.perf_counter() - t0) * 1000
        print(f"  seg={seg_size}: moved={len(result.moved_positions)}  time={dt:.3f}ms  per_cube={dt/len(result.moved_positions) if result.moved_positions else 0:.4f}ms  success={result.success}")

def bench_persistence():
    print("\n" + "=" * 60)
    print("BENCH 5: Save/Restore Cost")
    print("=" * 60)
    for n in [100, 1000, 10000]:
        snake = Snake(f"p_{n}", mode='chain', domain_size=n+100)
        for _ in range(n):
            snake.grow(0)
        t0 = time.perf_counter()
        state = snake.state_dict()
        dt_save = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        snake2 = Snake(f"p_{n}", mode='chain', domain_size=n+100)
        snake2.restore(state)
        dt_restore = (time.perf_counter() - t0) * 1000
        print(f"  n={n:>6}: save={dt_save:.2f}ms  restore={dt_restore:.2f}ms  total={dt_save+dt_restore:.2f}ms")

def bench_multi_snake():
    print("\n" + "=" * 60)
    print("BENCH 6: Multi-Snake (10 snakes, 100 cubes each)")
    print("=" * 60)
    snakes = []
    t0 = time.perf_counter()
    for i in range(10):
        s = Snake(f"ms_{i}", mode='chain', domain_size=200)
        for _ in range(100):
            s.grow(0)
        snakes.append(s)
    dt = time.perf_counter() - t0
    total = sum(len(s.positions) for s in snakes)
    print(f"  10 snakes x 100 cubes = {total} cubes in {dt:.3f}s ({total/dt:.0f} cubes/s)")
    # junction check all
    t0 = time.perf_counter()
    for s in snakes:
        s.check_junctions()
    dt2 = (time.perf_counter() - t0) * 1000
    print(f"  Junction check (10 snakes): {dt2:.2f}ms")

def bench_grape_cluster():
    print("\n" + "=" * 60)
    print("BENCH 7: Grape Cluster (1 snake -> 5 fruits -> layer growth)")
    print("=" * 60)
    snake = Snake("gc", mode='chain', domain_size=100)
    for _ in range(50):
        snake.grow(0)
    t0 = time.perf_counter()
    fruits = []
    for i in range(5):
        f, _ = FruitFormation.cut_and_remove(snake, 0, 5)
        if f:
            fruits.append(f)
            f.layer_grow(1)
            f.layer_grow(2)
    dt = (time.perf_counter() - t0) * 1000
    total_fruit = sum(len(f.positions) for f in fruits)
    print(f"  5 fruits, layer 1+2: {total_fruit} cubes in {dt:.2f}ms")
    print(f"  Snake remaining: {len(snake.positions)} cubes")

if __name__ == "__main__":
    bench_growth()
    bench_junction_check()
    bench_fruit()
    bench_graft()
    bench_persistence()
    bench_multi_snake()
    bench_grape_cluster()
    print("\n" + "=" * 60)
    print("PERFORMANCE BENCHMARK COMPLETE")
    print("=" * 60)