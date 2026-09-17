"""bond_scale.py — what does reading the bond graph cost, and does it stay correct?

Three questions, measured rather than guessed:

  1. cost   — reading and parsing the log per refresh, per client
  2. truth  — a capped read drops old edges silently, and their endpoints
              lose the address the plane needs to draw them at
  3. where  — is an edge index worth having, and which of the three candidate
              homes for it (this process's memory / a side file / pool.db)
              does the measurement actually justify

Runs entirely against a synthetic log in a temp dir. The live log is never
touched (same rule as the demo).

    python bond_scale.py                 # default sizes
    python bond_scale.py --chats 4000 --bonds 1000
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import pool  # noqa: E402


def _line(app, kind, text, prev, t, to="all"):
    """Same shape append() writes, so the benchmark measures the real format."""
    raw = (app + kind + text).encode("utf-8")
    rk = pool.rkey(raw)
    h = hashlib.sha256((prev + app + kind + to + text).encode("utf-8")).hexdigest()
    return ({"v": 3, "t": t, "app": app, "kind": kind, "text": text, "to": to,
             "tags": pool._extract_tags(text), "prev": prev, "rkey": rk,
             "enc": rk % 1440, "hash": h}, h)


def build(path, chats, bonds, on=("",)):
    """Write `chats` chat lines with `bonds` bond lines woven in.
    `on` selects where bonds go: 'early', 'late', or both (default)."""
    prev, t = "GENESIS", 1000.0
    hashes = []
    with open(path, "w", encoding="utf-8") as f:
        for i in range(chats + bonds):
            if i < chats:
                body, h = _line("bench", "chat", "line %d" % i, prev, t)
                hashes.append(h)
            else:
                src = hashes[(i - chats) % max(len(hashes), 1)]
                dst = hashes[(i * 7 + 3) % max(len(hashes), 1)]
                body, h = _line("bench", "bond", "%s uses %s" % (src, dst), prev, t)
            # interleave so some edges are old and some are new
            prev = h
            t += 0.001
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
    if on == ("late",):                      # rewrite: move bonds to the end
        pass
    return hashes


def build_interleaved(path, chats, bonds):
    """Bonds spread through the log, so `chats` lines come after the first ones."""
    prev, t = "GENESIS", 1000.0
    hashes, first_bond = [], None
    step = max(chats // max(bonds, 1), 1) if bonds else chats
    b = 0
    for i in range(chats + bonds):
        if bonds and b < bonds and i % (step + 1) == step:
            src = hashes[len(hashes) // 3] if hashes else "GENESIS" * 8
            dst = hashes[-1] if hashes else "GENESIS" * 8
            body, h = _line("bench", "bond", "%s uses %s" % (src, dst), prev, t)
            b += 1
            if first_bond is None:
                first_bond = h
        else:
            body, h = _line("bench", "chat", "line %d" % i, prev, t)
            hashes.append(h)
        prev = h
        t += 0.001
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
    return hashes, first_bond


def ms(fn, *a, **k):
    t0 = time.perf_counter()
    out = fn(*a, **k)
    return (time.perf_counter() - t0) * 1000.0, out


def run(mod, chats=4000, bonds=1000):
    """Measure the current read path against a synthetic log."""
    tmp = tempfile.mkdtemp(prefix="bond-scale-")
    try:
        log = os.path.join(tmp, "log.jsonl")
        open(log, "w").close()
        hashes, first_bond = build_interleaved(log, chats, bonds)
        mod.LOG = log
        mod.STATE = os.path.join(tmp, ".flush_state.json")
        n_lines = chats + bonds
        rep = {"log_lines": n_lines, "bonds_expected": bonds}

        rep["tail1000_ms"], t1 = ms(mod.tail, 1000)
        rep["tail20_ms"], _ = ms(mod.tail, 20)
        rep["tail_sees_lines"] = len(t1)

        mod._DERIVED["key"] = None          # cold: rebuild the derived index
        rep["cold_ms"], edges = ms(mod.all_bonds)
        rep["warm_ms"], _ = ms(mod.all_bonds)   # warm: cache hit, the common case
        rep["all_bonds_found"] = len(edges)
        rep["all_bonds_complete"] = len(edges) == bonds
        rep["all_bonds_unresolved_ends"] = sum(
            1 for e in edges if e["src_rkey"] is None or e["dst_rkey"] is None)
        rep["derived_mb"] = round(len(mod._DERIVED.get("timeline", [])) * 120 / 1048576.0, 1)

        # bonds_for takes an ENDPOINT hash, not the bond's own hash — querying
        # the bond itself returns 0 by design (a mistake worth not repeating in
        # the benchmark: it made the old code look broken for the wrong reason)
        if edges:
            rep["bonds_for_oldest_ms"], old = ms(mod.bonds_for, edges[0]["src"][:8])
            rep["bonds_for_oldest_found"] = len(old)
        rep["bonds_for_newest_ms"], new = ms(mod.bonds_for, hashes[-1])
        rep["bonds_for_newest_found"] = len(new)

        # ---- what an interactive message costs: append, then read the graph --
        # This is the case that decides whether the index is worth having.
        mod._DERIVED["key"] = None
        mod.derived()                        # dashboard is open, index warm
        mod.all_bonds()                      # ...and has drawn the graph once
        rep["append_chat_ms"], _ = ms(mod.append, "bench", "chat", "interactive message")
        rep["after_chat_append_ms"], e2 = ms(mod.all_bonds)
        rep["after_chat_append_edges"] = len(e2)
        mod.append("bench", "bond", "%s uses %s" % (hashes[0], hashes[1]))
        rep["after_bond_append_ms"], e3 = ms(mod.all_bonds)
        rep["after_bond_append_edges"] = len(e3)
        rep["edges_complete_after_append"] = len(e2) == bonds and len(e3) == bonds + 1
        mod._DERIVED["key"] = None           # what dropping the cache would cost
        rep["if_invalidated_ms"], _ = ms(mod.all_bonds)

        # what a COMPLETE read would cost (streaming, no cap) — the fix shape
        def full_scan():
            n, ks = 0, 0
            with open(log, encoding="utf-8") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    m = json.loads(raw)
                    n += 1
                    if m.get("kind") == "bond":
                        ks += 1
            return n, ks

        rep["full_scan_ms"], got = ms(full_scan)
        rep["full_scan_lines"], rep["full_scan_bonds"] = got
        rep["log_mb"] = round(os.path.getsize(log) / 1048576.0, 2)
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if "--chats" in argv:
        rows = [run(pool, int(argv[argv.index("--chats") + 1]),
                    int(argv[argv.index("--bonds") + 1]))]
    else:
        rows = [run(pool, c, b) for c, b in (
            (500, 100), (2000, 500), (5000, 1000), (20000, 2000), (50000, 5000))]
    print("%7s %6s | %8s %9s | %8s %7s | %6s | %6s | %s" % (
        "lines", "bonds", "tail(20)", "tail(1000)", "cold", "warm",
        "append", "edges", "derived"))
    for r in rows:
        print("%7d %6d | %7.1fms %8.1fms | %7.1fms %6.2fms | %5.2fms | %6d | %5.1f MB %s" % (
            r["log_lines"], r["bonds_expected"], r["tail20_ms"], r["tail1000_ms"],
            r["cold_ms"], r["warm_ms"], r["after_chat_append_ms"],
            r["all_bonds_found"], r["derived_mb"],
            "OK" if r["all_bonds_complete"] else "TRUNCATED"))
    last = rows[-1]
    print("\ntail(1000) is the NEWEST 1000 lines, not the history — %.1f ms at 55000 lines"
          " and %.1f ms at 2500, i.e. it no longer scales with the file."
          % (last["tail1000_ms"], rows[1]["tail1000_ms"]))
    print("A complete read is the number that does scale: %.1f ms at %d lines (%.2f MB),"
          % (last["full_scan_ms"], last["log_lines"], last["log_mb"]))
    print("all of it parsing — which is exactly what an index exists to pay once.\n")
    print("cost of one read of the bond graph, at %d lines / %d bonds:" % (
        last["log_lines"], last["bonds_expected"]))
    print("  cold (build)          %7.1f ms  -> %d/%d edges, %d ends unresolved" % (
        last["cold_ms"], last["all_bonds_found"], last["bonds_expected"],
        last["all_bonds_unresolved_ends"]))
    print("  warm (hit)            %7.2f ms" % last["warm_ms"])
    print("  bonds_for(endpoint)   %7.2f ms -> %d edges" % (
        last["bonds_for_oldest_ms"], last["bonds_for_oldest_found"]))
    print("  one chat append, then read:  %6.2f ms  (%d edges)  <- %.0fx cheaper" % (
        last["after_chat_append_ms"], last["after_chat_append_edges"],
        last["if_invalidated_ms"] / max(last["after_chat_append_ms"], 1e-6)))
    print("  one bond append, then read:  %6.2f ms  (%d edges)  <- edge set changed" % (
        last["after_bond_append_ms"], last["after_bond_append_edges"]))
    print("  if the append dropped the index instead: %.1f ms <- the old behaviour" %
          last["if_invalidated_ms"])
    print("  index in memory       %7.1f MB, per process, never written to disk" % last["derived_mb"])
    print("\nverdict — an edge index is worth it, and it belongs in the reading")
    print("process, folded from the append-only log (never persisted, never")
    print("authoritative: the log is). Persisting it is only worth doing once a")
    print("cold start hurts, and the home for that already exists — pool.db, the")
    print("materialized view the hourly flush rebuilds.")
    ok = (all(r["all_bonds_complete"] and r["edges_complete_after_append"] for r in rows)
          and last["after_chat_append_ms"] < last["cold_ms"] / 10)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
