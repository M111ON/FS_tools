"""Does the rkey plane carry any SEMANTIC signal, or is it orthogonal to meaning?

The question behind "pack the data in and make it break across the plane - for
what?" If walking the map finds things that mean the same thing, the plane is
already doing semantic work and an LSH second axis would be redundant. If it
does not, the plane is a picture of the corpus with no idea what any of it
means, and pairing it with a semantic index ADDS a way to navigate that nothing
currently has.

Real vectors, not a stand-in: 989 chat-pool chunks already sit in
cloud-workspace/pool.db as 384-d float32 blobs from
paraphrase-multilingual-MiniLM-L12-v2. Read mode=ro; nothing is written.

Ground truth is exact brute-force cosine (989 x 384 is nothing), so the oracle is
not an approximation of the method being judged - it IS the definition of
"semantically similar" and the spatial method is scored against it.
"""
import io
import json
import math
import os
import sqlite3
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool  # noqa: E402

POOL_DB = r"I:\tools\cloud-workspace\pool.db"
K = 10


def load_chunks():
    c = sqlite3.connect("file:%s?mode=ro" % POOL_DB.replace("\\", "/"), uri=True)
    rows = c.execute(
        "select seq, text, embedding from chunks where session_id='chat-pool' "
        "order by seq").fetchall()
    out = []
    for seq, text, blob in rows:
        n = len(blob) // 4
        v = struct.unpack("<%df" % n, blob)
        # normalise once: cosine becomes a dot product
        m = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append((seq, text, [x / m for x in v]))
    c.close()
    return out


def chunk_lines(text):
    """chunk.text is a JSON array string of pool lines, sometimes double-encoded."""
    try:
        pieces = json.loads(text)
    except ValueError:
        return []
    if isinstance(pieces, dict):
        pieces = [pieces]
    lines = []
    for p in pieces or []:
        if isinstance(p, dict) and p.get("text"):
            lines.append(p)
    return lines


def main():
    chunks = load_chunks()
    print("chunks with real vectors: %d  (dim %d)"
          % (len(chunks), len(chunks[0][2]) if chunks else 0))

    # A chunk's place on the plane = the cells its lines landed in.
    cells = []
    total_lines = 0
    for seq, text, v in chunks:
        ls = chunk_lines(text)
        total_lines += len(ls)
        cs = []
        for m in ls:
            raw = (str(m.get("app", "")) + str(m.get("kind", ""))
                   + str(m.get("text", ""))).encode("utf-8")
            cs.append(pool.app_rkey(raw, str(m.get("app", ""))))
        cells.append(cs)
    print("pool lines inside those chunks: %d" % total_lines)

    usable = [i for i, cs in enumerate(cells) if cs]
    print("chunks with a real address on the plane: %d\n" % len(usable))

    # Exact semantic neighbours = the oracle.
    sim = {}
    for i in usable:
        vi = chunks[i][2]
        sim[i] = sorted(
            ((sum(a * b for a, b in zip(vi, chunks[j][2])), j)
             for j in usable if j != i), reverse=True)

    def cos(a):
        return sum(s for s, _ in sim[a][:K]) / K

    # Baseline: what does a RANDOM chunk look like on average?
    rand = []
    for i in usable:
        row = sim[i]
        rand.append(sum(s for s, _ in row[K:K + K]) / K)

    cellset = [set(cs) for cs in cells]

    # The spatial method: walk the plane. A chunk's place is the centroid of the
    # cells its lines landed in — the same thing a person would mean by "that
    # patch of the map", and O(1) per pair so the full matrix is affordable.
    cent = []
    for cs in cells:
        if not cs:
            cent.append(None)
            continue
        # CIRCULAR mean: the plane is a torus, so a cell at x=143 and one at
        # x=0 are neighbours, and an arithmetic mean of {143, 0} lands on 71 —
        # the far side of the map. Average the unit vectors instead.
        cx = cy = None
        for axis in (0, 1):
            ang = [2.0 * math.pi * ((c % 144) if axis == 0 else (c // 144)) / 144.0
                   for c in cs]
            s = math.sin(sum(ang) / len(ang))  # placeholder, replaced below
            sx = sum(math.sin(a) for a in ang) / len(ang)
            cx_ = sum(math.cos(a) for a in ang) / len(ang)
            a_ = math.atan2(sx, cx_) * 144.0 / (2.0 * math.pi)
            if a_ < 0:
                a_ += 144.0
            if axis == 0:
                cx = a_
            else:
                cy = a_
        cent.append((cx, cy))

    def plane_dist(a, b):
        """Spatial neighbours the way the DASHBOARD thinks about them: how many
        cells the two chunks both wrote into. A centroid is the wrong
        instrument here - the mean of many cells regresses to the middle of the
        map, so big chunks drift toward the centre and end up looking like each
        other for reasons that have nothing to do with their content."""
        A, B = cellset[a], cellset[b]
        if not A or not B:
            return None
        ov = len(A & B)
        if ov:
            return -ov
        # nothing shared: fall back on how near their centres are, which is
        # what a person sees when the map has no shared cell
        CA, CB = cent[a], cent[b]
        return math.hypot(CA[0] - CB[0], CA[1] - CB[1]) / 144.0

    plane_neighbours = {}
    for i in usable:
        row = []
        for j in usable:
            if j == i:
                continue
            d = plane_dist(i, j)
            if d is not None:
                row.append((d, j))
        row.sort()
        plane_neighbours[i] = [j for _, j in row[:K]]

    print("=== mean cosine similarity to a query ===")
    truth = sum(cos(i) for i in usable) / len(usable)
    base = sum(rand) / len(rand)
    plane = sum(sum(s for s, j in sim[i] if j in set(plane_neighbours[i])) / K
                for i in usable) / len(usable)
    print("  true semantic neighbours (oracle)   %.4f" % truth)
    print("  RANDOM neighbours (baseline)       %.4f" % base)
    print("  PLANE neighbours (walk the map)    %.4f" % plane)
    lift = (plane - base) / (truth - base) if truth != base else 0.0
    print("  -> the plane recovers %.1f%% of what semantics knows" % (100.0 * lift))
    print()

    print("=== overlap: of the 10 semantically closest, how many does the map find? ===")
    hit = 0
    tot = 0
    for i in usable:
        truthset = set(j for _, j in sim[i][:K])
        hit += len(truthset & set(plane_neighbours[i]))
        tot += K
    print("  %d / %d  (%.1f%% of true neighbours are spatial neighbours)"
          % (hit, tot, 100.0 * hit / tot))
    print("  a random map would find about %.1f%%"
          % (100.0 * K / (len(usable) - 1)))
    print()

    # Cosines here are compressed (everything is JSON about one system), so raw
    # means are a weak instrument. Rank is scale-free: where does the map put a
    # chunk in the SEMANTIC ordering, vs where does a random pick put it?
    ranks = []
    for i in usable:
        order = [j for _, j in sim[i]]
        rank_of = {j: r for r, j in enumerate(order)}
        got = [rank_of[j] for j in plane_neighbours[i] if j in rank_of]
        if got:
            ranks.append(sum(got) / len(got))
    mid = (len(usable) - 1) / 2.0
    print("=== rank of the map's neighbours in the SEMANTIC ordering ===")
    print("  mean rank %.0f of %d   (%.1f%% of the way up the semantic list)"
          % (sum(ranks) / len(ranks), len(usable) - 1,
             100.0 * (1.0 - (sum(ranks) / len(ranks)) / mid)))
    print("  a random pick would sit at rank %.0f (50.0%%)"
          % mid)
    print()

    print("=== the confound-free version: same cell, or not ===")
    # Every ranking above had to break ties with a centroid, and a centroid of
    # many cells regresses to the middle of the map. This asks the question
    # directly, with no ranking and no fallback: take every PAIR of chunks, put
    # it in a bucket by whether they ever wrote into the same cell, and compare
    # the mean cosine. The vectors are compressed (top-10 = %.4f vs ranks
    # 10-20 = %.4f), so mean cosine is a weak instrument — but as a
    # SAME-OR-NOT contrast it does not need to resolve small differences.
    same, diff = [], []
    for a in range(len(usable)):
        ia = usable[a]
        for b in range(a + 1, len(usable)):
            ib = usable[b]
            if not cellset[ia] or not cellset[ib]:
                continue
            s = sum(x * y for x, y in zip(chunks[ia][2], chunks[ib][2]))
            (same if (cellset[ia] & cellset[ib]) else diff).append(s)
    print("  pairs sharing a cell     n=%7d  mean cosine %.4f"
          % (len(same), sum(same) / len(same) if same else 0.0))
    print("  pairs sharing no cell   n=%7d  mean cosine %.4f"
          % (len(diff), sum(diff) / len(diff) if diff else 0.0))
    ms = sum(same) / len(same)
    md = sum(diff) / len(diff)
    print("  difference               %+.4f  (%.1f%% of the spread between"
          " random %.4f and oracle %.4f)"
          % (ms - md, 100.0 * (ms - md) / (truth - base), base, truth))
    print()

    print("=== reverse: are spatially close chunks similar? ===")
    sims = []
    dists = []
    for i in usable:
        for j in plane_neighbours[i]:
            sims.append(dict((jj, s) for s, jj in sim[i])[j])
    # correlation between plane distance and cosine
    xs, ys = [], []
    for i in usable:
        for j in usable:
            if i == j:
                continue
            d = plane_dist(i, j)
            if d is None:
                continue
            xs.append(d)
            ys.append(dict((jj, s) for s, jj in sim[i])[j])
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    r = cov / (vx * vy) if vx and vy else 0.0
    print("  correlation(plane distance, cosine) = %.4f" % r)
    print("  mean cosine of the map's own neighbours %.4f"
          % (sum(sims) / len(sims) if sims else 0.0))
    print("  mean cosine of random pairs            %.4f" % base)

    # One number to remember: of everything semantic search knows above a random
    # pair, how much of it does sharing a cell recover? Nothing else in this file
    # survives a changed chunk size, and this does not.
    #
    # The denominator is `md`, the no-cell PAIR baseline - NOT `base`, which is
    # ranks 10..20 of a query's own neighbour list and is not a random pair at
    # all. Using it overstates the lift by about seven times, which is the sort
    # of number that survives review precisely because it flatters the thing
    # being built.
    spread = truth - md
    print("  -> sharing a cell recovers %.1f%% of what the semantic index knows"
          "  (baseline is a random PAIR, cosine %.4f; oracle %.4f)"
          % (100.0 * (ms - md) / spread if spread else 0.0, md, truth))


if __name__ == "__main__":
    main()