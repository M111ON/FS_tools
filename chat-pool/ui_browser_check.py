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
#
# Four writers, not one. The kind axis only has anything to choose between if
# cells hold several kinds, and the WRITER axis only has anything to choose
# between if cells hold several writers — the two are different partitions and a
# corpus that made one interesting would leave the other trivially "correct".
# The kind totals are held identical to the one-writer recipe (CHAT 40, bond 26,
# DECISION 16, pointer 6, block 3, msg 2) so the kind figures this suite already
# asserts are unchanged by adding the second axis.
RECIPE = (("index", "bond", 26), ("index", "DECISION", 12),
          ("chatdrop", "CHAT", 24), ("chatdrop", "msg", 2),
          ("chatdrop", "block", 3), ("opencode", "CHAT", 16),
          ("opencode", "pointer", 6), ("hermes", "DECISION", 4))


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

    # The corpus is written through pool.append, which writes to whatever log
    # sits next to the pool.py that got imported. If that file is the REAL one,
    # this function appends a test corpus to the reader's actual log — and it
    # did, once, during development: 93 lines, removed by hand. A sandbox that
    # silently is not a sandbox is worse than no sandbox, so this refuses.
    if os.path.dirname(os.path.abspath(pool.__file__)) != os.path.abspath(box):
        raise SystemExit(
            "ui_browser_check: REFUSING to build. pool.py resolved to %s, not the "
            "sandbox %s — writing this corpus would append to the real log."
            % (pool.__file__, box))

    genesis = {
        "v": 3, "t": time.time(), "app": "system", "kind": "CHAT",
        "text": "ui_browser_check genesis", "to": "all", "tags": [], "prev": "",
        "rkey": pool.rkey(b"ui-browser-check-genesis"), "enc": 0,
    }
    genesis["hash"] = hashlib.sha256(
        ("systemCHATall" + genesis["text"]).encode("utf-8")).hexdigest()
    # ONE line with no writer at all. `app` is optional in the format, and the
    # plane has to put such a line SOMEWHERE: by_app keys on `app or "?"`, and a
    # writer called "null" in a dropdown is the alternative. The check below
    # cannot see this rule unless the corpus contains the case it is about.
    orphan = {"v": 3, "t": time.time(), "kind": "msg",
              "text": "ui_browser_check line with no writer", "to": "all",
              "tags": [], "prev": genesis["hash"],
              "rkey": pool.rkey(b"ui-browser-check-orphan"), "enc": 7}
    orphan["hash"] = hashlib.sha256(
        ("orphan" + orphan["text"]).encode("utf-8")).hexdigest()
    with open(os.path.join(box, "log.jsonl"), "w", encoding="utf-8") as f:
        for b in (genesis, orphan):
            f.write(json.dumps(b, ensure_ascii=False) + "\n")

    i = 0
    for app, kind, n in RECIPE:
        for j in range(n):
            # the subject repeats on purpose: without it every line would land in
            # a cell of its own and nothing would ever hold two kinds — or two
            # writers, which is what the second axis needs to be interesting.
            #
            # A bond is the ONE kind whose text is a grammar, not a sentence:
            # "src rel dst". Every recipe line used to be prose, so every bond
            # was malformed and the corpus had NO EDGES AT ALL — which meant the
            # relation axis of the composition strip had nothing to draw and no
            # check could grade it. An axis nothing in the corpus exercises is an
            # axis nobody has tested.
            text = ("subj%d %s subj%d" % (j % 7, ["uses", "next", "answers",
                                                  "same"][j % 4], (j + 3) % 7)
                    if kind == "bond" else
                    "%s line %d about subject %d" % (kind, j, j % 7))
            pool.append(app, kind, text)
            i += 1

    d = pool.derived()
    # ── two writers in ONE cell ───────────────────────────────────────────
    # An rkey is derived from (app, kind, text), so two DIFFERENT apps can never
    # land in the same address through append(). That quietly made the sandbox
    # blind to the whole point of this axis: "which cells HOLD this writer" and
    # "who wrote this cell LAST" can only ever be the same question here, so
    # borrowing one number for the other — bug 5, the mistake the kind filter
    # shipped — would have passed.
    #
    # These lines are written by hand with an explicit rkey for exactly that
    # reason, and in this order: hermes writes into four addresses chatdrop
    # holds, and then chatdrop writes into those same four again. chatdrop HOLDS
    # them and does not end up owning them, which is the disagreement the filter
    # has to be graded against.
    shared = sorted(pool.derived()["by_app"].get("chatdrop") or {})[:4]
    extra = []
    for i, rk in enumerate(shared):
        for app, kind in (("hermes", "DECISION"), ("chatdrop", "msg")):
            body = {"v": 3, "t": time.time(), "app": app, "kind": kind,
                    "text": "%s revisits address %d" % (app, i), "to": "all",
                    "tags": [], "prev": "", "rkey": rk, "enc": i}
            body["hash"] = hashlib.sha256(
                ("shared-%s-%d" % (app, i)).encode("utf-8")).hexdigest()
            extra.append(body)
    if extra:
        with open(os.path.join(box, "log.jsonl"), "a", encoding="utf-8") as f:
            for b in extra:
                f.write(json.dumps(b, ensure_ascii=False) + "\n")
        pool._DERIVED["key"] = None          # the file moved; do not trust the cache

    truth = _truth_from(pool)
    del sys.modules["pool"]
    return truth, i


def _truth_from(pool):
    """Every ground truth this file grades against, read out of the library."""
    d = pool.derived()
    by_kind = d["by_kind"]
    by_app = d["by_app"]
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
        # The same shape for the other partition, and the reason it is NOT the
        # same number: by_kind answers "which cells HOLD this kind" while the
        # writer legend answers "which cells were last written by this one".
        "apps": {a: {"cells": len(v), "lines": sum(v.values())}
                 for a, v in sorted(by_app.items())},
        # (ordinal, rkey) in append order, per axis. The page picks its own tick
        # to scrub back to, so the truth for a SCRUBBED frame can only be
        # computed here, afterwards, from the tick the page reported.
        "kind_seq": {}, "app_seq": {},
        "layer": layer,
        "cells_total": len(d["cells"]),
        "lines_total": sum(d["cells"].values()),
        "lines_read": len(d["timeline"]),
        "mixed_cells": sum(1 for v in by_kind.values() if len(v) > 1),
        "mixed_apps": sum(1 for v in by_app.values() if len(v) > 1),
        # the composition strip reads /patterns, so its truth is the census —
        # and each axis is graded against its OWN map, because "who wrote this
        # LAST" and "which cells hold this writer" are different numbers (bug 5)
        "census": {
            "kinds": dict(pool.pattern_census()["kinds"]),
            "writers": {a: v["lines"] for a, v in pool.pattern_census()["by_app"].items()},
            "rels": pool.pattern_census()["rels"],
            "shapes": pool.pattern_census()["shapes"],
        },
        "pruned_differs": len(d["cells"]) != len(d["cells_clean"]),
    }
    for i, row in enumerate(d["timeline"]):
        rk = row[1]
        if rk is None:
            continue
        truth["kind_seq"].setdefault(row[2], []).append((i, rk))
        truth["app_seq"].setdefault(row[7], []).append((i, rk))
    return truth


def reread_truth(box):
    """The truth again, AFTER the server has been running.

    The corpus is written first and graded last, and in between the server
    started — which registers itself, installs a session guard and starts a
    bridge thread, so it APPENDS to the very log the truth was computed from.
    Grading the page against a corpus that had since grown is how the legend
    check started failing on a healthy plane: `pointer` was 6 when the truth
    was read and 7 by the time the page drew it, and nothing was wrong with
    either number. The truth has to describe the log the server actually read.
    """
    sys.path.insert(0, box)
    for mod in [m for m in ("pool", "ctx_plane") if m in sys.modules]:
        del sys.modules[mod]
    import pool
    try:
        return _truth_from(pool)
    finally:
        sys.modules.pop("pool", None)


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
// Module scope so the failure path can close the socket: leaving it open keeps
// node alive forever, so a driver that threw used to cost the suite its full
// 300s timeout and then report a timeout — which says nothing about the error
// that had already been printed above it.
let WS = null;
const bye = () => { try { if (WS) WS.close(); } catch (x) {} };

(async () => {
  const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const page = list.find(t => t.type === 'page');
  if (!page) return bail('chrome exposed no page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  WS = ws;
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
  // The tour must not MUTATE the corpus it grades. Opening a cell asks for
  // /neighbors?...&record=1, which declares a SIGHTING — and a sighting goes
  // through append(), into the very log this file's ground truth was read from.
  // The plane was fetched before the tour and the who/when layer after it, so a
  // single click left the legend claiming `pointer` on a cell the truth had not
  // heard of: the suite grading a moving target, and the app blamed for it.
  // Installed before navigation, so it is in place before the app's script runs.
  const QUIET_READS =
    "(function(){var f=window.fetch;window.fetch=function(u,o){" +
    "try{if(typeof u==='string'&&u.indexOf('record=1')>=0)u=u.replace(/&?record=1/,'');}catch(e){}" +
    "return f.call(this,u,o);};})()";
  await send('Page.addScriptToEvaluateOnNewDocument', {source: QUIET_READS});
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
    // Every phase names itself here. A driver that stalls used to take the
    // whole suite down as an opaque 300-second subprocess timeout, which says
    // nothing about WHERE it stopped — so the page program now carries its own
    // budget and reports the last phase it reached. A stall is a result.
    window.__pvPhase = 'boot';
    const PH = (n) => { window.__pvPhase = n; };
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

    PH('open-plane'); R.steps.url = location.href;
    R.steps.hasBtn = !!document.querySelector('#open-plane');
    R.steps.hasModal = !!document.querySelector('#plane-modal');
    R.steps.clickErr = null;
    try {
      document.querySelector('#open-plane').click();
    } catch (e) { R.steps.clickErr = String(e && e.message || e); }
    R.steps.modalClassNow = (document.querySelector('#plane-modal') || {}).className;
    R.steps.modalOpen = await waitFor(() => document.querySelector('#plane-modal').className === 'open', 20000);
    R.steps.modalClassAfter = (document.querySelector('#plane-modal') || {}).className;
    PH('find-controls'); document.querySelector('#pv-opt').click();
    const sel = document.querySelector('#pv-kind');
    const selA = document.querySelector('#pv-app');
    R.steps.promptAppeared = await waitFor(
      () => [...sel.options].some(o => o.value === '__load') && !sel.disabled, 20000);
    R.steps.promptText = [...sel.options].map(o => o.textContent);
    // The SAME promise on the second control. A filter whose list cannot ask for
    // its own layer is a control that cannot be opened, and the second axis was
    // written by copying the first — so it is exactly the copy that might have
    // shipped disabled.
    R.steps.appPromptAppeared = await waitFor(
      () => selA && [...selA.options].some(o => o.value === '__load') && !selA.disabled, 5000);
    R.steps.appPromptText = selA ? [...selA.options].map(o => o.textContent) : null;
    if (!R.steps.promptAppeared || !R.steps.appPromptAppeared) return R;

    // Pay through the WRITER control. The layer is one fetch; both lists must be
    // filled from it, so choosing the second control must leave the first one
    // offering real kinds instead of a stale "โหลดรายชื่อชนิด…".
    PH('load-layer'); selA.value = '__load'; selA.dispatchEvent(new Event('change'));
    R.steps.layerLoaded = await waitFor(
      () => PV.metaOn && Object.keys(PV.kinds || {}).length > 0
           && Object.keys(PV.apps || {}).length > 0, 30000);
    R.steps.paidViaAppControl = !!PV.apps && Object.keys(PV.apps).length > 0;
    R.list = [...sel.options].map(o => ({value: o.value, text: o.textContent, n: num(o.textContent)}));
    R.listApp = [...selA.options].map(o => ({value: o.value, text: o.textContent, n: num(o.textContent)}));
    R.disabled = sel.disabled;
    R.disabledApp = selA.disabled;
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
        statusLines: num((/·\\s*([0-9,]+)\\s*บรรทัดของ/.exec(info) || [])[1]),
        frameCells: keys.length,
        frameLines: Object.values(src).reduce((a, b) => a + b, 0),
        litCells: lit.length,
        litLines: f ? lit.reduce((a, k) => a + f[k], 0) : Object.values(src).reduce((a, b) => a + b, 0),
        // row[3] is the kind and row[2] the rkey — the same two columns pvCellsUpTo
        // reads. Asking row[2] 'for the kind' returns rkeys, which is how the
        // first version reported 27 'stray kinds' that were addresses.
        kindsInFrame: [...new Set(PV.timeline.slice(0, (PV.seq === null ? PV.seqMax : PV.seq + 1)).map(r => r[3]))],
        // the WIRE row is [ordinal,t,rkey,kind,hash8,writer] — six columns. row[7] is
        // the index the FOLD uses, which reads as undefined here, and asking
        // row[2] (the rkey) for the writer returns addresses: two ways to
        // collect a list of "writers" that is not a list of writers.
        appsInFrame: [...new Set(PV.timeline.slice(0, (PV.seq === null ? PV.seqMax : PV.seq + 1)).map(r => r[5]))],
        emptyClaimed: /ยังไม่มีข้อมูล(ชนิด|ผู้เขียน)นี้/.test(info),
        filt: PV.filt ? PV.filt.dim + ':' + PV.filt.key : null,
      };
    };

    PH('per-kind'); R.perKind = {};
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
    PH('absent-at-tick-0'); const seqEl = document.querySelector('#pv-seq');
    sel.value = Object.keys(PV.kinds)[Object.keys(PV.kinds).length - 1];
    sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    PV.seq = 0; seqEl.value = 0; seqEl.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 700));
    R.absent = Object.assign({kindOnly: PV.filt, filtIsNull: pvFilt() === null}, readFrame());

    PH('scrub-kind'); // scrub back, with a real filter on, and read the frame again
    sel.value = Object.keys(PV.kinds)[0]; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    PV.seq = Math.floor(+seqEl.max * 0.4); seqEl.value = PV.seq; seqEl.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 700));
    R.scrubbed = Object.assign({seq: PV.seq, kindOnly: PV.filt}, readFrame());

    PV.seq = null; sel.value = ''; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 400));
    R.cleared = Object.assign({kindOnly: PV.filt, filtIsNull: pvFilt() === null}, readFrame());

    // ── the WRITER axis, driven through its own control ──────────────────
    PH('per-app'); R.perApp = {};
    for (const app of Object.keys(PV.apps)) {
      selA.value = app; selA.dispatchEvent(new Event('change'));
      await new Promise(r => setTimeout(r, 350));
      R.perApp[app] = Object.assign(
        {selected: selA.value, kindSel: sel.value, kindFilt: PV.filt && PV.filt.dim},
        readFrame());
    }
    // ONE state, TWO controls: choosing the writer axis must clear the kind
    // control, because there is no intersection to honour and a reader shown
    // "ทุกชนิด" next to a live filter has been told two things at once.
    PH('axis-swap'); R.axisSwap = {afterApp: null, afterKind: null};
    R.axisSwap.afterApp = {app: selA.value, kind: sel.value, dim: PV.filt && PV.filt.dim};
    sel.value = Object.keys(PV.kinds)[0]; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    R.axisSwap.afterKind = {kind: sel.value, app: selA.value, dim: PV.filt && PV.filt.dim};

    PH('scrub-app'); // the same scrub contract on the other axis, filtered on the
    // WRITER WITH NO NAME if the corpus has one: that is the line the fold
    // normalises to '?', and a rule nothing in the corpus exercises is a rule
    // no check can see.
    const orphanApp = Object.keys(PV.apps).indexOf('?') >= 0 ? '?' : Object.keys(PV.apps)[0];
    selA.value = orphanApp; selA.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    // halfway, not tick 0: a frame with nothing in it agrees with a broken
    // filter by being empty, so the only interesting scrub for a one-line
    // writer is one where that line HAS happened
    PV.seq = Math.floor(+seqEl.max * 0.5); seqEl.value = PV.seq;
    seqEl.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 700));
    R.appScrubbed = Object.assign({seq: PV.seq, appOnly: PV.filt}, readFrame());
    const appsInScrub = new Set(R.appScrubbed.appsInFrame || []);
    R.appScrubbed.strayWriters = [...appsInScrub].filter(a => a !== (PV.filt && PV.filt.key));
    PV.seq = null; selA.value = ''; selA.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 400));

    // ── the three view modes, clicked for real ───────────────────────────
    // Three modes, three different addressing rules, and until now nothing
    // drove them: ui_check proved the SCRIPT names them, and ui_browser_check
    // only ever looked at the plane. A button that sets PV.mode and draws
    // nothing is the same class of bug as the dropdown that could not open.
    // With a filter ON, because the point of the tour is that a VIEW must not
    // change what the filter says.
    sel.value = Object.keys(PV.kinds)[0]; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 350));
    PH('mode-geometry'); const geom = () => {
      const step = pstep(), dpr = window.devicePixelRatio || 1;
      const px = Math.max(PV.w, PV.h) * step;
      const r = pcv.getBoundingClientRect();
      // Take the busiest few addresses the mode is actually drawing, and check
      // the pixel the painter puts them at hits back to the address the hit-test
      // claims. That is the round trip: a mode whose draw and whose click
      // disagree cannot be corrected by looking at it.
      const src = PV.cells;
      const sample = Object.keys(src).map(Number)
        .sort((a, b) => src[b] - src[a]).slice(0, 6);
      const round = sample.map(k => {
        const c = pvCenter(k);
        if (!c) return {k, back: null, ok: false};
        const hit = pvCellAt({clientX: r.left + c[0], clientY: r.top + c[1]});
        if (!hit) return {k, back: null, ok: false};
        const xy = pvXY(hit[2]);
        return {k, back: hit[2], ok: xy[0] === hit[0] && xy[1] === hit[1]};
      });
      // Did the canvas get INK, or is this a mode that draws nothing at all?
      // The WHOLE canvas, sampled on a stride: a fixed corner is not evidence,
      // because in the block view one step is 46px and a small corpus can
      // legitimately live nowhere near the top-left of the plane.
      let ink = -1;
      try {
        const c2 = pcv.getContext('2d');
        const W = pcv.width, H = pcv.height;
        const d = c2.getImageData(0, 0, W, H).data;
        const stride = Math.max(1, Math.floor(Math.min(W, H) / 160));
        ink = 0;
        for (let y = 0; y < H; y += stride)
          for (let x = 0; x < W; x += stride) {
            const i = (y * W + x) * 4;
            if (d[i] > 42 || d[i + 1] > 42 || d[i + 2] > 42) ink++;
          }
        ink = {lit: ink, sampled: Math.ceil(H / stride) * Math.ceil(W / stride)};
      } catch (e) { ink = -1; }
      return {
        mode: PV.mode, w: PV.w, h: PV.h, win: PV.win, step: step,
        cssPx: pcv.style.width,
        backing: pcv.width === Math.round(px * dpr) && pcv.height === pcv.width,
        square: PV.w === PV.h,
        round: round,
        inked: ink,
        statusCells: num((/สว่าง\\s*([0-9,]+)\\s*ช่อง/.exec(document.querySelector('#plane-info').textContent) || [])[1]),
        btnOn: ['#pv-plane', '#pv-block', '#pv-blocks']
          .filter(s => document.querySelector(s).classList.contains('on')),
      };
    };
    R.modes = {};
    for (const [id, name] of [['#pv-plane', 'plane'], ['#pv-block', 'window'], ['#pv-blocks', 'blocks']]) {
      PH('mode:' + name); document.querySelector(id).click();
      await new Promise(r => setTimeout(r, 450));
      R.modes[name] = geom();
    }
    document.querySelector('#pv-plane').click();
    await new Promise(r => setTimeout(r, 300));

    sel.value = ''; sel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 400));

    // The legend under the kind colour layer prints "how many cells carry this
    // kind LAST" beside each swatch. Counting that map with n[a]=n[a]||0 reads
    // a count and writes it straight back, so every swatch said (0) and sorting
    // a column of zeros still looked sorted — the dropdown never noticed,
    // because the dropdown counts a DIFFERENT map.
    //
    // It is read AFTER the mode tour on purpose. Both legend boxes live inside
    // #plane-detail, and opening a cell replaces that container's innerHTML — so
    // the first click of a session destroyed both, their painters bail at
    // 'if(!box)return', and nothing on screen ever said a word about it. Clicking
    // "▢ กรอบเลื่อน" calls openPoint, so the tour is what finds it.
    R.legendBeforeClicks = !!document.querySelector('#pv-legend');
    // ── the composition strip: the four axes, through their own buttons ──
    PH('composition');
    const readStrip = () => [...document.querySelectorAll('#pv-comp .cenrow')].map(el => ({
      label: ((el.querySelector('span') || {}).textContent || '').trim(),
      text: ((el.querySelector('.cennum') || {}).textContent || '').trim(),
      dim: el.dataset.cmpDim || null,
      clickable: el.style.cursor === 'pointer',
    }));
    R.strip = {};
    const AXES = ['kind', 'app', 'rel', 'shape'];
    for (let i = 0; i < AXES.length; i++) {
      document.querySelector('#pv-comp [data-cmp="' + i + '"]').click();
      await new Promise(r => setTimeout(r, 250));
      R.strip[AXES[i]] = readStrip();
    }
    // click a KIND row and confirm the filter it claims to set is the one on.
    // Back to axis 0 first: after the tour the strip is showing shape, and a
    // query for a kind row in a shape listing finds nothing — which would have
    // been reported as "the row does not work".
    document.querySelector('#pv-comp [data-cmp="0"]').click();
    await new Promise(r => setTimeout(r, 250));
    const krow = document.querySelector('#pv-comp [data-cmp-dim="kind"]');
    R.stripClick = null;
    if (krow) {
      krow.click();
      await new Promise(r => setTimeout(r, 450));
      R.stripClick = Object.assign({dim: PV.filt && PV.filt.dim,
                                    key: PV.filt && PV.filt.key}, readFrame());
      // clicking it again clears it — re-query, because the strip re-renders
      // and a detached row's click never reaches the delegation point
      const krow2 = document.querySelector('#pv-comp [data-cmp-dim="kind"]');
      if (krow2) krow2.click();
      await new Promise(r => setTimeout(r, 450));
      R.stripUnclick = PV.filt === null;
    }
    // ── the named views: ONE dropdown that sets all five axes ──────────
    // A preset is a second store of view state unless it is proved to be the
    // same store: it writes PV, the controls repaint from PV, and the reader
    // can still hand-edit any of the five afterwards. Each of those three
    // promises fails silently, and none of them fails loudly on screen — a
    // menu that says "ชนิด" over a plane coloured by writer looks fine.
    PH('presets');
    const readView = () => {
      const c = (id) => { const e = document.querySelector(id); return e ? e.value : null; };
      const note = document.querySelector('#pv-preset-note');
      // With no filter the status line does not say "สว่าง N ช่อง" (that is the
      // FILTERED wording); unfiltered its short form reads "N/20,736 ช่อง".
      // Asking for the filtered phrase here read nothing and looked like an
      // empty map, which is a failing check for a healthy plane.
      const info = document.querySelector('#plane-info').textContent;
      let sig = '';
      try {
        const c2 = pcv.getContext('2d'), W = pcv.width, H = pcv.height;
        const d = c2.getImageData(0, 0, W, H).data;
        const stride = Math.max(1, Math.floor(Math.min(W, H) / 120));
        let a = 0, b = 0;
        for (let y = 0; y < H; y += stride)
          for (let x = 0; x < W; x += stride) {
            const i = (y * W + x) * 4;
            a = (a * 31 + d[i] + d[i + 1] * 3 + d[i + 2] * 7) % 1000003;
            if (d[i] > 42 || d[i + 1] > 42 || d[i + 2] > 42) b++;
          }
        sig = a + ':' + b;
      } catch (e) { sig = 'err'; }
      return {
        preset: PV.preset, mode: PV.mode, colour: PV.colour, style: PV.style,
        hubK: PV.hubK, win: PV.win, seq: PV.seq, seqMax: PV.seqMax,
        w: PV.w, h: PV.h, step: pstep(), sig: sig,
        ui: {colour: c('#pv-colour'), style: c('#pv-style'), hubK: c('#pv-hubk'),
             win: c('#pv-win'), preset: c('#pv-preset')},
        note: note ? note.textContent : '',
        noteShown: note ? getComputedStyle(note).display !== 'none' : false,
        frameCells: num((/([0-9,]+)\\/20,736\\s*ช่อง/.exec(info) || [])[1]),
        lit: num((/สว่าง\\s*([0-9,]+)\\s*ช่อง/.exec(info) || [])[1]),
        when: (document.querySelector('#pv-when') || {}).textContent || '',
      };
    };
    const presSel = document.querySelector('#pv-preset');
    R.presetOrder = presSel ? [...presSel.options].map(o => o.value).filter(Boolean) : [];
    R.presetDecl = {};
    R.presetNotes = {};
    (typeof PV_PRESETS !== 'undefined' ? PV_PRESETS : [])
      .forEach(p => { R.presetDecl[p.id] = p.v; R.presetNotes[p.id] = p.note; });
    R.presets = {};
    for (const id of R.presetOrder) {
      PH('preset:' + id);
      presSel.value = id; presSel.dispatchEvent(new Event('change'));
      await new Promise(r => setTimeout(r, 650));
      R.presets[id] = readView();
    }
    // a preset that READS a layer must ask for it the way a click does: a
    // reader is allowed to arrive at "who wrote this" without first visiting a
    // colour dropdown, and the layer is not in the counts payload
    PH('preset-layer');
    PV.metaOn = false; PV.cellApp = {}; PV.cellSeq = {}; PV.cellKind = {};
    presSel.value = 'who'; presSel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 1200));
    R.presetLayer = {metaOn: !!PV.metaOn, writers: Object.keys(PV.cellApp).length,
                     preset: PV.preset};
    // the claim must die the moment a hand touches one of the five
    presSel.value = R.presetOrder[0]; presSel.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 600));
    R.presetBeforeTouch = PV.preset;
    const styleEl = document.querySelector('#pv-style');
    styleEl.value = styleEl.value === 'degree' ? 'flat' : 'degree';
    styleEl.dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 600));
    R.touched = Object.assign({preset: PV.preset,
                               sel: document.querySelector('#pv-preset').value,
                               note: (document.querySelector('#pv-preset-note') || {}).textContent || ''},
                              readView());

    PH('legend'); const col = document.querySelector('#pv-colour');
    col.value = 'kind'; col.dispatchEvent(new Event('change'));
    R.legendLoaded = await waitFor(
      () => /สี = ชนิดของบรรทัด/.test(document.querySelector('#pv-legend').textContent), 20000);
    await new Promise(r => setTimeout(r, 400));
    R.legend = [...document.querySelectorAll('#pv-legend span[title]')]
      .map(s => {const m = /^([^·]+)·\\s*(\\d+)\\s*ช่อง/.exec(s.getAttribute('title') || ''); return m ? {label: m[1].trim(), n: +m[2]} : null;})
      .filter(Boolean);
    R.legendText = document.querySelector('#pv-legend').textContent.slice(0, 300);
    PH('done'); return R;
  })()`);

  if (out.report === undefined) {
    out.reportMissing = true;
    // The page program ran but never came back: report WHICH phase it stopped
    // in rather than letting the subprocess timeout say nothing at all.
    try {
      out.stalledIn = await evaluate('window.__pvPhase || "unknown"');
    } catch (e) { out.stalledIn = 'unknown'; }
  }
  bye();
  return out;
})().then(out => { bye(); console.log(JSON.stringify(out)); }).catch(e => {
  bye();
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
def _upto(pairs, tick):
    """The cells/lines an axis had contributed by `tick` — the truth a scrubbed
    frame has to match. `pairs` is the corpus's own (ordinal, rkey) list."""
    if not pairs or tick is None:
        return {}
    out = {}
    for i, rk in pairs:
        if i <= tick:
            out[rk] = out.get(rk, 0) + 1
    return out


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
    scrkey = (scr.get("kindOnly") or {}).get("key")
    # The frame is graded against the CORPUS AT THAT TICK, not against the log.
    # The first version asked "does the scrubbed slice contain a kind that is
    # not the filtered one" — which reads the raw timeline, so it passed or
    # failed on rows the plane had already filtered out and never proved the
    # plane had filtered anything. Computing the truth here, at the tick the page
    # chose, is the only version that says what it means.
    skind = _upto(truth["kind_seq"].get(scrkey), scr.get("seq"))
    add("scrubbing back shows exactly what the corpus held at that tick",
        bool(scrkey) and scr.get("litCells") == len(skind)
        and scr.get("litLines") == sum(skind.values()),
        "tick %s kind %s: page lit %s/%s, corpus %s/%s" % (
            scr.get("seq"), scrkey, scr.get("litCells"), scr.get("litLines"),
            len(skind), sum(skind.values())))
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
    # The legend boxes live INSIDE #plane-detail and opening a cell replaces that
    # container's innerHTML, so the first click of a session used to destroy
    # both legends — silently, because their painters begin with
    # `if(!box)return`. The mode tour clicks a mode that opens a point, so this
    # is read AFTER it: a legend that is still there is one that survived.
    add("the legend survives a cell being opened",
        page.get("legendBeforeClicks") is True and bool(leg),
        "legend present after the mode tour: %r · swatches: %s" % (
            page.get("legendBeforeClicks"),
            ", ".join("%s=%s" % (l, n) for l, n in sorted(leg.items()))[:160] or "none"))
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

    # ── the WRITER axis ────────────────────────────────────────────────────
    # Added 2026-10-03 with by_app. Everything above it was written for the kind
    # axis, and a second axis added by copying the first is exactly where the
    # copy stops agreeing with the original — so it gets its own truth here
    # rather than being assumed to work because the first one does.
    add("the writer control is reachable before the layer is loaded",
        st.get("appPromptAppeared"),
        "options offered: %s" % (st.get("appPromptText") or []))
    add("the writer layer arrives on request, through the writer control",
        st.get("layerLoaded") and st.get("paidViaAppControl"),
        "PV.apps populated after the writer's own __load: %r" % st.get("paidViaAppControl"))

    alisted = {o["value"]: o["n"] for o in (page.get("listApp") or []) if o["value"]}
    amissing = sorted(set(truth["apps"]) - set(alisted))
    add("every writer in the corpus is offered", not amissing,
        "missing: %s" % amissing if amissing else "%d writers" % len(truth["apps"]))
    azero = sorted(a for a, n in alisted.items() if a in truth["apps"] and not n)
    add("no writer is listed with a zero count", not azero,
        "zero: %s" % azero if azero else "all counts > 0")

    awrong = []
    for a, want in sorted(truth["apps"].items()):
        got = (page.get("perApp") or {}).get(a) or {}
        if alisted.get(a) != want["cells"]:
            awrong.append("%s box=%s truth=%s" % (a, alisted.get(a), want["cells"]))
        if got.get("litCells") != want["cells"]:
            awrong.append("%s lit=%s truth=%s" % (a, got.get("litCells"), want["cells"]))
        if got.get("statusCells") != want["cells"] or got.get("statusLines") != want["lines"]:
            awrong.append("%s status=%s/%s truth=%s/%s" % (
                a, got.get("statusCells"), got.get("statusLines"),
                want["cells"], want["lines"]))
    add("the writer filter lights exactly what it promises", not awrong,
        "; ".join(awrong)[:400] or "%d writers agree" % len(truth["apps"]))

    swap = page.get("axisSwap") or {}
    one_at_a_time = (
        (swap.get("afterApp") or {}).get("dim") == "app"
        and not (swap.get("afterApp") or {}).get("kind")
        and (swap.get("afterKind") or {}).get("dim") == "kind"
        and not (swap.get("afterKind") or {}).get("app"))
    add("one axis at a time: the other control shows no filter",
        one_at_a_time,
        "after choosing a writer: kind control=%r dim=%r · after choosing a kind: "
        "writer control=%r dim=%r" % (
            (swap.get("afterApp") or {}).get("kind"), (swap.get("afterApp") or {}).get("dim"),
            (swap.get("afterKind") or {}).get("app"), (swap.get("afterKind") or {}).get("dim")))

    ascr = page.get("appScrubbed") or {}
    ascrkey = (ascr.get("appOnly") or {}).get("key")
    sapp = _upto(truth["app_seq"].get(ascrkey), ascr.get("seq"))
    add("the scrubbed writer frame is the corpus at that tick",
        bool(ascrkey) and ascr.get("litCells") == len(sapp)
        and ascr.get("litLines") == sum(sapp.values()),
        "tick %s writer %s: page lit %s/%s, corpus %s/%s" % (
            ascr.get("seq"), ascrkey, ascr.get("litCells"), ascr.get("litLines"),
            len(sapp), sum(sapp.values())))
    aframe_ok = ((ascr.get("litCells") == 0
                  and ascr.get("emptyClaimed") is True
                  and ascr.get("statusCells") is None)
                 or (ascr.get("litCells")
                     and ascr.get("statusCells") == ascr.get("litCells")
                     and ascr.get("statusLines") == ascr.get("frameLines")
                     and ascr.get("emptyClaimed") is False))
    add("the scrubbed writer frame's numbers describe that frame", aframe_ok,
        "status=%s/%s vs frame=%s/%s empty=%r" % (
            ascr.get("statusCells"), ascr.get("statusLines"),
            ascr.get("litCells"), ascr.get("frameLines"), ascr.get("emptyClaimed")))

    # ── the three view modes ───────────────────────────────────────────────
    modes = page.get("modes") or {}
    if not modes:
        add("all three view modes are reachable from their own buttons", False,
            "the page was never driven through the mode buttons")
    else:
        want_btn = {"plane": "#pv-plane", "window": "#pv-block", "blocks": "#pv-blocks"}
        gone = sorted(set(("plane", "window", "blocks")) - set(modes))
        wrong_mode = [m for m, g in modes.items()
                      if g.get("mode") != m or want_btn[m] not in (g.get("btnOn") or [])]
        add("all three view modes are reachable from their own buttons",
            not gone and not wrong_mode,
            "visited: %s%s" % (sorted(modes),
                               " · wrong mode or button: %s" % wrong_mode if wrong_mode else ""))

        # The pitch rule is the one this file spent a commit moving OFF the cell
        # count and onto the mode's name. Asserted in a browser, per mode, from
        # the step the canvas is actually using.
        pitch = []
        for m, g in sorted(modes.items()):
            lo, hi = (2, 8) if m == "plane" else (8, 84)
            if not isinstance(g.get("step"), int) or not lo <= g["step"] <= hi:
                pitch.append("%s step=%s outside %s..%s" % (m, g.get("step"), lo, hi))
            span = {"plane": 144, "window": g.get("win"), "blocks": 12}.get(m)
            if g.get("w") != span or g.get("h") != span:
                pitch.append("%s drew %sx%s, expected %s" % (m, g.get("w"), g.get("h"), span))
            if not g.get("backing"):
                pitch.append("%s backing store %s does not match its css size %s"
                             % (m, g.get("cssPx"), g.get("step")))
        add("each mode draws at the pitch its own name asks for", not pitch,
            "; ".join(pitch)[:400] or " · ".join(
                "%s %sx%s step %s" % (m, g.get("w"), g.get("h"), g.get("step"))
                for m, g in sorted(modes.items())))

        trip = ["%s: address %s drew at %s but hit-tests to %s" % (
            m, rt.get("k"), rt.get("back"), "nowhere") for m, g in sorted(modes.items())
            for rt in g.get("round") or [] if not rt.get("ok")]
        add("what a mode draws hits back to what it draws", not trip,
            "; ".join(trip)[:400] or " · ".join(
                "%s %d/%d addresses round-tripped" % (
                    m, sum(1 for rt in g.get("round") or [] if rt.get("ok")),
                    len(g.get("round") or [])) for m, g in sorted(modes.items())))

        blank = ["%s drew nothing at all (0 of %s sampled pixels)"
                 % (m, (g.get("inked") or {}).get("sampled")) for m, g in sorted(modes.items())
                 if not isinstance(g.get("inked"), dict) or g["inked"].get("lit", 0) <= 0]
        add("no mode opens onto a blank canvas", not blank,
            "; ".join(blank) or " · ".join(
                "%s %s/%s px" % (m, (g.get("inked") or {}).get("lit"),
                                 (g.get("inked") or {}).get("sampled"))
                for m, g in sorted(modes.items())))

        # A view is not a filter. Same corpus, same filter, three answers.
        said = {m: g.get("statusCells") for m, g in modes.items()}
        one_answer = len(set(said.values())) == 1 and None not in said.values()
        add("the view does not change what the filter says", one_answer,
            "status per mode: %s (the filter was %r throughout)" % (
                said, (modes.get("plane") or {}).get("filt")))
    # ── the composition strip ──────────────────────────────────────────────
    # The reader opens the plane and cannot answer "what is this made of"
    # without a button press, so the strip is graded against the census the
    # server computed — every axis against its OWN map, because the writer axis
    # counts lines while the writer LEGEND counts last-writers, and using the
    # wrong one is bug 5 with a new address.
    strip = page.get("strip") or {}
    want = {"kind": truth["census"]["kinds"], "app": truth["census"]["writers"],
            "rel": truth["census"]["rels"], "shape": truth["census"]["shapes"]}
    wrong = []
    for ax, rows in sorted(want.items()):
        got = strip.get(ax)
        if not got:
            wrong.append("%s: the strip drew no rows" % ax)
            continue
        if len(got) != len(rows):
            wrong.append("%s: %d rows for %d groups" % (ax, len(got), len(rows)))
        # Each row must state a number, and it must be one the corpus has.
        # Comparing the LABEL would be weaker: two kinds can share a count, and
        # the strip renames them for the reader, so the label is not an identity.
        ns = []
        for row in got:
            m = re.match(r"^([\d,]+)\s*·\s*<?\d+%$", row["text"] or "")
            if not m:
                wrong.append("%s: row %r reads %r, which states no count"
                             % (ax, row["label"], row["text"]))
                continue
            n_ = int(m.group(1).replace(",", ""))
            ns.append(n_)
            if n_ not in rows.values():
                wrong.append("%s: %s shows %s, a number the corpus does not have"
                             % (ax, row["label"], n_))
        if ns != sorted(ns, reverse=True):
            wrong.append("%s: rows are not sorted by size: %s" % (ax, ns))
    add("every composition axis matches the census", not wrong,
        "; ".join(wrong)[:400] or " · ".join(
            "%s %d groups" % (a, len(strip.get(a) or [])) for a in sorted(strip)))

    clickable = {"kind", "app"}
    plain = [ax for ax in strip if ax not in clickable
             and any(r["clickable"] for r in strip[ax] or [])]
    add("an axis the scrub cannot honour does not pretend it filters",
        not plain,
        "clickable on: %s" % plain if plain
        else "relation and shape are read-only, and say so in their tooltip")

    sc = page.get("stripClick")
    add("clicking a strip row sets exactly the filter it names",
        bool(sc) and sc.get("dim") == "kind" and bool(sc.get("key"))
        and isinstance(sc.get("statusCells"), int) and sc["statusCells"] > 0,
        "row click -> %r · status lit %s cells%s" % (
            (sc or {}).get("key"), (sc or {}).get("statusCells"),
            "" if page.get("stripUnclick") else " · clicking it again did NOT clear"))
    add("clicking the same row again clears the filter", page.get("stripUnclick") is True,
        "PV.filt after a second click: %r" % (page.get("stripUnclick"),))

    # ── the named views ────────────────────────────────────────────────
    presets = page.get("presets") or {}
    order = page.get("presetOrder") or []
    decl = page.get("presetDecl") or {}
    add("every view the menu offers was actually driven", len(order) >= 4 and set(order) == set(presets),
        "the menu offers %d, the tour clicked %d%s" % (
            len(order), len(presets),
            "" if set(order) == set(presets) else " · unvisited: %s"
            % sorted(set(order) - set(presets))))
    # One store, not two. The picker and the five controls are all views of PV,
    # so after a preset the controls must read back the fields it wrote. A
    # preset that set the state and left the boxes showing the old values would
    # pass every arithmetic check in the file.
    mirror = []
    notes = page.get("presetNotes") or {}
    for pid in sorted(presets):
        v = presets[pid]
        if v.get("preset") != pid:
            mirror.append("%s: PV.preset=%r, so the name is not carried by the state"
                          % (pid, v.get("preset")))
        if (v.get("ui") or {}).get("preset") != pid:
            mirror.append("%s: the picker shows %r" % (pid, (v.get("ui") or {}).get("preset")))
        for fld in ("colour", "style", "hubK", "win"):
            if (v.get("ui") or {}).get(fld) != str(v.get(fld)):
                mirror.append("%s: #pv-%s shows %r while PV.%s is %r"
                              % (pid, fld, (v.get("ui") or {}).get(fld), fld, v.get(fld)))
        # the sentence has to be THIS view's. A caption borrowed from the first
        # entry in the array is a caption describing another picture, which is
        # the whole failure the picker exists to avoid.
        if (v.get("note") or "").strip() != (notes.get(pid) or "").strip():
            mirror.append("%s: the sentence on screen is %r, not its own %r"
                          % (pid, v.get("note"), notes.get(pid)))
        elif not v.get("noteShown"):
            mirror.append("%s: its sentence is not shown" % pid)
    add("a named view sets the controls it claims to set", not mirror,
        "; ".join(mirror)[:400] or "%d views, each with its five controls and its sentence"
        % len(presets))
    # And it must do what it says — a menu of eight labels that all land on the
    # same picture is worse than no menu, because it looks like a choice.
    claims = []
    for pid in sorted(presets):
        v, d = presets[pid], decl.get(pid) or {}
        if not d:
            claims.append("%s: offered by the menu, described by nothing" % pid)
            continue
        for fld in ("mode", "colour", "style", "hubK"):
            if d.get(fld) is not None and v.get(fld) != d[fld]:
                claims.append("%s: says %s=%r and the state is %r" % (pid, fld, d[fld], v.get(fld)))
        if d.get("win") is not None and v.get("win") != d["win"]:
            claims.append("%s: says a %r frame and drew %r" % (pid, d["win"], v.get("win")))
        # the shape has to be the shape it names, in CELLS and not in buttons
        side = {"plane": 144, "blocks": 12}.get(v.get("mode"), d.get("win"))
        if side is not None and v.get("w") != side:
            claims.append("%s: mode %s drew a %sx%s grid" % (pid, v.get("mode"), v.get("w"), v.get("h")))
        # and time has to be where it says — one preset parks it, the rest do not
        maxs = max((v.get("seqMax") or 1) - 1, 0)
        if d.get("seqFrac") is not None:
            want = int(maxs * d["seqFrac"])
            if v.get("seq") is None or abs(v["seq"] - want) > 1:
                claims.append("%s: parks time at %r, wanted ~%d of %d"
                              % (pid, v.get("seq"), want, maxs))
        elif v.get("seq") is not None:
            claims.append("%s: says every line and parked time at %r" % (pid, v.get("seq")))
        if not v.get("frameCells"):
            claims.append("%s: the status line counts %r cells with lines in them"
                          % (pid, v.get("frameCells")))
    add("each named view lands on the picture it advertises", not claims,
        "; ".join(claims)[:400] or "%s" % " · ".join(
            "%s %s/%s %s%s cells" % (pid, presets[pid].get("mode"), presets[pid].get("colour"),
                                     presets[pid].get("style"), presets[pid].get("w"))
            for pid in sorted(presets)))
    # The views cannot all paint the same pixels, or half the menu is the same
    # picture wearing different labels. A sampled checksum of the canvas is the
    # honest witness; the threshold is low on purpose because a small corpus can
    # legitimately make two views identical (with 26 edges, hub-drawing draws
    # every hub there is) and the check is about the MENU, not about drawing.
    sigs = {v.get("sig") for v in presets.values()}
    add("the named views do not all paint the same picture", len(sigs) >= 3,
        "%d distinct canvases over %d views · %s" % (
            len(sigs), len(presets),
            " · ".join("%s=%s" % (pid, presets[pid].get("sig")) for pid in sorted(presets))))
    t = page.get("touched") or {}
    add("hand-editing a control drops the name the menu was claiming",
        bool(page.get("presetBeforeTouch")) and not t.get("preset")
        and not t.get("sel") and not t.get("note"),
        "menu chose %r; after touching #pv-style: PV.preset=%r, picker=%r, note=%r"
        % (page.get("presetBeforeTouch"), t.get("preset"), t.get("sel"), t.get("note")))
    pl = page.get("presetLayer") or {}
    add("a named view asks for the layer it reads",
        bool(pl.get("metaOn")) and bool(pl.get("writers")),
        "with the writer layer forced off, choosing \"who wrote this\" left "
        "metaOn=%r with %r writers mapped%s"
        % (pl.get("metaOn"), pl.get("writers"),
           "" if pl.get("preset") else " · and lost its own name"))
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
    report = {}
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
        print("  log: %d lines · %d cells · %d kinds (%d cells hold >1) · %d writers (%d cells hold >1)%s"
              % (nlines, truth["cells_total"], len(truth["kinds"]), truth["mixed_cells"],
                 len(truth["apps"]), truth["mixed_apps"],
                 " · pruned view differs" if truth["pruned_differs"] else ""))

        port = free_port()
        env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8")
        out = open(os.path.join(box, "server.out"), "w", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, os.path.join(box, "pool_server.py")],
            cwd=box, env=env, stdout=out, stderr=subprocess.STDOUT)
        # Popen duplicates the handle for the child, so the parent's copy can go
        # straight back. Holding it open for the rest of main() is what left
        # server.out undeletable on Windows, and therefore the whole sandbox
        # undeletable, one directory per run.
        out.close()
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
        # The truth was read before the browser existed. If anything wrote to
        # the corpus while the page was being driven, every comparison below is
        # against a corpus the page never saw — so say so loudly instead of
        # grading a moving target and blaming the app.
        fresh = reread_truth(box)
        results = check(report, truth)
        results.append((
            "the corpus did not move while the page was driven",
            fresh["lines_read"] == truth["lines_read"],
            "the sandbox held %d lines when the truth was read and %d when the "
            "verdict came back — every figure here describes the first"
            % (truth["lines_read"], fresh["lines_read"])))
    except subprocess.TimeoutExpired as e:
        # A driver that never returns is a FAILURE with a name, not a
        # traceback: the sandbox log is already gone by the time the traceback
        # would be read, so it told nobody what had happened.
        print("  FAIL driver: still running after 300s. stdout tail: %r"
              % ((e.stdout or b"")[-300:]))
        return 1
    finally:
        for p in (browser, server):
            if p:
                try:
                    p.kill()
                    # wait for it to actually be gone. Chrome holds its profile
                    # open for a moment after the signal, and a directory that
                    # is still open cannot be removed on Windows.
                    p.wait(timeout=15)
                except Exception:
                    pass
        # NOT ignore_errors=True. That is how 105 sandboxes, each with a ~50MB
        # Chrome profile in it, piled up in TEMP until the disk was full — every
        # run left its directory behind, silently, and the suite that is
        # supposed to notice things going wrong was the thing going wrong. If the
        # directory cannot go, say so, with its name.
        for _attempt in range(3):
            try:
                shutil.rmtree(box)
                break
            except OSError:
                time.sleep(1.0)
        else:
            print("  WARN  the sandbox could not be removed and is still at %s "
                  "(~50MB of Chrome profile). Delete it by hand." % box)

    bad = 0
    for label, ok, detail in results:
        print("  %-5s %-52s %s" % ("PASS" if ok else "FAIL", label, detail))
        bad += not ok
    if os.environ.get("UI_BROWSER_DUMP"):
        print("---- raw report ----")
        print(json.dumps(report, ensure_ascii=False, indent=1)[:12000])
    print("ui_browser_check: %d/%d PASS" % (len(results) - bad, len(results)))
    if bad:
        print("  (the dashboard is judged against the corpus computed in "
              "Python, not against itself — a filter that shows more than the "
              "log holds is the one failure no reader can catch)")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())