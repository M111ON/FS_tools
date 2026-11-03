"""Compact Identity: lossless encoding comparison."""
import sys, time, random
sys.path.insert(0, '.')
from snake_sim import Snake, seed_rng

# ── Encoding schemes ────────────────────────────────────────────────────────

def encode_baseline(letters):
    """Original: list of 3 tuples, each (4-letter tuple, frame char)."""
    return letters  # no encoding, just reference

def encode_string(letters):
    """Compact: concatenate 4 letters + frame into 5-char string per axis."""
    parts = []
    for rv in letters:
        letters_str = ''.join(rv[0])
        frame = rv[1]
        parts.append(letters_str + frame)
    return ''.join(parts)  # 15 chars total

def decode_string(s):
    """Decode string back to original format."""
    result = []
    for i in range(0, 15, 5):
        chunk = s[i:i+5]
        letters = tuple(chunk[:4])
        frame = chunk[4]
        result.append((letters, frame))
    return result

def encode_bytes(letters):
    """Compact: pack letters (A-Z -> 0-25, 5 bits) + frame (a-h -> 0-7, 3 bits) into bytes."""
    # Each axis: 4 letters (4 bytes) + 1 frame (1 byte) = 5 bytes
    # Total: 15 bytes
    buf = bytearray()
    for rv in letters:
        for ch in rv[0]:
            buf.append(ord(ch) - ord('A'))
        buf.append(ord(rv[1]) - ord('a'))
    return bytes(buf)

def decode_bytes(b):
    """Decode bytes back to original format."""
    result = []
    for i in range(0, 15, 5):
        letters = tuple(chr(b[i+j] + ord('A')) for j in range(4))
        frame = chr(b[i+4] + ord('a'))
        result.append((letters, frame))
    return result

def encode_int(letters):
    """Compact: pack into single Python int (69 bits)."""
    val = 0
    for rv in letters:
        for ch in rv[0]:
            val = (val << 5) | (ord(ch) - ord('A'))
        val = (val << 3) | (ord(rv[1]) - ord('a'))
    return val

def decode_int(val):
    """Decode int back to original format."""
    # Extract in reverse
    frames = []
    letter_groups = []
    for _ in range(3):
        frame = val & 0x7
        val >>= 3
        letters = []
        for _ in range(4):
            letters.append(chr((val & 0x1F) + ord('A')))
            val >>= 5
        letters.reverse()
        letter_groups.append((tuple(letters), chr(frame + ord('a'))))
    letter_groups.reverse()
    return letter_groups

# ── Lossless verification ───────────────────────────────────────────────────
def verify_lossless():
    print("=" * 70)
    print("LOSSLESS VERIFICATION")
    print("=" * 70)
    import random
    r = random.Random(42)
    # generate random letters
    test_cases = []
    for _ in range(1000):
        letters = []
        for _ in range(3):
            l = tuple(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
            f = r.choice("abcdefgh")
            letters.append((l, f))
        test_cases.append(letters)

    all_ok = True
    for tc in test_cases:
        # string
        enc = encode_string(tc)
        dec = decode_string(enc)
        if dec != tc:
            print(f"  String FAIL: {tc} != {dec}")
            all_ok = False
        # bytes
        enc = encode_bytes(tc)
        dec = decode_bytes(enc)
        if dec != tc:
            print(f"  Bytes FAIL: {tc} != {dec}")
            all_ok = False
        # int
        enc = encode_int(tc)
        dec = decode_int(enc)
        if dec != tc:
            print(f"  Int FAIL: {tc} != {dec}")
            all_ok = False
    print(f"  All 1000 test cases lossless: {all_ok}")
    return all_ok

# ── Memory comparison ───────────────────────────────────────────────────────
def bench_memory():
    print("\n" + "=" * 70)
    print("MEMORY COMPARISON (n=5000)")
    print("=" * 70)
    n = 5000
    snake = Snake("mem", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)

    import sys as _sys
    # Baseline: original letters
    baseline_bytes = 0
    for pos, val in snake.letters.items():
        baseline_bytes += _sys.getsizeof(val)
        for rv in val:
            if isinstance(rv, tuple):
                baseline_bytes += _sys.getsizeof(rv)
                if isinstance(rv[0], tuple):
                    baseline_bytes += _sys.getsizeof(rv[0])

    # String encoding
    str_encoded = {}
    for pos, val in snake.letters.items():
        str_encoded[pos] = encode_string(val)
    str_bytes = sum(_sys.getsizeof(v) for v in str_encoded.values())

    # Bytes encoding
    bytes_encoded = {}
    for pos, val in snake.letters.items():
        bytes_encoded[pos] = encode_bytes(val)
    bytes_bytes = sum(_sys.getsizeof(v) for v in bytes_encoded.values())

    # Int encoding
    int_encoded = {}
    for pos, val in snake.letters.items():
        int_encoded[pos] = encode_int(val)
    int_bytes = sum(_sys.getsizeof(v) for v in int_encoded.values())

    print(f"  Baseline (tuples):  {baseline_bytes:>10} B  ({baseline_bytes/n:.1f} B/cube)")
    print(f"  String encoding:    {str_bytes:>10} B  ({str_bytes/n:.1f} B/cube)  ratio={baseline_bytes/str_bytes:.1f}x")
    print(f"  Bytes encoding:     {bytes_bytes:>10} B  ({bytes_bytes/n:.1f} B/cube)  ratio={baseline_bytes/bytes_bytes:.1f}x")
    print(f"  Int encoding:       {int_bytes:>10} B  ({int_bytes/n:.1f} B/cube)  ratio={baseline_bytes/int_bytes:.1f}x")

# ── Encode/Decode Time ──────────────────────────────────────────────────────
def bench_encode_decode():
    print("\n" + "=" * 70)
    print("ENCODE/DECODE TIME (n=5000)")
    print("=" * 70)
    n = 5000
    snake = Snake("ed", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)

    # String
    t0 = time.perf_counter()
    str_enc = {pos: encode_string(val) for pos, val in snake.letters.items()}
    t_enc_str = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    str_dec = {pos: decode_string(v) for pos, v in str_enc.items()}
    t_dec_str = (time.perf_counter() - t0) * 1000

    # Bytes
    t0 = time.perf_counter()
    bytes_enc = {pos: encode_bytes(val) for pos, val in snake.letters.items()}
    t_enc_bytes = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    bytes_dec = {pos: decode_bytes(v) for pos, v in bytes_enc.items()}
    t_dec_bytes = (time.perf_counter() - t0) * 1000

    # Int
    t0 = time.perf_counter()
    int_enc = {pos: encode_int(val) for pos, val in snake.letters.items()}
    t_enc_int = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    int_dec = {pos: decode_int(v) for pos, v in int_enc.items()}
    t_dec_int = (time.perf_counter() - t0) * 1000

    print(f"  {'scheme':<10} {'encode_ms':>10} {'decode_ms':>10} {'total_ms':>10} {'per_cube_us':>12}")
    print(f"  {'string':<10} {t_enc_str:>10.2f} {t_dec_str:>10.2f} {t_enc_str+t_dec_str:>10.2f} {(t_enc_str+t_dec_str)/n*1000:>12.2f}")
    print(f"  {'bytes':<10} {t_enc_bytes:>10.2f} {t_dec_bytes:>10.2f} {t_enc_bytes+t_dec_bytes:>10.2f} {(t_enc_bytes+t_dec_bytes)/n*1000:>12.2f}")
    print(f"  {'int':<10} {t_enc_int:>10.2f} {t_dec_int:>10.2f} {t_enc_int+t_dec_int:>10.2f} {(t_enc_int+t_dec_int)/n*1000:>12.2f}")

# ── Restore Time with Compact Encoding ──────────────────────────────────────
def bench_restore_compact():
    print("\n" + "=" * 70)
    print("RESTORE TIME: Baseline vs Compact (n=5000)")
    print("=" * 70)
    n = 5000
    snake = Snake("rc", mode='chain', domain_size=n+100)
    for _ in range(n):
        snake.grow(0)

    # Baseline restore (store original letters)
    state_base = snake.state_dict()
    # compact: store bytes-encoded letters
    compact_letters = {pos: encode_bytes(val) for pos, val in snake.letters.items()}
    state_compact = dict(state_base)
    state_compact['letters'] = {",".join(str(x) for x in pos): list(v) for pos, v in compact_letters.items()}
    # mark as compact
    state_compact['compact'] = 'bytes'

    # Baseline restore
    t0 = time.perf_counter()
    s_base = Snake("rc", mode='chain', domain_size=n+100)
    s_base.restore(state_base)
    t_base = (time.perf_counter() - t0) * 1000
    ok_base, tot_base = s_base.check_junctions()

    # Compact restore (decode bytes)
    t0 = time.perf_counter()
    s_comp = Snake("rc", mode='chain', domain_size=n+100)
    s_comp.positions = [tuple(p) for p in state_compact['positions']]
    s_comp.visited = set(s_comp.positions)
    s_comp.current_domain = state_compact['current_domain']
    s_comp.growth_count = state_compact['growth_count']
    s_comp.letters = {}
    for key, val in state_compact['letters'].items():
        parts = key.split(",")
        pos = tuple(int(x) for x in parts)
        s_comp.letters[pos] = decode_bytes(bytes(val))
    t_comp = (time.perf_counter() - t0) * 1000
    ok_comp, tot_comp = s_comp.check_junctions()

    print(f"  Baseline restore: {t_base:.2f}ms  junctions={ok_base}/{tot_base}")
    print(f"  Compact restore:  {t_comp:.2f}ms  junctions={ok_comp}/{tot_comp}")
    print(f"  Speedup: {t_base/t_comp:.2f}x")
    print(f"  Identity preserved: {s_base.letters == s_comp.letters}")

if __name__ == "__main__":
    verify_lossless()
    bench_memory()
    bench_encode_decode()
    bench_restore_compact()
    print("\n" + "=" * 70)
    print("COMPACT IDENTITY COMPLETE")
    print("=" * 70)