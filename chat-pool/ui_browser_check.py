"""ui_browser_check.py — does the dashboard's JavaScript actually WIRE UP, in a browser?

ui_check.py proves the script parses, that every `$('#id')` exists, and that the
kind filter's arithmetic is right. It proved all of that about a feature that
could not be opened: the dropdown was wired to `ks.join('|')` where `ks` is a
count OBJECT, so the guard threw a TypeError on every path that had kinds to
show, and `<select>` sat on one option forever. Eight bugs in a row, all of them
between correct arithmetic and the page, and every check was green.

The difference between that and this one: this drives the REAL control in a REAL
browser and compares **what the page rendered** against **what the corpus says**,
computed independently in Python. Not a regex, not arithmetic on extracted
functions — a reader's click, and a number on the screen.

No dependencies. Chrome is launched with `--remote-debugging-port=0` and driven
over the DevTools Protocol using node's BUILT-IN WebSocket (node >= 22); there
is no playwright, no selenium, no jsdom, and nothing to install. If neither
Chrome nor Edge can be found this SKIPS, loudly, naming what it could not run —
it never reports a pass it did not earn (§13: a check that cannot fail is not a
check, and one that pretends to have passed is worse).

Run:  python ui_browser_check.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The sandbox, the copy list and the "is the server up yet" wait are prune_check's
# job already. Imported, not retyped: a second copy of that tuple drifts the
# moment anyone adds an import, and the failure arrives from a child process
# naming nothing useful.
sys.path.insert(0, HERE)
from prune_check import COPIES, local_imports, free_port, wait_health  # noqa: E402

# Deliberately broad, and ordered: first hit wins. A check that only knows one
# browser path is a check that silently stops running on the other machine.
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "chrome", "google-chrome", "chromium", "chromium-browser", "msedge",
)

# A corpus with a real mix, so the filter has something to choose BETWEEN. Every
# line survives prune (no "test" noise), so the plane the UI draws and the map
# Python computes are the same map and any disagreement is the UI's fault.
RECIPE = (("CHAT", 40), ("bond", 26), ("DECISION", 16), ("pointer", 6),
          ("block", 3), ("msg", 2))


# ── the sandbox log ────────────────────────────────────────────────────────
def build_log(box):
    """Write a small corpus with a deliberate kind mix, and return the ground
    truth read back OUT of the library that will serve it.

    The truth is computed by pool.derived() rather than counted here, so the
    check cannot agree with the UI by sharing a bug with the thing it grades.
    """
    sys.path.insert(0, box)
    for mod in [m for m in ("pool", "ctx_plane") if m in sys.modules]:
        del sys.modules[mod]
    import hashlib

    import pool

    genesis = {
        "v": 3, "t": time.time(), "app": "system", "kind": "CHAT",
        "text": "ui_browser_check genesis", "to": "all", "tags": [], "prev": "",
        "rkey": pool.rkey(b"ui-browser-check-genesis"), "enc": 0,
    }
    genesis["hash"] = hashlib.sha256(
        ("systemCHATall" + genesis["text"]).encode("utf-8")).hexdigest()
    with open(os.path.join(box, "log.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(genesis, ensure_ascii=False) + "\n")

    i = 0
    for kind, n in RECIPE:
        for j in range(n):
            # the subject repeats on purpose: without it every line would land in
            # a cell of its own and nothing would ever hold two kinds
            pool.append("browser-check", kind,
                        "%s line %d about subject %d" % (kind, j, j % 7))
            i += 1

    d = pool.derived()
    by_kind = d["by_kind"]
    # The colour LAYER is a different question from the filter: "which cells was
    # this cell last written by". Both are printed to the reader — the filter in
    # the dropdown, the layer in the legend — so both need a truth of their own.
    layer = {}
    for k in d["cell_kind_clean"]:
        a = d["cell_kind_clean"][k] or "?"
        layer[a] = layer.get(a, 0) + 1
    truth = {
        "kinds": {k: {"cells": len(v), "lines": sum(v.values())}
                  for k, v in sorted(by_kind.items())},
        "layer": layer,
        "cells_total": len(d["cells"]),
        "lines_total": sum(d["cells"].values()),
        "mixed_cells": sum(1 for v in by_kind.values() if len(v) > 1),
        "pruned_differs": len(d["cells"]) != len(d["cells_clean"]),
    }
    del sys.modules["pool"]
    return truth, i


# ── the driver that runs inside node ────────────────────────────────────────
DRIVER = r"""
// Everything below runs in node against a page Chrome has actually rendered.
// It REPORTS what it saw. It does not decide whether that was right — the
// truth lives in Python, so a bug in this file cannot excuse a bug in the page.
const wsUrl = process.argv[2];
const pageUrl = process.argv[3];
const port = new URL(wsUrl).port;

const out = {consoleErrors: [], pageErrors: [], steps: {}};
function bail(why) { out.fatal = why; return out; }

(async () => {
  const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const page = list.find(t => t.type === 'page');
  if (!page) return bail('chrome exposed no page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = () => no(new Error('cdp socket refused')); });

  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      out.consoleErrors.push((m.params.args || []).map(a => a.value ?? a.description ?? '').join(' ').slice(0, 300));
    if (m.method === 'Runtime.exceptionThrown')
      out.pageErrors.push((m.params.exceptionDetails || {}).text + ' ' +
        ((m.params.exceptionDetails || {}).exception || {}).description);
  };
  const send = (method, params = {}) => new Promise(res => {
    const i = ++id; pending.set(i, res);
    ws.send(JSON.stringify({id: i, method, params}));
  });
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true});
    const ex = r.result && r.result.exceptionDetails;
    if (ex) throw new Error(ex.text + ' ' + ((ex.exception || {}).description || ''));
    if (r.error) throw new Error('cdp: ' + JSON.stringify(r.error));
    return r.result && r.result.result && r.result.result.value;
  };
  // A probe that answers nothing must say so. The first version returned
  // undefined here, the caller stored undefined, and every check downstream read
  // an absent report as a field that happened to be None — so a driver that
  // never ran printed a table of confident Nones instead of one failure.

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', {url: pageUrl});
  // Wait for THE APP, not for the document. `document.readyState` is 'complete'
  // on the blank page we started from, so polling it alone clicks the plane
  // button before pool_server's script has run: the markup exists, the handler
  // does not, the click is a no-op, and the suite reports a dashboard that will
  // not open — which is exactly the failure it exists to catch.
  let booted = false;
  for (let i = 0; i < 400; i++) {
    booted = await evaluate(
      'location.origin === new URL(' + JSON.stringify(pageUrl) + ').origin' +
      " && typeof PV === 'object' && !!(document.querySelector('#open-plane')||{}).onclick"
    ).catch(() => false);
    if (booted) break;
    await new Promise(r => setTimeout(r, 100));
  }
  out.booted = !!booted;

  const raw1 = await send('Runtime.evaluate', {expression: '1+1', returnByValue: true});
  out.cdpSanity = JSON.stringify(raw1.result || raw1).slice(0, 200);

  // ONE in-page program does the reading, because a round trip per fact would
  // cost more than the whole suite.
  out.report = await evaluate(`(async () => {
    const R = {steps:{}};
    const waitFor = async (fn, ms) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        let v = false;
        try { v = fn(); } catch (e) {}
        if (v) return true;
        await new Promise(r => setTimeout(r, 60));
      }
      return false;
    };
    const num = (s) => { const m = /([0-9,]+)/.exec(s || ''); return m ? +m[1].replace(/,/g, '') : null; };

    R.steps.url = location.href;
    R.steps.hasBtn = !!document.querySelector('#open-plane');
    R.steps.hasModal = !!document.querySelector('#plane-modal');
    R.steps.clickErr = null;
    try {
      document.querySelector('#open-plane').click();
    } catch (e) { R.steps.clickErr = String(e && e.message || e); }
    R.steps.modalClassNow = (document.querySelector('#plane-modal') || {}).className;
    R.steps.modalOpen = await waitFor(() => document.querySelector('#plane-modal').className === 'open', 20000);
    R.steps.modalClassAfter = (document.querySelector('#plane-modal') || {}).className;
    document.querySelector('#pv-opt').click();
    const sel = document.querySelector('#pv-kind');
    R.steps.promptAppeared = await waitFor(
      () => [...sel.options].some(o => o.value === '__load') && !sel.disabled, 20000);
    R.steps.promptText = [...sel.options].map(o => o.textContent);
    if (!R.steps.promptAppeared) return R;

    sel.value = '__load'; sel.dispatchEvent(new Event('change'));
    R.steps.layerLoaded = await waitFor(() => PV.metaOn && Object.keys(PV.kinds || {}).length > 0, 30000);
    R.list = [...sel.options].map(o => ({value: o.value, text: o.textContent, n: num(o.textContent)}));
    R.disabled = sel.disabled;
    if (!R.steps.layerLoaded) return R;

    const readFrame = () => {
      const info = document.querySelector('#plane-info').textContent;
      const f = pvFilt();
      const src = (PV.seq === null || PV.seq >= PV.seqMax - 1) ? PV.plane : pvCellsUpTo(PV.seq);
      const keys = Object.keys(src);
      const lit = f ? keys.filter(k => f[k]) : keys;
      return {
        infoText: info,
        statusCells: num((/สว่าง\\s*([0-9,]+)\\s*ช่อง/.exec(info) || [])[1]),
        statusLines: num((/·\\s*([0-9,]+)\\s*บรรทัดของชนิดนี้/.exec(info) || [])[1]),
        frameCells: keys.length,
        frameLines: Object.values(src).reduce((a, b) => a + b, 0),
        litCells: lit.length,
        litLines: f ? lit.reduce((a, k) => a + f[k], 0) : Object.values(src).reduce((a, b) => a + b, 0),
        // row[3] is the kind and row[2] the rkey — the same two columns pvCellsUpTo
        // reads. Asking row[2] 'for the kind' returns rkeys, which is how the
        // first version reported 27 'stray kinds' that were addresses.
        kindsInFrame: [...new Set(PV.timeline.slice(0, (PV.seq === null ? PV.seqMax : PV.seq + 1)).map(r => r[3]))],
        emptyClaimed: /ยังไม่มีข้อมูลชนิดนี้/.test(info),
      };
    };

    R.perKind = {};
    for (const kind of Object.keys(PV.kinds)) {
      sel.value = kind; sel.dispatchEvent(new Event('change'));
      await new Promise(r => setTimeout(r, 350));
      R.perKind[kind] = Object.assign({selected: sel.value}, readFrame());
    }

    // A kind that is NOT in the corpus cannot be reached through the control at
    // all — assigning an unknown value to a <select> silently reverts it to "" —
    // so that case belongs to the node probe, which can set the state directly.
    // What a reader CAN hit is a kind that exists but not yet AT THIS TICK, and
    // the contract is the same: the filter stays ON and says so, rather than
    // quietly drawing the unfiltered plane under a filter that claims to be on.
    const seqEl = document.querySelector('#pv-seq');
    sel.value = Object.keys(PV.kinds)[Object.keys(PV.kinds).length - 1];
    sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    PV.seq = 0; seqEl.value = 0; seqEl.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 700));
    R.absent = Object.assign({kindOnly: PV.kindOnly, filtIsNull: pvFilt() === null}, readFrame());

    // scrub back, with a real filter on, and read the frame again
    sel.value = Object.keys(PV.kinds)[0]; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    PV.seq = Math.floor(+seqEl.max * 0.4); seqEl.value = PV.seq; seqEl.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 700));
    R.scrubbed = Object.assign({seq: PV.seq, kindOnly: PV.kindOnly}, readFrame());

    PV.seq = null; sel.value = ''; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 400));
    R.cleared = Object.assign({kindOnly: PV.kindOnly, filtIsNull: pvFilt() === null}, readFrame());

    // The legend under the kind colour layer prints "how many cells carry this
    // kind LAST" beside each swatch. Counting that map with n[a]=n[a]||0 reads
    // a count and writes it straight back, so every swatch said (0) and sorting
    // a column of zeros still looked sorted — the dropdown never noticed,
    // because the dropdown counts a DIFFERENT map.
    const col = document.querySelector('#pv-colour');
    col.value = 'kind'; col.dispatchEvent(new Event('change'));
    R.legendLoaded = await waitFor(
      () => /สี = ชนิดของบรรทัด/.test(document.querySelector('#pv-legend').textContent), 20000);
    await new Promise(r => setTimeout(r, 400));
    R.legend = [...document.querySelectorAll('#pv-legend span[title]')]
      .map(s => {const m = /^([^·]+)·\\s*(\\d+)\\s*ช่อง/.exec(s.getAttribute('title') || ''); return m ? {label: m[1].trim(), n: +m[2]} : null;})
      .filter(Boolean);
    R.legendText = document.querySelector('#pv-legend').textContent.slice(0, 300);
    return R;
  })()`);

  if (out.report === undefined) out.reportMissing = true;
  ws.close();
  return out;
})().then(out => { console.log(JSON.stringify(out)); }).catch(e => {
  console.log(JSON.stringify({fatal: String(e && e.message || e)}));
});
"""


def launch_browser(profile):
    """Start Chrome headless and return (process, devtools websocket url).

    `--remote-debugging-port=0` makes Chrome pick a free port and PRINT it, which
    is why the url comes back out of stderr rather than being guessed: a hard
    coded port is a collision waiting for the day run_all is run twice.
    """
    exe = next((b for b in BROWSERS if (os.path.isfile(b) or shutil.which(b))), None)
    if not exe:
        return None, None
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--remote-debugging-port=0", "--user-data-dir=" + profile,
            "--window-size=1280,900", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True,
                            errors="ignore", encoding="utf-8")
    end = time.time() + 30
    while time.time() < end:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                return proc, None
            continue
        m = re.search(r"(ws://\S+/devtools/browser/\S+)", line)
        if m:
            return proc, m.group(1)
    return proc, None


# ── the checks ─────────────────────────────────────────────────────────────
def check(report, truth):
    # the legend's title carries the WIRE kind ("CHAT · 28 ช่อง"), not the Thai
    # label, so both maps key the same way and each is graded against its own
    page_truth_layer = truth["layer"]
    """Compare what the page RENDERED against what the corpus says. Returns a
    list of (label, ok, detail) — the shape run_all already knows how to print."""
    r = []

    def add(label, ok, detail):
        r.append((label, bool(ok), detail))

    fatal = report.get("fatal")
    if fatal:
        add("the browser actually ran", False, fatal)
        return r
    add("the browser actually ran", bool(report.get("booted")),
        "chrome reported a devtools socket and the dashboard's own script ran"
        if report.get("booted") else
        "the dashboard never finished booting: PV or the plane button's handler "
        "was missing after 40s — the check drove a page that was not the app")

    if report.get("reportMissing") or not isinstance(report.get("report"), dict):
        add("the page driver produced a reading", False,
            "the in-page program returned nothing (cdp sanity: %s) — every "
            "number below would be an absent value read as a result"
            % report.get("cdpSanity"))
        return r
    add("the page driver produced a reading", True,
        "the in-page program answered")
    # node wraps the page's own answer as out.report; the steps, the list and
    # every frame live INSIDE it. Reading them off the wrapper is how the first
    # version reported eleven confident Nones about a dashboard that had opened,
    # loaded its layer and listed every kind correctly.
    page = report["report"]
    st = page.get("steps") or {}
    add("the plane opens", st.get("modalOpen"),
        "btn=%r modal=%r class %r after click (was %r%s) url=%s" % (
            st.get("hasBtn"), st.get("hasModal"),
            st.get("modalClassAfter"), st.get("modalClassNow"),
            "" if not st.get("clickErr") else ", click threw: " + str(st.get("clickErr")),
            st.get("url")))
    # THE first bug, and the one no arithmetic check can see: the control has to
    # be REACHABLE. It shipped disabled, so it could never fire the handler that
    # loads the layer it needs.
    add("the kind control is reachable before the layer is loaded",
        st.get("promptAppeared"),
        "options offered: %s" % (st.get("promptText") or []))
    add("the kind layer loads on request", st.get("layerLoaded"),
        "PV.metaOn + PV.kinds populated")
    add("no uncaught page errors", not report.get("pageErrors"),
        "; ".join(report.get("pageErrors") or [])[:400] or "none")
    add("no console errors", not report.get("consoleErrors"),
        "; ".join(report.get("consoleErrors") or [])[:400] or "none")

    listed = {o["value"]: o["n"] for o in (page.get("list") or []) if o["value"]}
    missing = sorted(set(truth["kinds"]) - set(listed))
    add("every kind in the corpus is offered", not missing,
        "missing: %s" % missing if missing else "%d kinds" % len(truth["kinds"]))
    zero = sorted(k for k, n in listed.items() if k in truth["kinds"] and not n)
    add("no kind is listed with a zero count", not zero,
        "zero: %s" % zero if zero else "all counts > 0")

    # THE promise: the number in the box must be the number the filter lights.
    # This shipped quoting the COLOUR LAYER instead ("cells whose last line was
    # this") — 1961 in the box against 2651 lit, for one click.
    wrong = []
    for k, want in sorted(truth["kinds"].items()):
        got = (page.get("perKind") or {}).get(k) or {}
        if listed.get(k) != want["cells"]:
            wrong.append("%s box=%s truth=%s" % (k, listed.get(k), want["cells"]))
        if got.get("litCells") != want["cells"]:
            wrong.append("%s lit=%s truth=%s" % (k, got.get("litCells"), want["cells"]))
    add("the number in the box is the number it lights", not wrong,
        "; ".join(wrong)[:400] or "%d kinds agree" % len(truth["kinds"]))

    over = []
    seen = page.get("perKind") or {}
    if not seen:
        add("the status line names the corpus, on every kind", False,
            "the page was never driven far enough to select a kind")
        seen = {}
    else:
        for k, want in sorted(truth["kinds"].items()):
            got = seen.get(k) or {}
            if got.get("statusCells") != want["cells"] or got.get("statusLines") != want["lines"]:
                over.append("%s status=%s/%s truth=%s/%s" % (
                    k, got.get("statusCells"), got.get("statusLines"),
                    want["cells"], want["lines"]))
        add("the status line names the corpus, on every kind", not over,
            "; ".join(over)[:400] or "live plane agrees for all kinds")

    # THE contract of the whole feature, read off the rendered page: the picture
    # may show less than the corpus holds, never more.
    worse = []
    # A missing reading is a FAILURE, never a pass. Comparing a default of 0
    # against a truth of 3 "passes" when the page reported nothing at all — the
    # first version of this suite printed three green lines on a dashboard it
    # had never managed to open, which is the failure mode it was written to
    # catch, wearing its clothes.
    if not seen:
        worse.append("no frame was read for any kind")
    for k, want in sorted(truth["kinds"].items()):
        got = seen.get(k) or {}
        if got.get("litCells") is None or got.get("litLines") is None:
            worse.append("%s: the page reported no count" % k)
            continue
        if got["litCells"] > want["cells"] or got["litLines"] > want["lines"]:
            worse.append("%s claims %s/%s of a corpus holding %s/%s" % (
                k, got["litCells"], got["litLines"], want["cells"], want["lines"]))
    add("the filter may show less, never more", not worse,
        "; ".join(worse)[:400] or "no kind overstates the corpus")

    # An absent kind must stay ON and say so. Falling back to the unfiltered map
    # would draw the whole plane under a filter that claims to be on — the one
    # answer this layer must never give.
    absent = page.get("absent") or {}
    add("a kind with nothing here keeps its filter and says so",
        bool(absent) and absent.get("filtIsNull") is False
        and bool(absent.get("kindOnly")) and absent.get("emptyClaimed") is True
        and absent.get("litCells") == 0,
        "at tick 0 with kind %r: filterNull=%r claimsEmpty=%r lit=%r status=%r" % (
            absent.get("kindOnly"), absent.get("filtIsNull"),
            absent.get("emptyClaimed"), absent.get("litCells"),
            (absent.get("infoText") or "")[-80:]))

    scr = page.get("scrubbed") or {}
    kinds = set(scr.get("kindsInFrame") or [])
    stray = sorted(kinds - {scr.get("kindOnly")})
    add("scrubbing back cannot smuggle another kind in",
        bool(scr.get("kindOnly")) and not stray,
        "stray kinds in the scrubbed frame: %s" % stray if stray
        else "frame at tick %s holds only %s" % (scr.get("seq"), scr.get("kindOnly")))
    # Shipped wrong three times in a row: the whole log (13,572 against a
    # picture holding 1,421), then whole-log f[k] summed over an already-filtered
    # src (7,938), then `lit.length` on an object (reported empty while 943 were
    # lit). The number must describe THE FRAME.
    frame_ok = (scr.get("statusCells") is not None
                and scr.get("statusCells") == scr.get("litCells")
                and scr.get("statusLines") == scr.get("frameLines")
                and scr.get("emptyClaimed") is False)
    add("the scrubbed frame's numbers describe that frame", frame_ok,
        "status=%s/%s vs frame=%s/%s cells=%s empty=%r" % (
            scr.get("statusCells"), scr.get("statusLines"),
            scr.get("litCells"), scr.get("frameLines"),
            scr.get("frameCells"), scr.get("emptyClaimed")))

    # The legend quotes the LAYER map; the dropdown quotes the FILTER map. Both
    # are numbers about the same picture and they are not the same number, so
    # each is graded against its own truth.
    leg = {e["label"]: e["n"] for e in (page.get("legend") or [])}
    zeroed = sorted(l for l, n in leg.items() if not n)
    add("the kind legend names a count for every swatch",
        bool(page.get("legendLoaded")) and not zeroed,
        "legend read: %s%s" % (
            ", ".join("%s=%s" % (l, n) for l, n in sorted(leg.items()))[:240],
            " · ZERO: %s" % zeroed if zeroed else ""))
    layer_ok = [l for l, n in sorted(leg.items()) if n != page_truth_layer.get(l)]
    add("every legend count matches the corpus", not layer_ok,
        "; ".join("%s legend=%s corpus=%s" % (l, leg[l], page_truth_layer.get(l))
                  for l in layer_ok)[:400]
        or "%d swatches agree with the corpus" % len(leg))

    cl = page.get("cleared") or {}
    add("clearing the filter returns the whole plane",
        cl.get("filtIsNull") is True and cl.get("frameCells") == truth["cells_total"],
        "cleared frame=%s cells, corpus=%s" % (
            cl.get("frameCells"), truth["cells_total"]))
    return r


def main():
    node = shutil.which("node")
    if not node:
        print("ui_browser_check: SKIPPED — node is not installed, so nothing "
              "here ran. This suite drives Chrome over the DevTools Protocol "
              "using node's built-in WebSocket and installs nothing.")
        return 0
    try:
        major = int(re.search(r"v?(\d+)", subprocess.run(
            [node, "-v"], capture_output=True, text=True).stdout).group(1))
    except Exception:
        major = 0
    if major < 22:
        print("ui_browser_check: SKIPPED — node %d has no global WebSocket "
              "(needs 22+). Nothing ran." % major)
        return 0

    box = tempfile.mkdtemp(prefix="ui-browser-")
    proc = server = browser = None
    results = []
    try:
        missing = local_imports()
        if missing:
            print("ui_browser_check: FAILED — the sandbox cannot boot; these "
                  "modules are imported by pool_server.py and would not travel "
                  "with it: %s" % ", ".join(missing))
            return 1
        for name in COPIES:
            src = os.path.join(HERE, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(box, name))

        truth, nlines = build_log(box)
        print("ui_browser_check: sandbox %s" % box)
        print("  log: %d lines · %d cells · %d kinds · %d cells hold >1 kind%s"
              % (nlines, truth["cells_total"], len(truth["kinds"]),
                 truth["mixed_cells"],
                 " · pruned view differs" if truth["pruned_differs"] else ""))

        port = free_port()
        env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8")
        out = open(os.path.join(box, "server.out"), "w", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, os.path.join(box, "pool_server.py")],
            cwd=box, env=env, stdout=out, stderr=subprocess.STDOUT)
        if not wait_health(port, server):
            print("  FAIL server: did not come up on %d" % port)
            print(open(os.path.join(box, "server.out"), encoding="utf-8",
                       errors="ignore").read()[-800:])
            return 1

        profile = os.path.join(box, "chrome")
        browser, ws = launch_browser(profile)
        if not ws:
            print("  FAIL browser: no devtools socket. Chrome said: %s"
                  % ((browser.stderr.read() if browser else "") or "")[-300:])
            return 1

        drv = os.path.join(box, "drive.js")
        with open(drv, "w", encoding="utf-8") as f:
            f.write(DRIVER)
        r = subprocess.run([node, drv, ws, "http://127.0.0.1:%d/" % port],
                           capture_output=True, text=True, timeout=300)
        try:
            report = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            print("  FAIL driver: no verdict on stdout.\n  %s\n  %s"
                  % (r.stdout[-600:], r.stderr[-600:]))
            return 1
        results = check(report, truth)
    finally:
        for p in (browser, server):
            if p:
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(box, ignore_errors=True)

    bad = 0
    for label, ok, detail in results:
        print("  %-5s %-52s %s" % ("PASS" if ok else "FAIL", label, detail))
        bad += not ok
    if os.environ.get("UI_BROWSER_DUMP"):
        print("---- raw report ----")
        print(json.dumps(report, ensure_ascii=False, indent=1)[:4000])
    print("ui_browser_check: %d/%d PASS" % (len(results) - bad, len(results)))
    if bad:
        print("  (the dashboard is judged against the corpus computed in "
              "Python, not against itself — a filter that shows more than the "
              "log holds is the one failure no reader can catch)")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())