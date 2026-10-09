#!/usr/bin/env python3
"""kv_pool_bridge.py — KV frames over the chat-pool log.

The standalone DWGLS tooling (core/kv_temporal.h, tools/kv_temporal_llama.c)
already proves the model: a chat turn is a seekable FRAME, stored as a tiny
token delta, with a full anchor base every K frames. This file is the pool
side of that model — it lets the SAME records ride the production chat-pool
log, without changing pool.py's core:

  * record_file(...)  — append a frame delta / anchor record as an ordinary
                        pool line (kind "kv-delta" / "kv-anchor"). The line
                        text is JSON with the frame's fields; the anchor's
                        heavy base BLOB is NOT put in the log — it lives in
                        a side blob file and the line carries {off,len,fnv}.
  * iter_kv(...)      — read those lines back (kind filter) in append order.
  * seek(...)         — nearest-anchor plan for a target turn: which anchor
                        to resume and which deltas to replay. Pure logic,
                        same rule as core/kv_temporal.h:kvt_seek.

WHY A SIDE BLOB FILE: a base is ~626 KB (observed, K8V4). Inlining it would
make the pool index and every /tail read pay for KV bytes. The log carries
addresses; bytes stay out. This mirrors pool's own split (log = addresses,
content elsewhere).

WHY kind "kv-anchor"/"kv-delta" ARE SKIPPED BY THE EMBEDDER: they are machine
records like bond/pointer — see INDEX_SKIP_KINDS in pool.py. This module
lists them in KV_KINDS; add them there so push_outbox does not embed them.
"""
from __future__ import annotations

import json
import os
import struct

import pool

KV_KINDS = ("kv-anchor", "kv-delta")
KV_APP = "kv-temporal"
ANCHOR_EVERY = 8            # must match KVT_ANCHOR_EVERY in core/kv_temporal.h

_BLOB_PATH = os.path.join(pool.DIR, "kv_blobs.bin")
# KVDeltaHdr / KVTAnchor as laid out in core/kv_temporal.h — kept here so a
# C host and this bridge agree on the record shape without linking.
_DELTA_HDR = struct.Struct("<I Q i i")        # turn, prefix_hash, parent, n_tokens
_ANCHOR = struct.Struct("<i I I Q Q Q i")     # turn, base_n, tk, tv, fnv, off, len, n_back


# ── blob side file ────────────────────────────────────────────────────────

def blob_append(data: bytes) -> tuple[int, int, int]:
    """Append base bytes; return (offset, length, fnv1a)."""
    off = os.path.getsize(_BLOB_PATH) if os.path.exists(_BLOB_PATH) else 0
    with open(_BLOB_PATH, "ab") as f:
        f.write(data)
    return off, len(data), _fnv1a(data)


def blob_read(off: int, length: int) -> bytes:
    with open(_BLOB_PATH, "rb") as f:
        f.seek(off)
        return f.read(length)


def _fnv1a(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


# ── records on the pool log ───────────────────────────────────────────────

def frame_delta(turn: int, tokens: list[int], parent: int = 0) -> str:
    """Append one frame's token delta. Returns the pool line hash."""
    text = json.dumps({"turn": turn, "parent": parent, "tokens": tokens},
                      separators=(",", ":"))
    return pool.append(KV_APP, "kv-delta", text)


def frame_anchor(turn: int, base_n_tokens: int, blob: bytes,
                 type_k: int = 8, type_v: int = 2) -> str:
    """Append an anchor record; the base bytes go to the side blob file.
    Returns the pool line hash."""
    off, length, fnv = blob_append(blob)
    text = json.dumps({"turn": turn, "base_n_tokens": base_n_tokens,
                       "type_k": type_k, "type_v": type_v,
                       "blob_off": off, "blob_len": length, "blob_fnv": fnv},
                      separators=(",", ":"))
    return pool.append(KV_APP, "kv-anchor", text)


def iter_kv(kind: str | None = None):
    """Yield KV records in append order: dicts with 'kind' merged in.
    Reads the real log through pool._iter_lines (streaming, no full parse
    of history beyond what we ask for)."""
    for m in pool._iter_lines(reverse=True):
        k = m.get("kind")
        if k not in KV_KINDS:
            continue
        if kind and k != kind:
            continue
        try:
            body = json.loads(m.get("text", "{}"))
        except json.JSONDecodeError:
            continue
        body["kind"] = k
        body["hash"] = m.get("hash")
        body["t"] = m.get("t")
        yield body


def collect():
    """All KV records split into (deltas, anchors), each ordered by turn."""
    deltas, anchors = [], []
    for r in iter_kv():
        (anchors if r["kind"] == "kv-anchor" else deltas).append(r)
    deltas.sort(key=lambda r: r["turn"])
    anchors.sort(key=lambda r: r["turn"])
    return deltas, anchors


# ── seek plan (same rule as kvt_seek in the C header) ─────────────────────

def seek(anchors: list[dict], want: int) -> dict:
    """Nearest anchor with turn <= want; replay deltas from anchor+1..want."""
    best = None
    for a in anchors:
        if a["turn"] <= want and (best is None or a["turn"] > best["turn"]):
            best = a
    if best is None:
        return {"have_anchor": False, "first_delta": 1, "last_delta": want,
                "n_deltas": max(0, want), "total_tokens": 0}
    first = best["turn"] + 1
    return {"have_anchor": True, "anchor": best, "first_delta": first,
            "last_delta": want, "n_deltas": max(0, want - first + 1),
            "total_tokens": best["base_n_tokens"]}


def verify_blob(anchor: dict) -> bool:
    """Re-read the anchor blob and check its FNV matches the record."""
    data = blob_read(anchor["blob_off"], anchor["blob_len"])
    return len(data) == anchor["blob_len"] and _fnv1a(data) == anchor["blob_fnv"]


def main():
    """Self-check in a temp dir — never touches the live log."""
    import tempfile
    import shutil

    tmp = tempfile.mkdtemp(prefix="kv_pool_bridge_")
    global _BLOB_PATH
    saved_dir, saved_log, saved_blob = pool.DIR, pool.LOG, _BLOB_PATH
    try:
        pool.DIR = tmp
        pool.LOG = os.path.join(tmp, "log.jsonl")
        _BLOB_PATH = os.path.join(tmp, "kv_blobs.bin")

        passes = failures = 0

        def check(cond, msg):
            nonlocal passes, failures
            if cond:
                passes += 1
            else:
                failures += 1
                print(f"  FAIL: {msg}")

        N, K = 20, ANCHOR_EVERY
        for t in range(1, N + 1):
            toks = [t * 1000 + i for i in range(4 + (t % 5))]
            if t % K == 0:
                blob = bytes((t * 7 + i) & 0xFF for i in range(tokens_len(toks) * 1024))
                frame_anchor(t, base_n_tokens=t, blob=blob, type_k=8, type_v=2)
            frame_delta(t, toks, parent=(t if t % K == 0 else 0))

        deltas, anchors = collect()
        print(f"tempdir frames: {len(deltas)} deltas, {len(anchors)} anchors")

        check(len(deltas) == N, "one delta per frame")
        check(len(anchors) == N // K, "one anchor every K")

        # every delta's tokens round-trip through the pool log
        by_turn = {d["turn"]: d for d in deltas}
        rt = all(by_turn[t]["tokens"] == [t * 1000 + i for i in range(4 + (t % 5))]
                 for t in range(1, N + 1))
        check(rt, "every delta's tokens round-trip via the pool log")

        # anchor blobs verify (fnv) and content is byte-exact
        ok = True
        for a in anchors:
            if not verify_blob(a):
                ok = False
            data = blob_read(a["blob_off"], a["blob_len"])
            nt = 4 + (a["turn"] % 5)
            want = bytes(((a["turn"] * 7 + i) & 0xFF) for i in range(nt * 1024))
            if data != want:
                ok = False
        check(ok, "anchor blob round-trip byte-exact (fnv + content)")

        # seek plan
        s = seek(anchors, 13)
        check(s["have_anchor"] and s["anchor"]["turn"] == 8, "seek(13) -> anchor@8")
        check(s["first_delta"] == 9 and s["n_deltas"] == 5, "seek(13) -> replay t9..t13")
        s = seek(anchors, 5)
        check(not s["have_anchor"] and s["first_delta"] == 1, "seek(5) -> no anchor")
        s = seek(anchors, 16)
        check(s["anchor"]["turn"] == 16 and s["n_deltas"] == 0, "seek(16) -> anchor itself")
        s = seek(anchors, 20)
        check(s["anchor"]["turn"] == 16 and s["n_deltas"] == 4, "seek(20) -> anchor@16 +4")

        # the log lines are ordinary pool lines with a valid hash chain
        chain = pool.verify_chain() if hasattr(pool, "verify_chain") else None
        if chain is None:
            prev = "GENESIS"
            chain = True
            for m in pool._iter_lines():
                if m.get("prev") != prev:
                    chain = False
                    break
                prev = m.get("hash")
        check(chain, "pool hash chain stays contiguous with KV lines in it")

        print(f"=== kv_pool_bridge: {passes} PASS, {failures} FAIL ===")
        return 0 if failures == 0 else 1
    finally:
        pool.DIR, pool.LOG = saved_dir, saved_log
        _BLOB_PATH = saved_blob
        shutil.rmtree(tmp, ignore_errors=True)


def tokens_len(toks):
    return len(toks)


if __name__ == "__main__":
    raise SystemExit(main())
