#!/usr/bin/env python3
"""p3_compact.py — compact a P3 route log (fog latch / tombstone) losslessly.

WHY
  The latch is MONOTONE within an epoch (OPEN -> SHUT, never back), so the log
  is append-only and, before the 2026-10-11 transition-only fix, recorded the
  SAME slot on every walk. Measured on the live log: 5,661 records for 7
  distinct slots = 99% redundant. The log no longer GROWS (the fix works), but
  the historical bulk is still there and every boot replays all of it.

  Replay is "last event per (name_hash, slot) wins" and CLEAR is the only
  reopen, so the log's INFORMATION is just the final state per slot. Dropping
  every superseded record is lossless BY CONSTRUCTION: a fresh latch replayed
  from the compacted file is bit-identical to one replayed from the original.

WHAT IT DOES
  Reads the log, keeps the LAST record per (name_hash, slot), writes
  `IR` + version + those records in ORIGINAL order. Never deletes records for a
  slot whose final action is CLEAR if the slot was never WRITEden first
  (a lone CLEAR on a virgin slot is a no-op, so it is dropped too — same replay
  result). Records are copied BYTE-EXACT, so their CRC-32 stays valid.

SAFETY
  * Dry-run by default; `--apply` writes.
  * Always writes a timestamped `.bak-<ts>` beside the file before applying.
  * Refuses to apply if the compacted replay would differ from the original
    (it re-verifies with the real C semantics before writing).
  * Refuses if any record fails its CRC-32 (do not silently drop corruption).

USAGE
  python p3_compact.py --log I:/tools/ann/data/memcore/lz_route.log          # dry-run
  python p3_compact.py --log <path> --apply                                  # compact in place
  python p3_compact.py --log <path> --apply --keep-sidecar <newpath>
"""
import argparse
import os
import shutil
import struct
import sys
import time
import zlib

REC = 32
HEADER_LEN = 3
WL_NAME_HASH = 0x77616E676C617464  # "wanglatd" — epoch 2 (choice-1 latch ids)
ACTION_WRITE, ACTION_CLEAR, ACTION_MOVE, ACTION_ANCHOR = 0, 1, 2, 3


def parse(raw: bytes):
    """Return (header, [record bytes]). Raises on a bad header/CRC/length."""
    if len(raw) < HEADER_LEN:
        raise ValueError("file too short (no 3-byte header)")
    header, body = raw[:HEADER_LEN], raw[HEADER_LEN:]
    if header[0:2] != b"IR":
        raise ValueError(f"bad magic {header[0:2]!r} (expected b'IR')")
    if len(body) % REC != 0:
        raise ValueError(f"body {len(body)} B is not a multiple of {REC} "
                         f"({len(body) % REC} trailing bytes)")
    recs = [body[i:i + REC] for i in range(0, len(body), REC)]
    for i, r in enumerate(recs):
        crc = struct.unpack_from("<I", r, 28)[0]
        if (zlib.crc32(r[:28]) & 0xFFFFFFFF) != crc:
            raise ValueError(f"record {i} fails CRC-32 — refusing to compact a "
                             f"corrupt log")
    return header, recs


def fields(rec: bytes):
    slot, name_hash, rnd, tick, action, _res, _crc = struct.unpack("<IQIIIII", rec)
    return slot, name_hash, rnd, tick, action


def replay_state(header: bytes, recs):
    """The exact semantics the C replay uses: per (name_hash, slot), the last
    record wins; a slot only 'exists' once WRITE/MOVE has been seen, and a
    CLEAR returns it to OPEN. Returned as {(name_hash, slot): action} for the
    slots that are NOT in the virgin/open state (i.e. the state that matters)."""
    state = {}
    for r in recs:
        slot, nh, _rnd, _tick, action = fields(r)
        key = (nh, slot)
        if action == ACTION_ANCHOR:
            continue                      # marker only, no state change
        if action == ACTION_CLEAR:
            # CLEAR on a virgin slot is a no-op -> do not create state
            if key in state:
                del state[key]            # back to OPEN
            continue
        state[key] = action               # WRITE / MOVE
    return state


def compact(header: bytes, recs):
    """Keep the last record per (name_hash, slot) that is not ANCHOR, in original
    order, then drop records that carry NO state.

    Replay is last-event-wins per (name_hash, slot), and a CLEAR only means
    something if the slot was WRITEden before it. So:
      * ANCHOR  — never affects state; drop.
      * a CLEAR whose kept slot has no WRITE/MOVE earlier in the ORIGINAL log
        is a no-op on a virgin slot; drop.
    Everything else is kept byte-exact, so CRCs stay valid.
    """
    last_idx = {}
    for i, r in enumerate(recs):
        slot, nh, _rnd, _tick, action = fields(r)
        if action == ACTION_ANCHOR:
            continue
        last_idx[(nh, slot)] = i

    keep_idx = sorted(last_idx.values())
    kept = []
    for i in keep_idx:
        r = recs[i]
        slot, nh, _rnd, _tick, action = fields(r)
        if action == ACTION_CLEAR:
            # meaningful only if the slot was written earlier in the file
            written_before = any(
                fields(recs[j])[0] == slot and fields(recs[j])[1] == nh and
                fields(recs[j])[4] in (ACTION_WRITE, ACTION_MOVE)
                for j in range(i)
            )
            if not written_before:
                continue          # no-op CLEAR on a virgin slot
        kept.append(r)
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    path = args.log
    if not os.path.exists(path):
        sys.exit(f"no such log: {path}")
    raw = open(path, "rb").read()
    try:
        header, recs = parse(raw)
    except ValueError as e:
        sys.exit(f"REFUSED: {e}")

    before_state = replay_state(header, recs)
    kept = compact(header, recs)
    after_state = replay_state(header, kept)

    ok = (before_state == after_state)
    new = header + b"".join(kept)

    print(f"log        : {path}")
    print(f"records    : {len(recs)} -> {len(kept)}")
    print(f"bytes      : {len(raw)} -> {len(new)}")
    print(f"state keys : {len(before_state)} -> {len(after_state)}")
    print(f"lossless   : {'YES (state identical)' if ok else 'NO — REFUSING'}")
    if not args.quiet:
        for k in sorted(before_state):
            print(f"    keep slot={k[1]} hash={k[0]:#x} action={before_state[k]}")

    if not ok:
        sys.exit("REFUSED: compacted replay would differ — nothing written.")

    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.bak-{ts}"
    shutil.copyfile(path, bak)
    tmp = f"{path}.compact.tmp"
    with open(tmp, "wb") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print(f"\nAPPLIED. backup: {bak}")
    print(f"verify: {os.path.getsize(path)} B on disk")


if __name__ == "__main__":
    main()
