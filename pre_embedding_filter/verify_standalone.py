"""Standalone verifier for magic-context memory_verifications.

Completes what map_standalone.py starts: the mapper writes rows with
verified_at=0 (the plugin's own recordMemoryMapping behaviour), and the
plugin's verify task — which is what clears them — dies on the same
opencode-session HeadersTimeout. This drives the check directly:
read the mapped file, ask the model for evidence, then write the result
with the plugin's recordMemoryVerifications semantics (delete the memory's
rows, re-insert the surviving set with verified_at=now).

  pass -> confirmed files keep their rows, verified_at set
  fail -> all file rows dropped and a sentinel "" row written, i.e. the
          memory is declared file-independent, which is what the plugin
          itself records when no file backs the claim

Usage:
  python verify_standalone.py --dry
  python verify_standalone.py --limit 8
  python verify_standalone.py
  python verify_standalone.py --selftest
"""

import argparse
import os
import re
import subprocess
import sys
import time

from map_standalone import (
    ORIGIN,
    TOKEN_RE,
    TOK_BACKTICK,
    call_model,
    check_schema,
    connect,
    resolve_file,
    ROOTS,
)
import map_standalone as ms  # REJECTS is rebound by load_rejects(), keep the module

# files are read for evidence, not scanned whole
FILE_HEAD = 2000
HIT_CAP = 2500
MAX_FILES = 3
VERIFY_BATCH = 8

SYSTEM = (
    "You verify that a memory record is genuinely backed by a repository file. "
    "Read the memory's claim and the file excerpt. "
    'Output ONLY a manifest: <verifications><memory id="N" verdict="pass"/>'
    "</verifications> with one entry per id, verdict pass or fail. "
    "pass only when the excerpt directly supports the claim (same code, same "
    "constant, same behaviour). fail when the file is unrelated, truncated "
    "beyond usefulness, or the claim is a process/workflow rule rather than a "
    "statement about that file. No prose, no code fences."
)

VER_RE = re.compile(
    r"<memory\b[^>]*?\bid\s*=\s*\"(\d+)\"[^>]*?\bverdict\s*=\s*\"(pass|fail)\"",
    re.I,
)
VER_RE_SWAP = re.compile(
    r"<memory\b[^>]*?\bverdict\s*=\s*\"(pass|fail)\"[^>]*?\bid\s*=\s*\"(\d+)\"",
    re.I,
)


def parse_verdicts(text):
    """Accept either attribute order; returns {id: bool}."""
    out = {}
    for m in VER_RE.finditer(text):
        out[int(m.group(1))] = m.group(2).lower() == "pass"
    for m in VER_RE_SWAP.finditer(text):
        out.setdefault(int(m.group(2)), m.group(1).lower() == "pass")
    return out


def unverified(cur):
    return cur.execute(
        """SELECT DISTINCT m.id, m.category, m.content FROM memories m
            WHERE m.status IN ('active','permanent')
              AND EXISTS (SELECT 1 FROM memory_verifications v
                           WHERE v.memory_id=m.id AND v.file_path<>''
                             AND v.verified_at=0)
            ORDER BY m.id"""
    ).fetchall()


def files_of(cur, mid):
    return [r[0] for r in cur.execute(
        "SELECT file_path FROM memory_verifications WHERE memory_id=? "
        "AND file_path<>''", (mid,))]


def tokens(content, n=8):
    seen, out = set(), []
    for t in TOK_BACKTICK.findall(content or "") + TOKEN_RE.findall(content or ""):
        t = t.strip(".")
        if len(t) < 6 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= n:
            break
    return out


def read_excerpt(content, rel, root):
    """Head of the file plus keyword hits, so evidence is not lost when the
    claim points at line 900 of a 3000-line header."""
    path = os.path.join(root, rel)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(FILE_HEAD)
    except Exception:
        return ""
    toks = tokens(content)
    if not toks:
        return head
    cmd = ["rg", "-n", "-m", "4", "-C", "2", "-F"]
    for t in toks:
        cmd += ["-e", t]
    cmd += [path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        hits = (r.stdout or "").strip()
    except Exception:
        hits = ""
    if hits:
        return head + "\n...\n[keyword hits]\n" + hits[:HIT_CAP]
    return head


def build_prompt(batch):
    """batch = [(mid, cat, content, [(rel, excerpt), ...]), ...]"""
    parts = []
    for mid, cat, content, files in batch:
        block = f"[{mid}] {cat}\nMemory: {(content or '')[:600]}"
        if not files:
            block += "\n(no readable file on disk)"
        for rel, excerpt in files:
            block += f"\n--- file {rel} ---\n{excerpt}"
        parts.append(block)
    return (
        "## Verify memory -> file mappings\n\n"
        "For each memory below decide verdict pass/fail, then output ONE "
        "<verifications> manifest covering every id.\n\n"
        + "\n\n".join(parts)
    )


def verify(con, cur, rows, dry):
    checked = passed = failed = unresolvable = 0
    for b in range(0, len(rows), VERIFY_BATCH):
        chunk = rows[b:b + VERIFY_BATCH]
        prepared = []
        for mid, cat, content in chunk:
            rels = files_of(cur, mid)
            got = []
            for rel in rels[:MAX_FILES]:
                root = resolve_file(rel)
                if root:
                    got.append((rel, read_excerpt(content, rel, root)))
            prepared.append((mid, cat, content, got))
        # a memory whose files are all unreadable cannot be passed
        force_fail = {mid for mid, _c, _ct, got in prepared if not got}
        need_model = [p for p in prepared if p[0] not in force_fail]
        verdicts = {}
        if need_model:
            try:
                verdicts = parse_verdicts(call_model(build_prompt(need_model)))
            except Exception as e:  # noqa: BLE001
                print(f"  batch {b // VERIFY_BATCH}: MODEL FAIL {e}", flush=True)
                continue
        now = int(time.time() * 1000)
        for mid, _cat, _ct, got in prepared:
            verdicts.setdefault(mid, mid in force_fail)
            rels = files_of(cur, mid)
            if not dry:
                # fail is not a verdict, it is a rejection: drop the mapping so
                # the memory goes back to unmapped and remember the files that
                # failed so the mapper cannot pick them again. When every
                # candidate is rejected the mapper marks it file-independent.
                cur.execute("DELETE FROM memory_verifications WHERE memory_id=?", (mid,))
                if verdicts[mid]:
                    kept = sorted({rel for rel, ex in got if ex})
                    if not kept:
                        kept = [""]  # passed but nothing readable: stay mapped
                    for rel in kept:
                        cur.execute(
                            """INSERT INTO memory_verifications
                               (memory_id, file_path, verified_at, mapped_at, mapping_origin)
                               VALUES (?,?,?,?,'standalone')""",
                            (mid, rel, now, now),
                        )
                    passed += 1
                else:
                    if rels:
                        ms.REJECTS[str(mid)] = sorted(set(ms.REJECTS.get(str(mid), []))
                                                      | set(rels))
                        ms.save_rejects()
                    failed += 1
            else:
                passed += verdicts[mid]
                failed += (not verdicts[mid])
            checked += 1
            if mid in force_fail:
                unresolvable += 1
        if not dry:
            con.commit()
        print(f"  batch {b // VERIFY_BATCH + 1}/{(len(rows) + VERIFY_BATCH - 1) // VERIFY_BATCH}: "
              f"checked {checked} pass={passed} fail={failed} "
              f"unreadable={unresolvable}", flush=True)
    return checked, passed, failed, unresolvable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        t = ('<verifications>\n<memory id="1" verdict="pass"/>\n'
             '<memory verdict="fail" id="2"/>\n</verifications>')
        got = parse_verdicts(t)
        assert got == {1: True, 2: False}, got
        assert parse_verdicts("```xml\n" + t + "\n```") == got
        assert resolve_file("core/geo_tess_container.h")
        print("selftest OK:", got)
        return 0

    if args.revert:
        con = connect()
        cur = con.execute(
            "UPDATE memory_verifications SET verified_at=0 WHERE mapping_origin=?",
            (ORIGIN,))
        con.commit()
        print(f"reverted {cur.rowcount} rows to verified_at=0")
        con.close()
        return 0

    con = connect()
    cur = con.cursor()
    check_schema(cur)
    ms.load_rejects()
    rows = unverified(cur)
    print(f"unverified at start: {len(rows)}")
    if args.dry:
        for p in sorted(ROOTS):
            n = cur.execute(
                """SELECT COUNT(DISTINCT m.id) FROM memories m
                    WHERE m.project_path=? AND m.status IN ('active','permanent')
                      AND EXISTS (SELECT 1 FROM memory_verifications v
                                   WHERE v.memory_id=m.id AND v.file_path<>''
                                     AND v.verified_at=0)""",
                (p,)).fetchone()[0]
            if n:
                print(f"  {p}: {n}")
        con.close()
        return 0

    rows = rows[:args.limit] if args.limit else rows
    checked, passed, failed, unresolvable = verify(con, cur, rows, False)
    left = len(unverified(cur))
    print(f"checked={checked} pass={passed} fail={failed} "
          f"unreadable={unresolvable} unverified_left={left}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
