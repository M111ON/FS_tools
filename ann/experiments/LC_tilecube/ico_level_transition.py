"""Level transition L2 -> L3, and the true cost of monitoring max-cell.

Part 1 — monitoring cost, split as the owner required:
    counter update          O(1)          (cell_size[i] += 1)
    recompute max           O(V)          (np.max over the cell size array)
    max-heap update         O(log V)      (heappush + heappop, or lazy push)
The heap is NOT O(1); only the counter is.

Part 2 — real level transition with the service kept up:
    A stop-the-world   reassign every row, then serve
    B chunked + swap   reassign in chunks, serve queries between chunks, then
                       atomically swap the index reference
Measured: rows that actually change cell, data movement, downtime, memory peak,
queries served during the transition, and query latency before/during.
"""
import os, sys, time, heapq, gc
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import Ico, gt_set, l2_topk, _fvecs
from ico_cost_quality import ndcg, TOPK

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
N = 300_000
NQ = 30
CHUNK = 20_000
BUDGET = 5.0


def part1_monitoring():
    print("=" * 96)
    print("PART 1 — monitoring cost of max-cell, split by component")
    print("=" * 96)
    rng = np.random.default_rng(0)
    print(f"  {'cells V':>8} {'counter O(1)':>14} {'recompute O(V)':>16} "
          f"{'heap O(logV)':>14} {'heap/counter':>13}")
    for V in [42, 162, 642, 2562]:
        sizes = rng.integers(1, 5000, size=V).astype(np.int64)
        n_ins = 200_000
        touched = rng.integers(0, V, size=n_ins)

        t0 = time.perf_counter()
        for i in touched:
            sizes[i] += 1                       # O(1) counter
        t_counter = (time.perf_counter() - t0) / n_ins * 1e6

        t0 = time.perf_counter()
        for i in touched:
            sizes[i] += 1
            _ = sizes.max()                     # O(V) recompute
        t_recompute = (time.perf_counter() - t0) / n_ins * 1e6

        h = [(-int(s), j) for j, s in enumerate(sizes)]
        heapq.heapify(h)
        t0 = time.perf_counter()
        for i in touched:
            sizes[i] += 1
            heapq.heappush(h, (-int(sizes[i]), int(i)))   # O(log V)
        t_heap = (time.perf_counter() - t0) / n_ins * 1e6

        print(f"  {V:>8} {t_counter:>13.3f}us {t_recompute:>15.3f}us "
              f"{t_heap:>13.3f}us {t_heap/t_counter:>12.1f}x")
    print("  (lazy-marking alternatives trade memory for a smaller constant;")
    print("   the ordering is what matters: counter << heap < recompute)")


def part2_transition():
    print("\n" + "=" * 96)
    print(f"PART 2 — level transition L2 -> L3 at N={N}, budget {BUDGET}%")
    print("=" * 96)
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(4)
    X = X_all[rng.choice(len(X_all), size=N, replace=False)]
    qidx = rng.choice(N, size=NQ, replace=False)
    qs = X[qidx]

    ico2 = Ico(X, 2)
    sz2 = np.array([len(ico2.cell(i)) for i in range(ico2.nv)])
    cells2 = ico2.node.copy()
    print(f"  L2 built: {ico2.nv} cells, max {sz2.max()} = {sz2.max()/N*100:.2f}% of N, "
          f"index_build {ico2.index_build:.2f}s, budget {BUDGET}%")

    def serve(idx, ico):
        lat = []
        for q in qs:
            t0 = time.perf_counter()
            cands, vis, tr, h = ico.query(q, BUDGET)
            l2_topk(X, q, cands, TOPK)
            lat.append((time.perf_counter() - t0) * 1000)
        return lat

    base_lat = serve(None, ico2)
    print(f"  baseline query latency p50 {np.percentile(base_lat,50):.1f} ms, "
          f"p95 {np.percentile(base_lat,95):.1f} ms")

    # ---- A: stop the world ----
    gc.collect()
    t0 = time.perf_counter()
    ico3_stw = Ico(X, 3)
    downtime_stw = time.perf_counter() - t0
    cells3 = ico3_stw.node
    moved = int((cells3 != cells2).sum()) if len(cells3) == len(cells2) else -1
    print(f"\n  A) stop-the-world: downtime {downtime_stw*1000:.0f} ms, "
          f"rows moved {moved}/{N} ({moved/N*100:.1f}%)")
    print(f"     memory peak (shadow held): index ids {2*N*4/2**20:.1f} MiB; "
          f"if cells hold uint8 vectors {2*N*128/2**20:.0f} MiB")

    # ---- B: chunked reassign with queries interleaved, then swap ----
    gc.collect()
    vL3, fL3 = None, None
    from ico_cost_quality import icosphere
    v3, f3 = icosphere(3)
    vc3 = v3.T.astype(np.float32)
    P = ico2.proj(X)                 # same projection: only the cell size changes
    newnode = np.empty(N, dtype=np.int32)
    newbuf = [[] for _ in range(len(v3))]
    served_during, lat_during = 0, []
    t0 = time.perf_counter()
    for s in range(0, N, CHUNK):
        e = min(s + CHUNK, N)
        newnode[s:e] = np.argmax(P[s:e] @ vc3, axis=1)
        for nid in np.unique(newnode[s:e]):
            ids = np.nonzero(newnode[s:e] == nid)[0]
            newbuf[int(nid)].append((ids + s).astype(np.int32))
        # serve one query between chunks: the old index is untouched
        q = qs[served_during % NQ]
        tq = time.perf_counter()
        cands, vis, tr, h = ico2.query(q, BUDGET)
        l2_topk(X, q, cands, TOPK)
        lat_during.append((time.perf_counter() - tq) * 1000)
        served_during += 1
    build_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    ico2.buffers = newbuf
    ico2.cells = {}
    ico2.nv = len(v3)
    ico2.v = v3
    ico2.vc = vc3
    ico2.f = f3
    from ico_cost_quality import vert_adj
    ico2.adj = vert_adj(len(v3), f3)
    swap = time.perf_counter() - t0
    after_lat = serve(None, ico2)

    print(f"\n  B) chunked + atomic swap: shadow build {build_elapsed*1000:.0f} ms "
          f"({len(range(0,N,CHUNK))} chunks)")
    print(f"     swap {swap*1000:.2f} ms, downtime = swap only")
    print(f"     queries served DURING the transition: {served_during} "
          f"(all completed on the old index)")
    print(f"     query latency during transition p50 {np.percentile(lat_during,50):.1f} ms, "
          f"p95 {np.percentile(lat_during,95):.1f} ms")
    print(f"     query latency after swap p50 {np.percentile(after_lat,50):.1f} ms, "
          f"p95 {np.percentile(after_lat,95):.1f} ms")

    sz3 = np.array([len(ico2.cell(i)) for i in range(ico2.nv)])
    print(f"     L3 now: {ico2.nv} cells, max {sz3.max()} = {sz3.max()/N*100:.2f}% of N")
    print(f"     rows that changed cell L2 -> L3: {int((newnode != cells2).sum())}/{N} "
          f"({(newnode != cells2).mean()*100:.1f}%) -> that is the data movement")

    # memory accounting
    print(f"\n  memory during B (old kept live while new is built):")
    print(f"     ids only      : {2*N*4/2**20:.1f} MiB peak")
    print(f"     uint8 vectors : {2*N*128/2**20:.0f} MiB peak")
    print(f"     f32 384-dim   : {2*N*384*4/2**20:.0f} MiB peak  (text embeddings case)")
    print("\n  Scope: candidate retrieval only. Python prototype, GIL-bound, no RCU/fences.")


if __name__ == '__main__':
    part1_monitoring()
    part2_transition()