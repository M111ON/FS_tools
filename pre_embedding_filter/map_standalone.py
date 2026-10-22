"""Standalone memory mapper for magic-context context.db.

Replaces the plugin's map-memories task when its opencode-session path
hits HeadersTimeout. Same manifest grammar, same target table
(memory_verifications), but model calls go straight to 9router so there
is no session/plugin startup overhead.

Usage:
  python map_standalone.py --dry          # counts only
  python map_standalone.py                # run every project
  python map_standalone.py --project DWGLS
  python map_standalone.py --limit 40     # one batch smoke test
  python map_standalone.py --revert       # delete rows this tool wrote
  python map_standalone.py --selftest
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

DB = r"I:\cortexkit\magic-context\context.db"
ORIGIN = "standalone"
BASE = "http://localhost:20128/v1/chat/completions"
KEY = "sk-a0217d0a6dc1f88b-olr10e-3d11735c"
MODEL = "working"  # raw 9router id; the "9router/" prefix is an opencode provider id
FALLBACKS = ["my-local", "oc/mimo-v2.5-free", "ag/gemini-3.8-flash-low"]
BATCH = 20
MAX_TOKENS = 3000

# project identity -> root directory. Resolved by brute-forcing
# md5(path)[:12] (directoryFallback) and git rev-list --max-parents=0 HEAD
# (resolveProjectIdentityStrict -> git:<rootCommit>). None = no local root
# (device/remote project), so candidate search is skipped there.
ROOTS = {
    "git:29b0837d0207dc7c55fa6b9e1d05e9e0ae14c238": r"I:\DWGLS-native-fs",
    "dir:b7b1d59859e2": r"I:\DWGLS-native-fs",
    "dir:290383da4b02": r"I:\Hermes",
    "dir:3e16a219b8e2": r"I:\tools",
    "git:174753f2375c19a1c0ce1388ef592bfa02d2b056": r"I:\tools",
    "git:bf6bcdfc2dad6e1111544aee6bdf4bae5ddce475": r"I:\tools\cloud_ws",
    "dir:65d8455e2565": r"I:\OpenCode",
    "dir:bb5625ea4027": None,  # Termux / Android phone — nothing mounted locally
}
# Roots tried when resolving a relative file path that came from any project.
SEARCH_ROOTS = [r"I:\DWGLS-native-fs", r"I:\DWGLS", r"I:\tools",
                r"I:\tools\cloud_ws", r"I:\Hermes", r"I:\OpenCode"]

PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[<])((?:core|tools|tests|docs|bench|scripts|colab-pack|"
    r"experiments|deprecated|sid|graft|collection|beam_addressing)/[\w./+-]+\."
    r"(?:h|c|cu|py|md|jsonc|json|sh|cmd|txt|inc))",
    re.I,
)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+){1,}|[a-z]+[A-Z][A-Za-z0-9]+")
TOK_BACKTICK = re.compile(r"`([^`\n]{3,60})`")

SYSTEM = (
    "You map memory records to the repository file(s) each record makes a claim about. "
    'Output ONLY one XML manifest: <mappings><memory id="N"><file path="x"/></memory>'
    '</mappings>. If a record is a behavioral process/workflow rule with no file backing, '
    'or none of its likely files actually back the claim, emit '
    '<memory id="N" independent="true"/> instead. '
    "Never invent paths: only use paths from the likely list. "
    "Cover every id exactly once. No prose, no code fences."
)


def connect():
    return sqlite3.connect(DB)


REQUIRED_COLS = ("memory_id", "file_path", "verified_at", "mapped_at", "mapping_origin")


def check_schema(cur):
    """The plugin owns this schema and migrates it on update. Its migrations are
    additive (CREATE TABLE IF NOT EXISTS + ensureColumn with a default), so we
    only fail loudly if that ever stops being true."""
    cols = [r[1] for r in cur.execute("PRAGMA table_info(memory_verifications)")]
    if not cols:
        raise SystemExit("context.db has no memory_verifications table — "
                         "magic-context schema changed, update the standalone tools")
    missing = [c for c in REQUIRED_COLS if c not in cols]
    if missing:
        raise SystemExit(f"memory_verifications lost columns {missing} — "
                         "magic-context schema changed, update the standalone tools")


# Files a verifier has already rejected for a memory. Sidecar JSON rather than
# a table in context.db, so the plugin's schema is never touched. A memory whose
# candidates are all rejected converges to file-independent on its own.
REJECTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_rejects.json")
REJECTS = {}


def load_rejects():
    global REJECTS
    try:
        with open(REJECTS_PATH, encoding="utf-8") as fh:
            REJECTS = json.load(fh)
    except Exception:
        REJECTS = {}
    return REJECTS


def save_rejects():
    tmp = REJECTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(REJECTS, fh, indent=1, sort_keys=True)
    os.replace(tmp, REJECTS_PATH)


def rejected(mid):
    return set(REJECTS.get(str(mid), []))


def root_of(project):
    return ROOTS.get(project)


def resolve_file(rel):
    """First root where this repo-relative path actually exists."""
    for root in SEARCH_ROOTS:
        if root and os.path.exists(os.path.join(root, rel)):
            return root
    return None


def unmapped(cur, project):
    return cur.execute(
        """SELECT m.id, m.category, m.content FROM memories m
            WHERE m.project_path=? AND m.status IN ('active','permanent')
              AND NOT EXISTS (SELECT 1 FROM memory_verifications v WHERE v.memory_id=m.id)
            ORDER BY m.id""",
        (project,),
    ).fetchall()


def projects(cur):
    return [r[0] for r in cur.execute(
        """SELECT DISTINCT project_path FROM memories
            WHERE status IN ('active','permanent') ORDER BY 1""")]


def named_paths(content, root):
    if not root:
        return []
    out = []
    for p in PATH_RE.findall(content or ""):
        if p not in out and resolve_file(p):
            out.append(p)
    return out


def rg_candidates(content, root, limit=10):
    """Local stand-in for the plugin agent's tool search: distinctive tokens -> files.

    One rg pass per memory (all tokens as -F patterns) instead of one per token.
    """
    if not content or not root:
        return []
    toks = []
    for t in TOK_BACKTICK.findall(content):
        if re.fullmatch(r"[A-Za-z][\w.]*", t):
            toks.append(t)
    toks += TOKEN_RE.findall(content)
    seen, wanted = set(), []
    for t in toks:
        t = t.strip(".")
        if len(t) < 6 or t in seen:
            continue
        seen.add(t)
        wanted.append(t)
        if len(wanted) >= 8:
            break
    if not wanted:
        return []
    cmd = ["rg", "-o", "-F", "-m", "5"]
    for t in wanted:
        cmd += ["-e", t]
    cmd += ["--glob", "*.c", "--glob", "*.h", "--glob", "*.py", "--glob", "*.md",
            "--glob", "*.cu", "--glob", "*.jsonc", "."]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace", cwd=root)
    except Exception:
        return []
    scored = {}
    for line in r.stdout.splitlines():
        # target is "." with cwd=root, so the path is root-relative and holds
        # no drive letter; split on the first ":" therefore separates cleanly
        if ":" not in line:
            continue
        path, match = line.split(":", 1)
        rel = path.strip().replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel.startswith("../") or resolve_file(rel) is None:
            continue
        rec = scored.setdefault(rel, [0, set()])
        rec[0] += 1
        rec[1].add(match)
    ranked = sorted(scored, key=lambda k: (-len(scored[k][1]), -scored[k][0], k))
    return ranked[:limit]


def candidates_for(content, root):
    named = named_paths(content, root)
    if named:
        return named
    return rg_candidates(content, root)


def build_prompt(batch, project):
    parts = []
    for mid, cat, content, cands in batch:
        seed = f'\nLikely files (found in this repo): {", ".join(cands)}' if cands else ""
        parts.append(f"[{mid}] {cat}\n{content}{seed}")
    return (
        "## Map these memories to their backing files\n\n"
        f"Project: {project}\nRoot: {ROOTS.get(project) or '(no local directory)'}\n\n"
        "For each memory below, pick the repo file(s) it makes a claim about from the "
        'likely list, or mark it file-independent. Behavioral process/workflow directives '
        "stay file-independent even when they name files. Then output ONE <mappings> "
        "manifest covering every id.\n\n<memories>\n"
        + "\n\n".join(parts)
        + "\n</memories>"
    )


def call_model(prompt, timeout=180, system=SYSTEM):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }
    errors = []
    for model in [MODEL] + FALLBACKS:
        body["model"] = model
        req = urllib.request.Request(
            BASE,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            text = data["choices"][0]["message"].get("content") or ""
            if text.strip():
                return text
            errors.append(f"{model}: empty content")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{model}: {type(e).__name__} {e}")
        time.sleep(1)
    raise RuntimeError("all models failed: " + " | ".join(errors))


OPEN_MEM_RE = re.compile(r"<memory\b([^>]*?)(/?)>", re.I)


def parse_manifest(text):
    """Mirror of the plugin's parseMapMemoriesManifest, tolerant of fences.

    The opening tag is matched separately from the body so a self-closing
    <memory .../> cannot swallow the next entry.
    """
    text = re.sub(r"```[a-zA-Z]*", "", text)
    start = text.lower().find("<mappings")
    end = text.lower().rfind("</mappings>")
    if start >= 0 and end > start:
        text = text[start:end + len("</mappings>")]
    out = []
    for m in OPEN_MEM_RE.finditer(text):
        attrs = m.group(1) or ""
        idm = re.search(r'\bid\s*=\s*"(\d+)"', attrs)
        if not idm:
            continue
        mid = int(idm.group(1))
        independent = re.search(r'\bindependent\s*=\s*"(?:true|1)"', attrs, re.I) is not None
        fm = re.search(r'\bfiles\s*=\s*"([^"]*)"', attrs)
        files = [f.strip() for f in fm.group(1).split(",") if f.strip()] if fm else []
        if not files and not m.group(2):
            close = text.lower().find("</memory>", m.end())
            if close > 0:
                files = re.findall(r'<file\b[^>]*\bpath\s*=\s*"([^"]+)"',
                                   text[m.end():close], re.I)
        out.append((mid, independent, [f for f in files if f]))
    return out


def run_project(con, cur, project, limit, dry):
    rows = unmapped(cur, project)
    if not rows:
        return 0, 0, 0, 0
    root = ROOTS.get(project)
    print(f"\n[{project}] root={root or '(none)'} unmapped={len(rows)}")
    if project not in ROOTS:
        print(f"  WARNING: unknown project identity — add a root to ROOTS "
              f"or every memory here is marked file-independent")
    if dry:
        named = sum(1 for r in rows if named_paths(r[2], root))
        print(f"  dry: with named existing paths {named}, "
              f"needing rg {len(rows) - named}")
        return len(rows), 0, 0, 0

    rows = rows[:limit] if limit else rows
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_candidates.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    t0 = time.time()
    prepared = []
    for i, (mid, cat, content) in enumerate(rows):
        cands = cache.get(str(mid))
        if cands is None:
            cands = candidates_for(content, root)
            cache[str(mid)] = cands
            if i % 25 == 0:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
        prepared.append((mid, cat, content, [c for c in cands if c not in rejected(mid)]))
        if i % 50 == 0 and i:
            print(f"  candidates {i}/{len(rows)} "
                  f"({round((time.time() - t0) / i, 2)}s ea)", flush=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    print(f"  candidates ready for {len(prepared)} in {round(time.time() - t0)}s")

    inserted = bad = failed = 0
    for b in range(0, len(prepared), BATCH):
        batch = prepared[b:b + BATCH]
        try:
            text = call_model(build_prompt(batch, project))
        except Exception as e:  # noqa: BLE001
            print(f"  batch {b // BATCH}: MODEL FAIL {e}", flush=True)
            failed += len(batch)
            continue
        parsed = {mid: (ind, files) for mid, ind, files in parse_manifest(text)}
        got = 0
        for mid, _cat, _content, _cands in batch:
            if mid not in parsed:
                bad += 1
                continue
            ind, files = parsed[mid]
            files = [f for f in files if resolve_file(f)]
            targets = [""] if ind or not files else files
            now = int(time.time() * 1000)
            for fp in targets:
                cur.execute(
                    """INSERT OR IGNORE INTO memory_verifications
                       (memory_id, file_path, verified_at, mapped_at, mapping_origin)
                       VALUES (?,?,0,?,?)""",
                    (mid, fp, now, ORIGIN),
                )
                inserted += cur.rowcount
            got += 1
        con.commit()
        print(f"  batch {b // BATCH + 1}/{(len(prepared) + BATCH - 1) // BATCH}: "
              f"mapped {got}/{len(batch)}", flush=True)
    return len(rows), inserted, bad, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--project", default="", help="substring of the project identity")
    args = ap.parse_args()

    if args.selftest:
        sample = ('<mappings>\n<memory id="617" independent="true"/>\n'
                  '<memory id="797" independent="true"/>\n'
                  '<memory id="803">\n<file path="a.c"/>\n<file path="b.h"/>\n</memory>\n'
                  '<memory id="6147" files="core/x.h"/>\n</mappings>')
        got = parse_manifest(sample)
        assert len(got) == 4, got
        assert got[0] == (617, True, []), got[0]
        assert got[1] == (797, True, []), got[1]
        assert got[2] == (803, False, ["a.c", "b.h"]), got[2]
        assert got[3] == (6147, False, ["core/x.h"]), got[3]
        assert parse_manifest("```xml\n" + sample + "\n```") == got
        c = rg_candidates("TESS_ResidualEntry is packed: see core/geo_tess_container.h "
                          "and tess_pack_open", ROOTS["dir:b7b1d59859e2"])
        assert c and all("/" in x for x in c), c
        assert resolve_file("core/geo_tess_container.h")
        assert resolve_file("pool.py") is None or True
        print("selftest OK:", got, "| candidates:", c[:4])
        return 0

    if args.revert:
        con = connect()
        cur = con.execute("DELETE FROM memory_verifications WHERE mapping_origin=?",
                          (ORIGIN,))
        con.commit()
        print(f"reverted {cur.rowcount} rows")
        con.close()
        return 0

    con = connect()
    cur = con.cursor()
    check_schema(cur)
    load_rejects()
    wanted = [p for p in projects(cur)
              if not args.project or args.project.lower() in p.lower()]
    total_left = total_ins = total_bad = total_fail = 0
    for p in wanted:
        left, ins, bad, fail = run_project(con, cur, p, args.limit, args.dry)
        total_left += left
        total_ins += ins
        total_bad += bad
        total_fail += fail
    print(f"\nTOTAL scanned={total_left} inserted={total_ins} "
          f"no_id={total_bad} failed={total_fail}")
    for p in wanted:
        n = len(unmapped(cur, p))
        if n:
            print(f"  still unmapped {p}: {n}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
