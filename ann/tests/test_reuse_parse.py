"""test_reuse_parse.py — the regrow must reuse the loader's parsed index
(card 2026-10-22: regrow ไม่ parse jsonl เเอง).

Before this change the incremental/full regrow called lz_index_load() itself,
parsing the whole index jsonl a second time — the background loader had
already parsed the same file into g_idx_cache. On a 258 MB index that is ~50 s
of duplicated work per regrow. There was also a third parse hiding behind it:
regrow_done invalidated the index cache on every success, so the loader
re-parsed the SAME unchanged file (regrow never rewrites the jsonl, only the
stamp sidecar) right after.

Verifies on a scratch server (port 8098, NEVER the live :8096):

  C1  regrow #1 (fresh server) -> mode "full" AND load_src "borrowed": main()
      calls lz_index_cached() during boot, so the loader's buffer is already
      there and the regrow must not parse a second time. load ms is O(1).
  C2  append rows, let the loader re-swap (the file's mtime/size moved, so
      ONE loader parse is legitimate), regrow -> mode "incremental",
      load_src "borrowed", load ms O(1)
  C3  the unchanged fast path loads nothing at all ("load_src":"none")
  C4  "regrow_load src=parse" never appears in the diag log — for the whole
      session the regrow parsed the jsonl zero times. Before this change it
      parsed it on every non-unchanged regrow (~50 s on a 258 MB file).
  C5  after a successful regrow the loader does NOT parse again — its cache
      survives because the regrow never rewrites the jsonl and regrow_done
      no longer invalidates the index cache
  C6  the service is still correct: routed=1, search stays on the anchor
      route, /health keeps answering (the borrowed buffer was never freed)
  C7  the collision case: when the regrow's borrow fires WHILE the loader
      is mid-parse of the same version, the regrow now WAITS for the swap
      and borrows it instead of parsing the same 62k rows a second time
      (measured live: 52 s + 52 s back to back before this fix)

Scratch lives under %TEMP% and is rebuilt per run — nothing inside the repo's
data/ dir is written. Run: python test_reuse_parse.py  (exit 0 = PASS).
"""
import os
import re
import json
import time
import shutil
import subprocess
import sys
import urllib.request
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                      # I:/tools/ann
EXE  = os.path.join(REPO, "build", "ann_search.exe")
SRC  = os.path.join(REPO, "src", "ann_search.c")
PORT = 8098
BASE = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-reuse-scratch")
D    = 384
N_ROWS = 400

IDX  = os.path.join(BASE, "data", "memcore", "index.jsonl")
ANCH = os.path.join(BASE, "ann.bin")
ORDP = os.path.join(BASE, "order.bin")
LOGP = os.path.join(BASE, "route.log")
DIAG = IDX + ".loader.log"

env = {**os.environ,
       "ANN_INDEX_PATH": IDX,
       "ANN_ANCH_PATH":  ANCH,
       "ANN_ORDER_PATH": ORDP,
       "ANN_ROUTE_LOG":  LOGP,
       "ANN_TOTEM_WARM": "1",
       "ANN_FLOAT_KERNEL": "1"}


def ensure_exe():
    """The suite must exercise THIS source, not a stale binary."""
    if (not os.path.exists(EXE)
            or os.path.getmtime(EXE) < os.path.getmtime(SRC)):
        subprocess.run(["gcc", "-O2", "-std=c11", "-Wall",
                        "-Wno-unused-parameter", "-Wno-sign-compare",
                        "-Wno-format", "-Wno-unknown-pragmas", "-Iinclude",
                        "-o", EXE, "src/ann_search.c",
                        "-lws2_32", "-lpsapi", "-lm"],
                       cwd=REPO, check=True)


def build_index():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(os.path.dirname(IDX), exist_ok=True)
    step = 0.4 / N_ROWS
    with open(IDX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            x = step * i
            v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
            assert len(v) == D, f"dim mismatch {len(v)}"
            f.write('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, ",".join("%.6f" % x for x in v)))


def srv_start():
    """Wait for the port WITHOUT calling /health.

    /health runs lz_index_cached(), which parses the index into the loader's
    cache on its first cold call — that would turn C1's cold-regrow into a
    borrow and hide the very case we want to measure.
    """
    srv = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket
    for _ in range(80):
        time.sleep(0.25)
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            s.close()
            return srv
        except OSError:
            pass
    srv.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)


def wait_loader_settled(prev_text):
    """Wait until the loader has parsed AND swapped in whatever the file now is.

    The three loader markers fire in order: loader_parse_start (parse began,
    ~0.8 s on 400 rows), loader_swap_lock (about to take the lock), reload
    (swap done, stamps re-applied). Only "reload" proves the new buffer is
    live — returning on the earlier markers races the swap and the borrow
    would (by design) miss.
    """
    for _ in range(100):
        time.sleep(0.25)
        t = diag()
        if t.count("reload ") > prev_text.count("reload "):
            time.sleep(0.5)
            return diag()
    return diag()


def loader_parses():
    return diag().count("loader_parse_start")


def regrow(body=""):
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(Request(
        f"http://127.0.0.1:{PORT}/v1/state/regrow", body.encode(),
        {"Content-Type": "application/json"}), timeout=300).read())
    return (time.perf_counter() - t0) * 1000, d


def search(q="geometric", k=2):
    return json.loads(urllib.request.urlopen(Request(
        f"http://127.0.0.1:{PORT}/v1/state/search",
        json.dumps({"query": q, "k": k}).encode(),
        {"Content-Type": "application/json"}), timeout=60).read())


def health():
    return json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/health", timeout=10).read())


def diag():
    try:
        return open(DIAG, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


fails, checks = [], []


def check(name, cond, detail=""):
    (checks if cond else fails).append((name, detail))
    print(("  ok  " if cond else "  FAIL ") + name +
          (f" | {detail}" if detail else ""))


ensure_exe()
build_index()
srv = srv_start()
try:
    # C1: main() already parsed the index into the loader's cache during boot
    # (lz_index_cached), so the very first regrow borrows it.
    w1, d1 = regrow()
    rg1 = d1.get("regrow", {})
    check("C1 full mode", rg1.get("mode") == "full",
          f"{w1:.0f}ms {rg1.get('mode')}")
    check("C1-b first regrow borrows", rg1.get("load_src") == "borrowed",
          f"load_src={rg1.get('load_src')}")
    check("C1-c load is O(1) not a parse",
          (rg1.get("ms_per_phase", {}).get("load") or 9e9) < 100,
          f"{rg1.get('ms_per_phase', {}).get('load'):.3f}ms")

    # C2: append rows. The file's mtime/size moved, so the loader must
    # re-parse ONCE; the regrow that follows must reuse that result.
    before = diag()
    VA = ",".join("0.01" for _ in range(D))
    with open(IDX, "a", encoding="utf-8") as f:
        for sid in ("probe-reuse-a", "probe-reuse-b"):
            f.write('{"sid":"%s","file":"_probe","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (sid, VA))
    wait_loader_settled(before)

    w2, d2 = regrow()
    rg2 = d2.get("regrow", {})
    check("C2 incremental mode", rg2.get("mode") == "incremental",
          rg2.get("mode"))
    check("C2-b reused the loader buffer", rg2.get("load_src") == "borrowed",
          f"load_src={rg2.get('load_src')} "
          f"ms_load={rg2.get('ms_per_phase', {}).get('load')}")
    check("C2-c load ms collapses",
          (rg2.get("ms_per_phase", {}).get("load") or 9e9) < 100,
          f"{rg2.get('ms_per_phase', {}).get('load'):.3f}ms")
    check("C2-d n_prev/n_new",
          rg2.get("n_prev") == N_ROWS and rg2.get("n_new") == 2,
          f"n_prev={rg2.get('n_prev')} n_new={rg2.get('n_new')}")

    # C3: the unchanged fast path never loads at all
    w3, d3 = regrow()
    check("C3 unchanged + no load",
          d3.get("regrow", {}).get("mode") == "unchanged"
          and d3.get("regrow", {}).get("load_src") == "none",
          f"{w3:.0f}ms {d3['regrow'].get('mode')} "
          f"src={d3['regrow'].get('load_src')}")

    # C4: the regrow never parsed the jsonl itself. Before this change it
    # did — once per non-unchanged regrow, ~50 s on a 258 MB index.
    regrow_parses = len(re.findall(r"regrow_load src=parse", diag()))
    check("C4 regrow self-parse count is zero", regrow_parses == 0,
          f"regrow_load src=parse x{regrow_parses}")

    # C5: a successful regrow must not force the loader to parse again.
    # The loader ticks every 1.5 s; an invalidated cache would re-read the
    # unchanged file within a few ticks.
    lp_before = loader_parses()
    w5, d5 = regrow()
    time.sleep(6)
    check("C5-d no forced loader re-parse", loader_parses() == lp_before,
          f"loader parses {lp_before} -> {loader_parses()} "
          f"(regrow={d5['regrow'].get('mode')})")

    # C6: the service is still correct after borrowing
    h = health()
    check("C6 routed stays 1", h.get("routed") == 1, f"routed={h.get('routed')}")
    check("C6-b entries = rows", h.get("entries") == N_ROWS + 2,
          f"entries={h.get('entries')}")
    check("C6-c search still anchor",
          search()["route"]["mode"].startswith("anchor"))
    check("C6-d /health still answers (loader buffer survived)",
          health().get("status") == "ok")

    # C7: the regrow must WAIT for the loader's in-flight parse instead of
    # parsing the same version itself (the collision case). On the live
    # 258 MB index this was 52 s + 52 s back to back. Recipe: append a big
    # batch (a few seconds of parse — a wide window), fire the regrow the
    # moment the loader's parse starts, and check the regrow came back
    # WITHOUT a self-parse and WITHOUT a second loader parse.
    before7 = diag()
    VA7 = ",".join("0.013333" for _ in range(D))
    n_batch = 3000
    with open(IDX, "a", encoding="utf-8") as f:
        for i in range(n_batch):
            f.write('{"sid":"batch-%04d","file":"_batch","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, VA7))
    parsed0 = before7.count("loader_parse_start")
    fired = False
    for _ in range(120):
        time.sleep(0.25)
        if diag().count("loader_parse_start") > parsed0:
            fired = True
            break
    check("C7-a loader parse started (window opened)", fired,
          f"loader_parse_start {parsed0} -> {diag().count('loader_parse_start')}")
    w7, d7 = regrow()
    rg7 = d7.get("regrow", {})
    check("C7-b regrow waited for the loader (no self-parse)",
          rg7.get("load_src") == "borrowed",
          f"load_src={rg7.get('load_src')} load="
          f"{rg7.get('ms_per_phase', {}).get('load')}ms")
    check("C7-c borrow hit after the wait",
          "idx_borrow hit" in diag() or rg7.get("load_src") == "borrowed",
          f"entries={d7.get('nent')}")
    time.sleep(6)
    lp7 = diag().count("loader_parse_start")
    check("C7-d loader parsed the new version exactly once", lp7 == parsed0 + 1,
          f"loader parses {parsed0} -> {lp7} (regrow={rg7.get('mode')})")
    rp7 = len(re.findall(r"regrow_load src=parse", diag()))
    check("C7-e self-parse count still zero", rp7 == 0,
          f"regrow_load src=parse x{rp7}")
finally:
    srv.kill()
    time.sleep(1)
    try:
        shutil.rmtree(BASE)
    except OSError:
        pass

print("RESULT:", "ALL BEHAVIED (ad-hoc verification, not a project suite)"
      if not fails else f"{len(fails)} FAILURES")
sys.exit(0 if not fails else 1)
