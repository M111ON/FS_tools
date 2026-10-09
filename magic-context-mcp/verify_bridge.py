#!/usr/bin/env python3
"""verify_bridge.py — ad-hoc verification for the Hermes<->magic-context bridge.

There is no canonical test/lint/build command for this tool, so this is a
focused probe of the behaviour that matters. It is versioned next to the code it
checks so the evidence can be reproduced at any revision.

All writes go to a throwaway copy of context.db; the live DB is opened read-only
and never mutated. Exit code 0 = all checks passed.

    python verify_bridge.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE = Path(os.environ.get("MAGIC_CONTEXT_DB", r"I:\cortexkit\magic-context\context.db"))
HERMES = Path(os.environ.get("HERMES_STATE_DB", r"I:\Hermes\state.db"))
PYEXE = sys.executable

# Transcribed from @cortexkit/opencode-magic-context v0.43.2 dist/v2/server.js
CATEGORIES = {"PROJECT_RULES", "ARCHITECTURE", "CONSTRAINTS", "CONFIG_VALUES", "NAMING",
              "USER_DIRECTIVES", "USER_PREFERENCES", "CONFIG_DEFAULTS",
              "ARCHITECTURE_DECISIONS", "ENVIRONMENT", "WORKFLOW_RULES", "KNOWN_ISSUES"}
STATUSES = {"active", "permanent", "archived"}
SCOPES = {"project", "ecosystem", "universe"}
SOURCES = {"historian", "agent", "dreamer", "user"}
GESTURE = re.compile(r"^\s*(-\s*)?(\.+|…+|newsession|new session|ok|done|continue|go|next|\d+\.[a-z_]+)\s*$", re.I)
EMPTY_OUT = re.compile(r"^Outcome:\s*(do|done|ok|operation interrupted|interrupted|n\.?a|\.+)\s*$", re.I)

FAIL: list[str] = []
OK: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    (OK if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_memory_row(r) -> bool:
    """Mirror of the plugin's isMemoryRow() guard — a row failing this is dropped."""
    return (isinstance(r["id"], int)
            and isinstance(r["project_path"], str) and bool(r["project_path"])
            and r["category"] in CATEGORIES
            and isinstance(r["content"], str)
            and isinstance(r["normalized_hash"], str) and bool(r["normalized_hash"])
            and isinstance(r["importance"], int)
            and r["scope"] in SCOPES
            and isinstance(r["shareable"], int)
            and r["source_type"] in SOURCES
            and isinstance(r["seen_count"], int)
            and isinstance(r["retrieval_count"], int)
            and r["status"] in STATUSES)


def check_sync_engine() -> None:
    print("A. hermes_mc_sync.py")
    os.environ["MAGIC_CONTEXT_DB"] = str(LIVE)
    os.environ["HERMES_STATE_DB"] = str(HERMES)
    sync = load_module(HERE / "hermes_mc_sync.py", "hermes_mc_sync")

    # A1 hash contract against real live rows
    ro = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    rows = ro.execute("SELECT content, normalized_hash FROM memories "
                      "WHERE status='active' AND length(content)>30 LIMIT 400").fetchall()
    ro.close()
    match = sum(1 for r in rows if sync.normalized_hash(r["content"]) == r["normalized_hash"])
    check("A1 normalized_hash matches plugin on live rows", match >= int(len(rows) * 0.95),
          f"{match}/{len(rows)} exact")

    # A2 identity: sorted root commit (not HEAD); ancestor walk; dir fallback
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r"I:\tools",
                          capture_output=True, text=True).stdout.strip()
    roots = sorted(l.strip() for l in subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=r"I:\tools",
        capture_output=True, text=True).stdout.splitlines() if l.strip())
    ident = sync.resolve_identity(r"I:\tools")
    check("A2a identity uses sorted root commit, not HEAD",
          ident == f"git:{roots[0]}" and ident != f"git:{head}",
          f"{ident[:24]}... ({len(roots)} roots; HEAD={head[:8]})")
    check("A2a2 sorting is load-bearing (>1 root commit)", len(roots) > 1, f"{len(roots)} roots")
    check("A2b nested dir resolves via ancestor walk", sync.resolve_identity(str(HERE)) == ident)
    norepo = Path(tempfile.mkdtemp(prefix="hermes-verify-norepo-"))
    try:
        expect = "dir:" + hashlib.md5(os.path.realpath(str(norepo)).encode()).hexdigest()[:12]
        check("A2c repo-less dir -> dir:<md5-12>", sync.resolve_identity(str(norepo)) == expect, expect)
    finally:
        shutil.rmtree(norepo, ignore_errors=True)
    check("A2d missing path -> None",
          sync.resolve_identity(None) is None and sync.resolve_identity(r"I:\does\not\exist") is None)

    # A3 digest hygiene + budget + provenance
    hc = sync.hermes_conn()
    sess = hc.execute("SELECT * FROM sessions WHERE message_count>100 "
                      "AND source NOT IN ('cron','subagent') "
                      "ORDER BY started_at DESC LIMIT 1").fetchone()
    digest = sync.build_digest(sess, sync.session_messages(hc, sess["id"]))
    leaked = [m for m in ("OUT-OF-BAND", "<system-reminder", "[IMPORTANT",
                          "Background process", "<available-skills") if m in digest]
    check("A3a digest strips harness plumbing", not leaked, f"leaked={leaked}")
    check("A3b digest within per-session budget", len(digest) // 4 < 500, f"{len(digest)//4} tok")
    check("A3c digest carries provenance", sess["id"] in digest)

    # A6/A7 the MCP entry point and the noise filters
    check("A6 run_sync is a callable function", callable(getattr(sync, "run_sync", None)))
    gestures = [".", "..", "…", "newsession", "ok", "done", "continue", "go", "next",
                "1.microscope", "ต่อ", "ไปต่อ", "โอเค"]
    check("A7a gesture prompts are rejected", all(sync._is_gesture(g) for g in gestures),
          f"leaked={[g for g in gestures if not sync._is_gesture(g)]}")
    check("A7b real prompts are kept", not any(sync._is_gesture(t) for t in
          ["แก้ runtime fail ยังไง", "why did the ANN warm path stop working", "commit แล้ว push เลยครับ"]))
    check("A7c empty outcome patterns rejected",
          all(sync.EMPTY_OUTCOME_RX.match(x) for x in
              ("Do", "done", "ok", "Operation interrupted.", "n/a", "---")))

    # A4/A5/A8/C against a throwaway copy
    tmp = Path(tempfile.mkdtemp(prefix="hermes-verify-"))
    try:
        copy = tmp / "context.db"
        shutil.copyfile(LIVE, copy)
        env = {**os.environ, "MAGIC_CONTEXT_DB": str(copy), "HERMES_STATE_DB": str(HERMES)}

        def run_cli():
            return subprocess.run(
                [PYEXE, str(HERE / "hermes_mc_sync.py"), "--apply", "--days", "45", "--quiet"],
                capture_output=True, text=True, timeout=600, env=env)

        def num(out: str, label: str) -> int:
            m = re.search(rf"{label}\s*:\s*(\d+)", out)
            return int(m.group(1)) if m else -1

        r1, r2 = run_cli(), run_cli()
        check("A4 idempotent on re-run (insert 0, update 0)",
              r1.returncode == 0 and r2.returncode == 0
              and num(r2.stdout, "memories inserted") == 0
              and num(r2.stdout, "memories updated") == 0,
              f"run1 insert={num(r1.stdout, 'memories inserted')}, "
              f"run2 insert={num(r2.stdout, 'memories inserted')} "
              f"update={num(r2.stdout, 'memories updated')}")

        cc = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        cc.row_factory = sqlite3.Row
        synced = cc.execute("SELECT * FROM memories "
                            "WHERE metadata_json LIKE '%hermes_session_id%'").fetchall()
        bad = [r["id"] for r in synced if not is_memory_row(r)]
        check("A5 all synced rows pass isMemoryRow()", bool(synced) and not bad,
              f"{len(synced)} rows, dropped={bad}")
        check("A5b all synced rows use source_type=agent",
              all(r["source_type"] == "agent" for r in synced))

        junky = [r["id"] for r in synced
                 if any(GESTURE.match(l) or EMPTY_OUT.match(l) for l in r["content"].splitlines())]
        check("A8a no synced digest contains gesture/empty-outcome junk", not junky, f"junky={junky}")
        check("A8b all synced digests have a real ask or outcome",
              all(("User asked about:" in r["content"]) or ("Outcome:" in r["content"])
                  for r in synced))
        n1 = len(synced)
        r3 = run_cli()
        c2 = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        n2 = c2.execute("SELECT COUNT(*) FROM memories "
                        "WHERE metadata_json LIKE '%hermes_session_id%'").fetchone()[0]
        c2.close()
        check("A8c re-run reaps instead of duplicating", r3.returncode == 0 and n2 == n1,
              f"before={n1} after={n2}")

        proj = synced[0]["project_path"]
        want = len([r for r in synced if r["project_path"] == proj])
        inj = cc.execute("SELECT COUNT(*) FROM memories WHERE project_path=? "
                         "AND status IN ('active','permanent') "
                         "AND (expires_at IS NULL OR expires_at>?)",
                         (proj, int(time.time() * 1000))).fetchone()[0]
        check("C1 injection query returns synced rows", inj >= want, f"{inj} injectable for {proj}")
        fts = cc.execute("SELECT COUNT(*) FROM memories_fts "
                         "WHERE memories_fts MATCH '\"Hermes work log\"'").fetchone()[0]
        mem = cc.execute("SELECT COUNT(*) FROM memories "
                         "WHERE content LIKE 'Hermes work log%'").fetchone()[0]
        check("C2 memories_fts consistent after UPDATE", fts == mem and mem > 0,
              f"fts={fts} memories={mem}")
        check("C2b quick_check ok", cc.execute("PRAGMA quick_check").fetchone()[0] == "ok")
        cc.close()

        check_bridge(copy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_bridge(copy: Path) -> None:
    print("B. magic_context_mcp_server.py")
    server = HERE / "magic_context_mcp_server.py"

    def resolve_with(**envkw) -> str:
        code = ("import importlib.util,os;"
                "os.environ.pop('MAGIC_CONTEXT_DB',None);"
                "os.environ.pop('MAGIC_CONTEXT_STORAGE_DIR',None);"
                "os.environ.pop('XDG_DATA_HOME',None);"
                f"os.environ.update({envkw!r});"
                f"s=importlib.util.spec_from_file_location('m',r'{server}');"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(str(m.DB_PATH))")
        return subprocess.run([PYEXE, "-c", code], capture_output=True, text=True).stdout.strip()

    check("B1a MAGIC_CONTEXT_DB wins",
          resolve_with(MAGIC_CONTEXT_DB=r"X:\explicit\context.db") == r"X:\explicit\context.db")
    check("B1b MAGIC_CONTEXT_STORAGE_DIR honored",
          resolve_with(MAGIC_CONTEXT_STORAGE_DIR=r"X:\storage") == os.path.join(r"X:\storage", "context.db"))
    check("B1c XDG_DATA_HOME honored",
          resolve_with(XDG_DATA_HOME=r"X:\xdg")
          == os.path.join(r"X:\xdg", "cortexkit", "magic-context", "context.db"))
    check("B1d falls back to platform default",
          resolve_with().replace("/", "\\").endswith(r".local\share\cortexkit\magic-context\context.db"))

    os.environ["MAGIC_CONTEXT_DB"] = str(copy)
    mcp = load_module(server, "mc_bridge")
    check("B2 DB_PATH points at temp copy", Path(mcp.DB_PATH) == copy)

    import asyncio
    names = {t.name for t in asyncio.run(mcp.mcp.list_tools())}
    check("B3 all 10 tools register", len(names) == 10, f"{sorted(names)}")
    check("B3b magic_sync_hermes present", "magic_sync_hermes" in names)
    check("B3c magic_remember present", "magic_remember" in names)

    tgt, body = "dir:3e16a219b8e2", "__VERIFY__ bridge contract probe marker"
    r = json.loads(mcp.magic_remember(content=body, category="ENVIRONMENT",
                                      importance=10, project_path=tgt))
    check("B4 valid write accepted", r.get("status") == "saved")
    check("B4b writes source_type=agent (the invisibility bug)",
          r.get("source_type") == "agent", r.get("source_type"))
    check("B4c duplicate detected within scope",
          json.loads(mcp.magic_remember(content=body, category="ENVIRONMENT",
                                        importance=10, project_path=tgt)).get("status") == "duplicate")
    check("B4d same content + different category is a new row",
          json.loads(mcp.magic_remember(content=body, category="CONSTRAINTS",
                                        importance=10, project_path=tgt)).get("status") == "saved")
    check("B5 invalid category rejected",
          json.loads(mcp.magic_remember(content="x" * 60, category="BOGUS",
                                        project_path=tgt)).get("status") == "error")
    check("B5b invalid scope rejected",
          json.loads(mcp.magic_remember(content="y" * 60, category="ENVIRONMENT",
                                        scope="galaxy", project_path=tgt)).get("status") == "error")

    cwd = os.getcwd()
    try:
        os.chdir(r"I:\tools")
        auto = json.loads(mcp.magic_remember(content="__VERIFY__ auto id " + str(time.time()),
                                             category="ENVIRONMENT", importance=5))
        check("B5c project identity auto-resolved when omitted",
              auto.get("status") == "saved" and str(auto.get("project", "")).startswith("git:"),
              str(auto.get("project")))
    finally:
        os.chdir(cwd)

    t0 = time.time()
    try:
        sy = json.loads(mcp.magic_sync_hermes(days=45, mode="digest", apply=False))
        elapsed = time.time() - t0
        check("B6 magic_sync_hermes in-process, no hang",
              sy.get("status") == "ok" and elapsed < 60, f"{elapsed:.2f}s status={sy.get('status')}")
        check("B6b summary has the expected keys",
              all(k in sy for k in ("sessions_processed", "inserted", "updated", "skipped", "reaped")))
    except Exception as e:
        check("B6 magic_sync_hermes in-process, no hang", False, f"{type(e).__name__}: {e}")
    check("B7 magic_sync_hermes rejects bad mode",
          json.loads(mcp.magic_sync_hermes(mode="bogus")).get("status") == "error")
    check("B7b sync engine pointed at this server's DB",
          Path(mcp.DB_PATH) == copy and Path(os.environ["MAGIC_CONTEXT_DB"]) == copy)

    cc = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    cc.row_factory = sqlite3.Row
    wrote = cc.execute("SELECT * FROM memories WHERE content LIKE '__VERIFY__%'").fetchall()
    check("B8 all bridge-written rows pass isMemoryRow()",
          bool(wrote) and all(is_memory_row(r) for r in wrote), f"{len(wrote)} rows")
    cc.close()


def main() -> int:
    check_sync_engine()
    print()
    print(f"AD-HOC VERIFICATION: {len(OK)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
