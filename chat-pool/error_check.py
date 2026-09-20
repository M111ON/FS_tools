"""error_check.py — every error the server can answer has a sentence of its own.

The dashboard translates the server's English `error` label into Thai at the
point of display (SPEC §17). That translation is a LIST, and a list drifts:
add a validation branch to pool_server.py and the browser starts showing a
fallback sentence that no longer says what went wrong — or, worse, starts
showing the English label again. Neither is visible from the page, from
`pool.py demo` (306/306 through every one of them) or from `ui_check`, which
only reads the dashboard and never asks the server anything.

So this drives a real server over a real socket, because the thing being
checked is the JOIN between two files:

  * LIVE — every endpoint, probed with malformed input, keeping the (status,
    error) pairs the server really answers. This is the truth about the wire.
  * SOURCE — every `{"error": …}` literal in pool_server.py. Some cannot be
    reached from a loopback test at all (the two 401s: `_authorized` trusts
    127.0.0.1), and "unreachable by this test" must not mean "unchecked".
  * DASHBOARD — the translator itself, sliced out of the page and executed in
    node rather than re-implemented here, so a pattern edit cannot make the
    test and the app disagree about what matches.

Each error is then classified by the app's OWN pattern list:

  NAMED   a pattern matched — the sentence says what actually went wrong
  STATUS  no pattern, but the HTTP status is in the table: true, but generic
  GENERIC nothing matched — the reader gets no cause at all

PASS requires: every probe answered (a request that returns nothing is
indistinguishable from a dead server — the failure §12 fixed on the write
path), every error a person can be shown is NAMED, and nothing anywhere is
GENERIC.

    python error_check.py            # start a server on a temp log, check, clean up
    python error_check.py --gaps     # also print the STATUS-only ones
    python error_check.py --keep     # leave the server up and print its port

The live log is never touched: the child gets its own log.jsonl + apps.json.
Requires node (same as ui_check) and refuses to pretend without it.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

import stream_check                       # noqa: E402  (its server + seed helpers)
import ui_check                           # noqa: E402  (its script extractor)

GENERIC = "เซิร์ฟเวอร์ปฏิเสธคำขอนี้"
NO_SERVER = "ต่อเซิร์ฟเวอร์ไม่ติด"


def _get_status(port, path, timeout=5):
    """GET and report (status, json-or-None) *including* a 4xx/5xx body.

    stream_check._get raises on an error status, which is exactly what these
    probes ask for; this reads it instead. Status 0 means no response at all.
    """
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, None
    except (urllib.error.URLError, OSError):
        return 0, None


# ── the probes ───────────────────────────────────────────────────────────────
# One row per (endpoint, way to be wrong). Written from the server's own
# validation branches, not from a list of the strings it happens to return:
# the point is to reach the branches, and whatever they say is the finding.
GET_PROBES = [
    ("tail-n-text",             "/tail?n=abc"),
    ("view-n-text",             "/view?n=abc"),
    ("stream-n-text",           "/stream?n=abc"),
    ("curator-n-text",          "/curator?n=abc"),
    ("curator-hide-text",       "/curator?hide=abc"),
    ("curator-at-text",         "/curator?prune=1&at=abc"),
    ("inbox-no-app",            "/inbox"),
    ("inbox-unregistered",      "/inbox?app=ghost&mark=0"),
    ("neighbors-no-rkey",       "/neighbors"),
    ("neighbors-bad-rkey",      "/neighbors?rkey=abc"),
    ("neighbors-bad-level",     "/neighbors?rkey=0&level=9"),
    ("neighbors-bad-path",      "/neighbors?rkey=0&level=0&path=xx"),
    ("pointers-bad-rkey",       "/pointers?rkey=abc"),
    ("explore-no-target",       "/explore"),
    ("explore-bad-hops",        "/explore?hash=deadbeef&hops=abc"),
    ("blocks-no-such-slug",     "/blocks?slug=nope-not-here"),
    ("ctx-bad-level",           "/ctx?level=9"),
    ("ctx-at-no-rkey",          "/ctx/at"),
    ("ctx-stats-no-index",      "/ctx/stats"),
    ("unknown-get",             "/nope"),
]

POST_PROBES = [
    ("register-bad-body",       "/register",    b"{broken json"),
    ("register-bad-name",       "/register",    {"app": "BAD NAME!"}),
    ("append-bad-body",         "/append",      b"{broken json"),
    ("append-empty-body",       "/append",      b""),
    ("append-unregistered",     "/append",      {"app": "ghost", "kind": "chat", "text": "x"}),
    ("append-no-text",          "/append",      {"app": "alice", "kind": "chat"}),
    ("append-bond-bad-ends",    "/append",      {"app": "alice", "kind": "bond", "text": "nope uses nope"}),
    ("append-bond-bad-rel",     "/append",      {"app": "alice", "kind": "bond", "text": "a" * 64 + " sideways " + "b" * 64}),
    ("append-unbind-bad-hash",  "/append",      {"app": "alice", "kind": "unbind", "text": "nope"}),
    ("block-bad-body",          "/block",       b"{broken json"),
    ("block-unregistered",      "/block",       {"app": "ghost", "slug": "s", "title": "t"}),
    ("block-bad-slug",          "/block",       {"app": "alice", "slug": "BAD SLUG", "title": "t"}),
    ("block-no-title",          "/block",       {"app": "alice", "slug": "fresh-one"}),
    ("block-bad-status",        "/block",       {"app": "alice", "slug": "fresh-two", "title": "t", "status": "weird"}),
    ("block-bad-ref",           "/block",       {"app": "alice", "slug": "fresh-three", "title": "t", "refs": "zz"}),
    ("session-end-bad-body",    "/session-end", b"{broken json"),
    ("session-end-no-app",      "/session-end", {}),
    ("session-end-unregistered", "/session-end", {"app": "ghost"}),
    ("unknown-post",            "/nope",        b"{}"),
]

# f-string placeholders in pool_server.py, filled with a value that looks like
# the real one so the pattern the dashboard uses sees the same shape.
SAMPLES = {"app": "ghost", "slug": "nope-not-here", "row['slug']": "x", "b['slug']": "x"}


# ── the translator, run for real ─────────────────────────────────────────────
DRIVER = """
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const hit = c => {
  const raw = c.error || '';
  for (let i = 0; i < SAY_ERRORS.length; i++) if (raw.match(SAY_ERRORS[i][0])) return i;
  return -1;
};
console.log(JSON.stringify(cases.map(c => {
  const r = {error: c.error};
  return {say: sayError(r, c.status), raw: sayRaw(r, c.status),
          hit: hit(c), byStatus: Object.prototype.hasOwnProperty.call(SAY_STATUS, c.status)};
})));
"""


def translator_source(html):
    """The slice of the dashboard that turns an error into a sentence."""
    js = ui_check.extract_script(html)
    i = js.find("const SAY_STATUS=")
    j = js.find("// a memory's status is a wire value")
    if i < 0 or j <= i:
        return None, ("the dashboard no longer has a translator between "
                      "`const SAY_STATUS=` and the BLK_STATUS comment")
    return js[i:j], None


def run_translator(src, cases):
    """(sentence, tooltip, pattern_hit, status_known) per case, in order."""
    node = shutil.which("node")
    if not node:
        return None, "node is not installed"
    tmp = tempfile.mkdtemp(prefix="error-check-")
    path = os.path.join(tmp, "say.js")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src + DRIVER)
        r = subprocess.run([node, path], input=json.dumps(cases),
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, (r.stdout + r.stderr).strip()[-800:]
        return json.loads(r.stdout), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def classify(out):
    if out["say"] in (GENERIC, NO_SERVER):
        return "GENERIC"
    if out["hit"] >= 0:
        return "NAMED"
    return "STATUS" if out["byStatus"] else "GENERIC"


# ── the static inventory ─────────────────────────────────────────────────────
def literal_errors():
    """Every `{"error": <literal|f-string>}` in pool_server.py → a sample string.

    Parsed with `ast` rather than a regex over the text so an f-string is
    expanded the way Python would expand it, and so a nested brace in a
    computed message cannot be mistaken for a literal.
    """
    with open(os.path.join(DIR, "pool_server.py"), encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "error" not in keys:
            continue
        value = node.values[keys.index("error")]
        if isinstance(value, ast.Constant):
            found.setdefault(value.value, []).append(node.lineno)
        elif isinstance(value, ast.JoinedStr):        # f-string
            parts = []
            for piece in value.values:
                if isinstance(piece, ast.Constant):
                    parts.append(str(piece.value))
                else:
                    name = ast.unparse(piece.value)
                    parts.append(SAMPLES.get(name, "x"))
            found.setdefault("".join(parts), []).append(node.lineno)
    return found


# ── driver ───────────────────────────────────────────────────────────────────
def run(keep=False, gaps=False):
    html = ui_check.page_html()
    src, err = translator_source(html)
    if src is None:
        print("error_check: FAILED — %s" % err)
        return 1
    probe = run_translator(src, [{"error": "", "status": 200}])
    if probe[0] is None:
        print("error_check: SKIPPED — the translator was NOT executed: %s" % probe[1])
        return 2

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-30s %s" % ("PASS" if ok else "FAIL", name, detail))

    tmp = tempfile.mkdtemp(prefix="error-check-")
    log = os.path.join(tmp, "log.jsonl")
    errfile = os.path.join(tmp, "stderr.txt")
    stream_check._seed(log, 10)
    errf = open(errfile, "w+", encoding="utf-8", errors="replace")
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve", "--log", log, "--port", "0"],
        cwd=DIR, stdout=subprocess.PIPE, stderr=errf, text=True)
    try:
        ready = child.stdout.readline().strip()
        if not ready.startswith("READY "):
            print("error_check: server did not start — %r" % ready)
            return 1
        port = int(ready.split()[1])
        print("error_check: server on 127.0.0.1:%d  (temp log, never the live one)" % port)

        # ── harvest ──────────────────────────────────────────────────────────
        harvest = {}            # error string -> [statuses, probe names]
        silent = []
        for name, path in GET_PROBES:
            st, body = _get_status(port, path)
            if st == 0 or not isinstance(body, dict) or "error" not in body:
                if st != 200:                       # 200 = this probe is simply legal
                    silent.append("%s -> status=%s" % (name, st))
                continue
            e = harvest.setdefault(body["error"], {"statuses": set(), "probes": []})
            e["statuses"].add(st)
            e["probes"].append(name)
        for name, path, body in POST_PROBES:
            st, b = stream_check._post_status(
                port, path, body if isinstance(body, bytes) else json.dumps(body).encode())
            if st == 0 or not isinstance(b, dict) or "error" not in b:
                if st != 200:
                    silent.append("%s -> status=%s" % (name, st))
                continue
            e = harvest.setdefault(b["error"], {"statuses": set(), "probes": []})
            e["statuses"].add(st)
            e["probes"].append(name)

        # 1. a probe that answers NOTHING is indistinguishable from a dead
        #    server. This is §12's finding, which was fixed for POST bodies and
        #    left standing for every GET query parameter.
        check("every-probe-answers", not silent,
              "%d of %d probes answered" % (len(GET_PROBES) + len(POST_PROBES) - len(silent),
                                            len(GET_PROBES) + len(POST_PROBES))
              if not silent else "no response at all: " + "; ".join(silent[:6]))

        # 2. the harvest has to be big enough to be worth trusting
        check("harvest-covers-endpoints", len(harvest) >= 12,
              "%d distinct errors from %d probes" % (len(harvest), len(GET_PROBES) + len(POST_PROBES)))

        # ── classify everything ──────────────────────────────────────────────
        cases = [{"error": e, "status": sorted(v["statuses"])[0]} for e, v in harvest.items()]
        out = run_translator(src, cases)
        if out[0] is None:
            print("error_check: FAILED — translator run 2: %s" % out[1])
            return 1
        live = list(zip(cases, out[0]))
        tiers = [(c["error"], classify(o), o, harvest[c["error"]]) for c, o in live]

        invented = {e: lines for e, lines in literal_errors().items()}
        for e, lines in invented.items():
            if e in harvest:
                continue
            r = run_translator(src, [{"error": e, "status": 404}])
            if r[0] is None:
                print("error_check: FAILED — translator run 3: %s" % r[1])
                return 1
            o = r[0][0]
            tiers.append((e, classify(o), o, {"statuses": {404}, "probes": ["(source only: line %s)" % lines[0]]}))

        named = [t for t in tiers if t[1] == "NAMED"]
        statusonly = [t for t in tiers if t[1] == "STATUS"]
        generic = [t for t in tiers if t[1] == "GENERIC"]

        # 3. every error a person can be shown says what went wrong itself —
        #    not the status-level sentence, which is true but says nothing
        check("no-error-falls-to-the-generic",
              not generic,
              "%d translated, %d named, %d status-only" % (len(tiers), len(named), len(statusonly))
              if not generic else "no sentence at all for: "
              + "; ".join(repr(t[0])[:44] for t in generic[:5]))

        def where(t):
            p = t[3]["probes"][0]
            return "" if "source only" in p else "  (" + p + ")"

        check("every-error-is-named",
              not statusonly,
              "%d named" % len(named) if not statusonly else
              "fell back to the HTTP status: "
              + "; ".join(repr(t[0])[:44] + where(t) for t in statusonly[:6]))

        # 4. the tooltip has to keep the original wording — a Thai sentence that
        #    replaced the technical detail outright would strand whoever debugs it
        lost = [t for t in tiers if t[0] and not t[2]["raw"]]
        check("tooltip-keeps-the-wire-text", not lost,
              "%d carry the server's own wording" % len(tiers) if not lost else
              "no tooltip text for: " + "; ".join(repr(t[0])[:40] for t in lost[:5]))

        if gaps and statusonly:
            print("\n  status-only (report only — the sentence is true but generic):")
            for e, _, o, meta in statusonly:
                print("    status %-4s %s" % (sorted(meta["statuses"])[0], repr(e)[:70]))
                print("        %s" % o["say"][:78])

        errf.flush()
        errf.seek(0)
        errtext = errf.read()
        check("no-traceback-on-bad-params", "Traceback" not in errtext,
              "%d bytes of stderr" % len(errtext))

        if keep:
            print("\nerror_check: --keep — server still on http://127.0.0.1:%d/" % port)
            print("             stop it with: taskkill //F //PID %d" % child.pid)
            return 0
    finally:
        if child.poll() is None and not keep:
            child.kill()
        errf.close()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print("\nerror_check: %d/%d PASS%s"
          % (len(results) - len(bad), len(results),
             "" if not bad else " — FAILED: " + ", ".join(bad)))
    return 1 if bad else 0


def main(argv):
    if "--serve" in argv:
        log = argv[argv.index("--log") + 1]
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
        stream_check._serve(log, port)
        return 0
    return run(keep="--keep" in argv, gaps="--gaps" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
