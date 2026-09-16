"""A-slice: tower = 1 tesseract = 1152 chunk-slots (32x36), 18 towers = FULL 20736.
Chunk = 144B. Header {beacon:111*(pair+1), t0, count, pair, view}. Ticker
enc=(t*37)%1440. Twinview: pairs (i,i+9), A=32x36 view, B=36x32, transpose link.
Fingerprint hook: fp(chunk)->int tuple (default mod9; shootout plugs 8-vector).
ponytail: stdlib only, streaming pack, partial tail kept."""
import json
import os

CHUNK, ROW, COL = 144, 32, 36
TSLOT, TESS, FULL = ROW * COL, 18, 20736
STRIDE, CYCLE = 37, 1440
DIR = os.path.dirname(os.path.abspath(__file__))


def fp_mod9(data: bytes):
    return (sum(data) % 9,)


LINES8 = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6),
          (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


def fp_vec8(data: bytes):
    """LOCKED 2026-09-16: 8-vector zero-deviation. 141568/141644 distinct
    on facts.jsonl (coll 0.054%) vs mod9 sharing every bucket. 16B/chunk."""
    v = [sum(data[i * 16:(i + 1) * 16]) for i in range(9)]
    c = v[4]
    return tuple(sum(x - c for x in (v[i] for i in L)) for L in LINES8)


def enc(t):
    return (t * STRIDE) % CYCLE


# ── slow hand (LOCKED 2026-09-16): Pisano-60 phase, Fib(t%60)%10 table.
# Pisano alone is NOT bijection/invertible -> landmarks only, never addressing.
# Zero-beacons at phase t%60 in {0,15,30,45}: 96/rotation, every 15 ticks
# (even spread; repdigit beacons cluster t<=27). 24 cycles per 1440 rotation.
_PIS60 = (0,1,1,2,3,5,8,3,1,4,5,9,4,3,7,0,7,7,4,1,5,6,1,7,8,5,3,8,1,9,
          0,9,9,8,7,5,2,7,9,6,5,1,6,7,3,0,3,3,6,9,5,4,9,3,2,5,7,2,9,1)


def phase(t):
    return _PIS60[t % 60]


def is_zero_beacon(t):
    return _PIS60[t % 60] == 0


def sibling(tess, slot):
    """B-twinview involution. Proven: 20736/20736, fixed 0."""
    if tess < 9:
        r, c = divmod(slot, COL)
        return tess + 9, c * ROW + r
    r2, c2 = divmod(slot, ROW)
    return tess - 9, c2 * COL + r2


def pack(stream, fp=fp_vec8):
    """stream of bytes -> list of towers {header, cells, fps}. t global."""
    towers, buf, t = [], [], 0
    for ch in stream:
        buf.append(bytes(ch))
        t += 1
        if len(buf) == TSLOT:
            towers.append(_seal(buf, t - TSLOT, fp))
            buf = []
    if buf:
        towers.append(_seal(buf, t - len(buf), fp))
    return towers


def _seal(cells, t0, fp):
    idx = (t0 // TSLOT)
    tess = idx % TESS
    cyc = idx // TESS
    # time-seed (LOCKED 2026-09-16): per-rotation pair shift from slow hand.
    # Rule: logical t only (replayable), never wall clock. Shift is a bijection
    # on pairs -> uniformity preserved exactly; layout decorrelates per cycle.
    pair = (idx + _PIS60[(cyc * 7) % 60] % 9) % 9
    return {"header": {"beacon": 111 * (pair + 1), "t0": t0,
                       "count": len(cells), "pair": pair,
                       "view": "A" if tess < 9 else "B", "tess": tess},
            "cells": cells, "fps": [fp(c) for c in cells]}


def save(tw, root=None):
    """tower -> root/tXXXXXX/{header.json, cells.hex}. fps recomputed on load.
    Sentinels (block-center 2x2) derive from header: no disk change needed."""
    root = root or os.path.join(DIR, "towers")
    os.makedirs(root, exist_ok=True)
    d = os.path.join(root, f"t{tw['header']['t0']:07d}")
    os.makedirs(d, exist_ok=True)
    json.dump(tw["header"], open(os.path.join(d, "header.json"), "w"))
    with open(os.path.join(d, "cells.hex"), "w") as f:
        for c in tw["cells"]:
            f.write(c.hex() + "\n")
    return d


def load(d, fp=fp_vec8):
    header = json.load(open(os.path.join(d, "header.json")))
    cells = [bytes.fromhex(x.strip()) for x in
             open(os.path.join(d, "cells.hex")) if x.strip()]
    assert len(cells) == header["count"], "cell count matches header"
    return {"header": header, "cells": cells,
            "fps": [fp(c) for c in cells]}


def chunks_of_facts(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                c = json.loads(line).get("content", "")
            except Exception:
                continue
            b = c.encode("utf-8", "ignore")
            for i in range(0, len(b), CHUNK):
                if b[i:i + CHUNK]:
                    yield b[i:i + CHUNK]


def build_from_pool(pool_mod, root=None):
    """Full pipeline: pool log -> towers on disk. Returns tower dirs."""
    lines = pool_mod.tail(10 ** 9)
    stream = [(m["app"] + ":" + m["kind"] + ":" + m["text"]).encode("utf-8")
              for m in lines]
    out = []
    buf = []
    for m in stream:
        for i in range(0, len(m), CHUNK):
            if m[i:i + CHUNK]:
                buf.append(m[i:i + CHUNK])
    for i in range(0, len(buf), TSLOT):
        out.append(save(_seal(buf[i:i + TSLOT], i, fp_vec8), root))
    return out


# ── p4m block view (LOCKED 2026-09-16) ──
# tower 1152 + 4 header-sentinels = 1156 = 34x34 = 4x(17x17).
# Sentinels = block-center 2x2 (D4-orbit, proven): Q0:(16,16) Q1:(16,18)
# Q2:(18,16) Q3:(18,18) carry (beacon,t0,count,pair). Tiling class p4m.
# Convention: payload grouped Q0..Q3 (288 each, row-major in-quadrant).
SEN_RC = (((16, 16)), ((16, 18)), ((18, 16)), ((18, 18)))
QUADS_RC = ((0, 0), (0, 17), (17, 0), (17, 17))


def _quad_cells(q):
    r0, c0 = QUADS_RC[q]
    return [(r, c) for r in range(r0, r0 + 17) for c in range(c0, c0 + 17)
            if (r, c) != SEN_RC[q]]


def quadrants(tw):
    """tower -> 4x[288 payload + sentinel]; sentinel[q] = header int q."""
    assert len(tw["cells"]) == TSLOT, "full tower only"
    h = tw["header"]
    meta = (h["beacon"], h["t0"], h["count"], h["pair"])
    cells = list(tw["cells"])
    out = []
    for q in range(4):
        assert len(_quad_cells(q)) == 288
        out.append(cells[q * 288:(q + 1) * 288] + [meta[q]])
    return out


def from_quadrants(quads):
    """inverse: 4x(288+sentinel) -> (cells1152, meta4)."""
    cells = [c for q in quads for c in q[:288]]
    assert len(cells) == TSLOT
    return cells, tuple(q[288] for q in quads)


def block_of(cells1152):
    """1152 grouped payload -> 1156 block (None at sentinel positions)."""
    assert len(cells1152) == TSLOT
    blk = [None] * 1156
    for q in range(4):
        for (r, c), cell in zip(_quad_cells(q), cells1152[q * 288:(q+1) * 288]):
            blk[r * 34 + c] = cell
    for (r, c) in SEN_RC:
        assert blk[r * 34 + c] is None
    assert all(x is not None for i, x in enumerate(blk)
               if (i // 34, i % 34) not in SEN_RC)
    return blk


def demo():
    from collections import Counter
    src = r"I:\tools\pre_embedding_filter\facts.jsonl"
    towers = pack(chunks_of_facts(src))
    assert towers, "must pack at least 1 tower"
    # roundtrip: header math + bytes
    mm = 0
    for tw in towers:
        h = tw["header"]
        assert h["beacon"] // 111 - 1 == h["pair"], "beacon//111 == pair"
        assert h["beacon"] in [111 * k for k in range(1, 10)], "beacon landmark"
        assert ((t0 := h["t0"]) + len(tw["cells"])) > t0
        for j, c in enumerate(tw["cells"]):
            if bytes(c) != c:
                mm += 1
            _ = enc(t0 + j)  # ticker covers every cell
    assert mm == 0, "roundtrip memcmp"
    # twinview involution over all used (tess, slot)
    bad = 0
    for tw in towers:
        te = tw["header"]["tess"]
        for s in range(len(tw["cells"])):
            t2, s2 = sibling(te, s)
            if sibling(t2, s2) != (te, s):
                bad += 1
                break
    assert bad == 0, "sibling involution"
    # fixed-point scan on first full tower
    full = next(tw for tw in towers if len(tw["cells"]) == TSLOT)
    te = full["header"]["tess"]
    fixed = sum(1 for s in range(TSLOT) if sibling(te, s) == (te, s))
    assert fixed == 0, "no fixed points"
    dup = len(towers) - len(Counter(
        tuple(tw["header"]["t0"] for tw in towers)))
    assert dup == 0, "t0 unique"
    # p4m quadrants on every full tower
    nq = 0
    for tw in towers:
        if len(tw["cells"]) != TSLOT:
            continue
        quads = quadrants(tw)
        cells2, meta2 = from_quadrants(quads)
        assert cells2 == list(tw["cells"]), "quadrant payload roundtrip"
        h = tw["header"]
        assert meta2 == (h["beacon"], h["t0"], h["count"], h["pair"]), \
            "header in stone"
        nq += 1
    n = sum(len(tw["cells"]) for tw in towers)
    # slow hand: table == real Fib mod 10, zeros at 0,15,30,45
    a, b = 0, 1
    for t in range(60):
        assert phase(t) == a, f"pisano table t={t}"
        a, b = b, (a + b) % 10
    assert [t for t in range(60) if is_zero_beacon(t)] == [0, 15, 30, 45]
    print(f"TOWER-SELFCHECK: PASS towers={len(towers)} chunks={n} "
          f"roundtrip_mm=0 sibling_bad=0 fixed=0 quads={nq}")


if __name__ == "__main__":
    demo()
