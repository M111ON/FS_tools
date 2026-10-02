"""ui_check.py — does the dashboard's JavaScript actually parse, and does every
element it grabs by id actually exist?

Written because it did not parse, once, and nothing noticed: a single misplaced
brace in an added function (an object literal starting with `(`) turned the whole
<script> block into a parse error, so every handler silently disappeared while
the server kept answering 200 and the Python self-check kept passing 94/94.
The browser console said `SyntaxError` and nothing else did.

The second check is the same failure one step later. `$('#x').onclick=…` on an
element that is not in the page throws, and because the whole dashboard is one
script, everything wired AFTER that line silently stays unbound: buttons that
render, look fine, and do nothing. Caught for real — moving a button out of the
header while leaving its `$('#…')` behind killed the tick rail, the replay keys
and the strip scrubber at once, with the page still drawing perfectly.

The third check is the same idea one step further in. The failures it guards are
INVISIBLE: the page renders, every element exists, the server answers 200, and
`pool.py demo` passes — only the MEANING of the view is wrong. Each invariant is
a "one value drives two things and they were allowed to disagree" rule that
actually shipped broken:

  * the composer cleared the textarea before it knew the append landed, so a
    failed send erased the draft, with no message (no `.catch` either);
  * `setIdentity` changed what the page SHOWED but not what the SSE stream
    DELIVERED, so picking "view as hermes" left the stream sending admin's
    every line — the header said hermes, the feed was admin's;
  * `#id-sel` was rebuilt from the loaded window alone, which dropped any
    identity with no lines in it, so the control displayed `admin` while the
    page was filtered as someone else.

  * `#plane-pop` lived inside a box that is `overflow:hidden` and 165px tall on
    a phone, and was 164px tall itself — clipped by construction, with a clamp
    that could not help (a popup taller than its container cannot be clamped
    into it). It is a `popover` now, positioned against the viewport;
  * `pvDraw` reallocated the canvas backing store on every call (30 assignments
    for 30 draws) while a single plane draw repainted 10,131 cells in 27.7ms —
    on a path where every zoom frame repainted the whole map.

The last three guard the WORDS, which fail the same invisible way: the page is
correct and unreadable. The app is read by people who did not build it, so the
screen says "ช่วงที่ 12" where the wire says `tick`, "ตัวกรองขยะ" where the code
says `curator`, and a Thai sentence where the server says `not registered`. The
rules that keep that true are checkable:

  * no internal vocabulary may reach a text node or a static tooltip;
  * a server error may be COPIED into the translator and nowhere else — the one
    shape that regressed was `toast('ยังไม่ส่ง: '+r.error)`, an English string
    with a HTTP status attached, shown to someone who cannot act on either;
  * a rule added to `CURATOR_RULES` in pool.py without a word in `RULE_TH` in the
    dashboard silently puts a code name (`hex-blob 3`) back on the screen — this
    one spans two files, which is exactly why it needs a check.

These are regexes over the source rather than a browser, so they are phrased
loosely (whitespace-tolerant) and deliberately few: each one is a rule whose
violation you cannot see by looking at the page on a wide screen.

This does not need a server, a browser, or a token: it imports the page out of
pool_server.py, extracts the script block, and hands it to a real parser. Node
is used when present because that is the same engine the agent runs on; if
node is missing the check says so and exits 2 rather than pretending to pass.

    python ui_check.py            # check the dashboard as it stands in the file
    python ui_check.py --url http://localhost:8390/   # check what is actually served
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

DIR = os.path.dirname(os.path.abspath(__file__))


def extract_script(html):
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("ui_check: no <script> block found — did the dashboard move?")
    return "\n".join(blocks)


def page_html(url=None):
    if url:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8")
    sys.path.insert(0, DIR)
    import pool_server                      # importing does NOT start the server
    return pool_server.DASHBOARD


def parse_js(js):
    node = shutil.which("node")
    if not node:
        return 2, ("ui_check: SKIPPED — node is not installed, so the JavaScript "
                   "in %d bytes of dashboard was NOT parsed" % len(js)), ""
    tmp = os.path.join(tempfile.mkdtemp(prefix="ui-check-"), "dashboard.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(js)
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    return r.returncode, r.stdout + r.stderr, "%d bytes of dashboard JavaScript parsed" % len(js)


def missing_ids(html, js):
    """Every `$('#id')` must match an id in the markup — including ids written by
    the script itself, because parts of the page (the popup, the sub-grid, the
    legend rows) are generated into innerHTML."""
    have = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", html))
    want = re.findall(r"""\$\('#([A-Za-z0-9_-]+)'\)""", js)
    return sorted({i for i in want if i not in have})


def block(js, start, end):
    """The slice between two markers, or "" when either is missing.

    Used instead of a `.{0,300}` window because a window is a guess: the first
    version of the identity invariant silently PASSED on the broken code because
    the explanatory comment between `identity=a;` and `listen();` was longer
    than the window. A check that is satisfied by a comment is not a check.
    """
    i = js.find(start)
    if i < 0:
        return ""
    j = js.find(end, i + len(start))
    return js[i:j] if j > i else ""


# ── the words ────────────────────────────────────────────────────────────────
# Internal vocabulary, by the name the wire and the code use for it. None of
# these may appear in a text node or a static tooltip: each one is a word the
# screen already has a plainer substitution for.
BANNED = (
    "tick", "curator", "prune", "bond", "hub", "bundle", "degree", "rkey",
    "sighting", "chunk", "registry", "inbox", "flush", "blast radius",
    "session-block", "magic-context", "torus", "who/when", "dedup", "RDH",
    "addr ", "lines", "line(s)", "sub ", "similarity", "noise", "pointer",
)


def visible_labels(html):
    """What a person actually reads: text nodes and static tooltips.

    Tooltips are included because half of this UI explains itself on hover, and
    a title is as read as a label. A title built by the script (`title="'+x+'"`)
    is skipped: its words come from data, not from a literal we can police.
    """
    body = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    body = re.sub(r"<style>.*?</style>", "", body, flags=re.S)
    labels = [n.strip() for n in re.findall(r">([^<>{}]+)<", body)]
    for t in re.findall(r'title="([^"]*)"', html):
        if "'+" not in t and "${" not in t:
            labels.append(t)
    return [s for s in labels if s.strip()]


def vocabulary_on_screen(html):
    hits = []
    for s in visible_labels(html):
        low = s.lower()
        for term in BANNED:
            if term.lower() in low:
                hits.append((term, s))
                break
    return hits


def error_leaks(js):
    """Every read of the server's `error` field except the translator's own.

    A presence test is fine — `if(r.error)` decides whether to speak, it does
    not speak. What must not come back is the wire string rendered to a person.
    """
    rest = js.replace(block(js, "function sayError", "// a memory's status is a wire value"), "")
    rest = re.sub(r"if\s*\(\s*!?\s*[\w$.]+\.error\s*\)", "", rest)
    return re.findall(r".{0,60}\.error.{0,40}", rest)


def _js_keys(js, name):
    m = re.search(re.escape(name) + r"\s*=\s*\{([^{}]*)\}", js)
    if not m:
        return None
    inner = m.group(1)
    keys = re.findall(r"(?:^|,)\s*'([^']+)'\s*:", inner)
    keys += re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$-]*)\s*:", inner)
    return set(keys)


def pool_source():
    with open(os.path.join(DIR, "pool.py"), encoding="utf-8") as f:
        return f.read()


def _code_matches(js, pattern):
    """Regex matches that are CODE, not prose.

    The §13.5 lesson, inverted. That rule stopped a check being satisfied BY a
    comment; this stops a comment SATISFYING a check — the sentence explaining
    why `kind==='chat'` is wrong contains `kind==='chat'`, so a naive scan
    counts the explanation as a second live comparison and the check fails on
    correct code. Block comments are blanked first (the explanation is a `/* */`
    block), then a `//` earlier on the same line means prose.

    Accepted limit: a `/*` inside a JS regex or string literal would be treated
    as a comment. The dashboard has none, and the failure mode is a check that
    under-reports — not one that passes something broken.
    """
    js = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), js, flags=re.S)
    out = []
    for m in re.finditer(pattern, js):
        start = js.rfind("\n", 0, m.start()) + 1
        if "//" in js[start:m.start()]:
            continue
        out.append(m)
    return out


def kind_words(js):
    """The kind palette's words, checked against the kinds the server acts on.

    Same shape as the curator-rule check (§17.5), for the same reason: the
    kind names live on the wire and the plain words live in the dashboard, so
    the join between two files is what can rot. A kind with no word is printed
    raw — `bond` and `pointer` are exactly the words this project already ruled
    off the screen, so a missing entry is not a cosmetic gap.

    Compared case-insensitively, because that is the rule the server itself
    follows: `curator_protect()` compares a kind by VALUE and never by
    spelling, and it had to be fixed for exactly this reason (§6). A check that
    matched `chat` against `CHAT` would demand a second entry for one kind and
    teach the next reader that kinds are spellings.

    The reverse direction cannot be sourced from pool.py — CHAT/DECISION/msg/
    bridge are written by platforms, and the server never branches on them —
    so it is checked where it IS knowable: every word in the table must have a
    colour, or the legend spends a label on something the canvas cannot draw.
    """
    py = pool_source()
    kinds = {k.lower() for k in re.findall(r'kind\s*==\s*"([\w-]+)"', py)}
    for grp in re.findall(r'kind\s+in\s+\(([^)]*)\)', py):
        kinds |= {w.strip('"\'').lower() for w in grp.replace('"', ' ').split()}
    # ONE table for one job. Two KIND_TH tables is exactly the defect §17.5
    # documents for the curator rules — the one that shipped as a silent
    # `hex-blob 3` on screen — because whichever copy a renderer reads can lag
    # the other and the page still renders perfectly.
    tables = re.findall(r"const KIND_TH\s*=", js)
    if len(tables) != 1:
        return [("KIND_TH declared %d times" % len(tables),
                 "one table, read by both the feed badge and the plane legend — "
                 "two copies is how a word stops being translated on one of them")]
    m = re.search(r"const KIND_TH\s*=\s*\{(.*?)\};", js, re.S)
    if not m:
        return ["kind word table", "<the whole KIND_TH table is gone>"]
    table = dict(re.findall(r"'?([\w-]+)'?\s*:\s*'([^']*)'", m.group(1)))
    mh = re.search(r"const KIND_HUE\s*=\s*\{(.*?)\};", js, re.S)
    hues = {k.lower() for k in re.findall(r"([\w-]+)\s*:", mh.group(1))} if mh else set()
    low = {k.lower() for k in table}
    gaps = []
    gaps += [("kind with no plain word", k) for k in sorted(kinds - low)]
    # a word identical to the wire name is a word that was never translated
    gaps += [("word is the wire name, not a translation", k)
             for k, v in sorted(table.items()) if v.lower() == k.lower()]
    gaps += [("plain word with no colour in the palette", k) for k in sorted(low - hues)]
    gaps += [("colour in the palette with no plain word", k) for k in sorted(hues - low)]
    return gaps


def vocabulary_gaps(js):
    """(what, names) for every internal name the screen has no word for.

    Three sources, one rule: a name the app can put on screen must have a word.
    The rule list and the protection reasons come from pool.py, so adding a rule
    to the server is what makes this check fail — not editing the dashboard.

    Bidirectional: a rule in pool.py must have a word in RULE_TH (forward),
    AND every word in RULE_TH must correspond to a rule in pool.py (reverse).
    The reverse catches orphaned translations left behind when a rule is deleted
    — the Thai word still renders on screen for a rule that no longer fires,
    so it either mislabels something or sits as dead weight.
    """
    py = pool_source()
    m = re.search(r"CURATOR_RULES\s*=\s*\((.*?)\n\)", py, re.S)
    rules = set(re.findall(r'\("([\w-]+)"\s*,', m.group(1))) if m else set()
    rules.add("repeat")            # added by the repeat-window pass, not the table
    reasons = set(re.findall(r'return\s+"(\w+)"',
                             block(py, "def curator_protect", "def curator_hits")))
    gaps = []
    for label, name, keys in (("filter rule", "RULE_TH", rules),
                              ("protection reason", "PROTECT_TH", reasons)):
        have = _js_keys(js, name)
        if have is None:
            gaps.append((label + " " + name, "<the whole table is gone>"))
            continue
        # forward: rule exists but has no Thai word
        gaps += [(label + " missing", n) for n in sorted(keys - have)]
        # reverse: Thai word exists but has no matching rule (orphaned)
        gaps += [(label + " orphaned", n) for n in sorted(have - keys)]
    return gaps


SEAM_PROBE = r"""
const PV={mode:'blocks',w:12,h:12,seq:null,seqMax:9,budget:64,win:12,over:[],
          bOrig:{x:0,y:0},origin:{wedge:0,ring:0},plane:{},cells:{}};
const bad=[];
// a plane with weight in every block: what the aggregation produces is checkable
for(let i=0;i<20736;i+=7) PV.plane[i]=(PV.plane[i]||0)+1;
pvDerive();
{
  const keys=Object.keys(PV.cells).map(Number);
  if(keys.length!==144) bad.push(['blocks counted',keys.length]);
  for(const k of keys){
    const col=k%144,row=Math.floor(k/144);
    if(col%12||row%12||col>=144||row>=144)
      bad.push(['a block is keyed by something that is not its first address',k,col,row]);
  }
}
// the glass asks for a CELL's value while the map holds block addresses, so a
// cell has to read the block around it or the glass draws a full map as empty
for(const b of Object.keys(PV.cells).map(Number)){
  for(const [dx,dy] of [[0,0],[5,5],[11,11]]){
    const cell=b+dx+dy*144;
    if(pvValAt(cell)!==PV.cells[b])
      bad.push(['a cell reads the wrong block',cell,b,pvValAt(cell),PV.cells[b]]);
  }
}
PV.mode='blocks';
for(const ox of [0,1,5,9,11]) for(const oy of [0,3,11]){
  PV.bOrig={x:ox,y:oy};
  for(let x=0;x<12;x++) for(let y=0;y<12;y++){
    const rk=pvRkeyAt(x,y);
    if(rk%144%12 || Math.floor(rk/144)%12) bad.push(['address-is-not-a-block-corner',x,y,rk]);
    const back=pvXY(rk);
    if(back[0]!==x||back[1]!==y) bad.push(['square-and-address-disagree',x,y,rk,back]);
    for(const [dx,dy] of [[5,5],[11,0],[0,11]]){
      const q=rk+dx+dy*144, inq=pvXY(q);
      if(inq[0]!==x||inq[1]!==y) bad.push(['cell-inside-the-block-lands-elsewhere',x,y,q,inq]);
    }
  }
}
console.log(JSON.stringify(bad.slice(0,6))+' :: '+bad.length);
"""


def _probe_count(out):
    """(prose, count) from a probe's last line — the COUNT is what decides.

    `.strip()` then `rpartition(" :: ")` was the old parser and it is wrong for
    an empty result: stripping " :: 0" removes the leading space, so the
    separator is gone and the whole line comes back as the tail — a probe that
    passed everything read as a total failure. It only ever worked because the
    other probes print something before the separator. Read the number.
    """
    line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    m = re.search(r"::\s*(\d+)\s*$", line)
    if not m:
        return line, None
    return line[:m.start()].rstrip(" :|"), int(m.group(1))


def seam_roundtrip(js):
    """Run the block map's address functions and check that they agree.

    A regex can only see that pvXY and pvRkeyAt both MENTION the seam; it cannot
    see that the arithmetic is right — and this arithmetic has already been wrong
    in a way that looked right: the 12x12 block map keyed its squares by a block
    INDEX while pvXY reads cell addresses, so all 144 blocks were drawn in row 0
    on top of one another while the edge lines around them were placed correctly.
    Nothing on screen says which square is which, so it read as "this map is
    confusing", never as a bug.

    So the functions are actually RUN: every square, at five seam offsets, with
    the cells inside each block checked too. The address a click resolves to must
    be a block's first cell, and every cell of that block must project back onto
    the square that produced it.
    """
    node = shutil.which("node")
    if not node:
        return ""
    pick = [js[js.find(a):js.find("\n", js.find(a))]
            for a in ("function pvWrap(v){", "function pvB(v){")]
    xy = block(js, "function pvXY(rk){", "\nfunction pvRkeyAt(x,y){").strip()
    at = block(js, "function pvRkeyAt(x,y){", "\n/* ── torus helpers").strip()
    agg = block(js, "function pvDerive(){", "\nfunction pvCenter(rk){").strip()
    val = block(js, "function pvValAt(rk){", "\nfunction pvCountAt(x,y,rk){").strip()
    if any(not p for p in pick + [xy, at, agg, val]):
        return ("pvWrap, pvB, pvXY, pvRkeyAt, pvDerive and pvValAt must all still "
                "be in the script: between them they are the only thing keeping "
                "the block map's picture, its clicks, its keys and its glass "
                "talking about the same square")
    d = tempfile.mkdtemp(prefix="ui-seam-")
    tmp = os.path.join(d, "seam.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(pick + [xy, at, agg, val]) + SEAM_PROBE)
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        return "the seam probe would not run: " + (r.stdout + r.stderr).strip()[:400]
    bad, n = _probe_count(r.stdout)
    if n != 0:
        return ("%s addresses read two different ways. Each line names what "
                "disagreed, so the same square is drawn through one expression "
                "and reported through another:\n    %s" % (n, bad[:600]))
    return ""


TORUS_PROBE = r"""
const T144=144;
const PV={mode:'plane',w:144,h:144,origin:null,bOrig:{x:0,y:0},win:24};
function pstep(){return 8}
const fails=[];
function near(a,b,eps){return Math.abs(a-b)<=eps}
function run(){
// plane: a seam-crossing pair 1 cell apart across the rim must be ~1 cell apart
let e=pvEdgeEnds(10*144+0,11*144+143);
let gap=(e[1][0]-e[0][0])/8;
if(!near(gap,-1,0.01))fails.push('plane seam pair gap='+gap+' (want -1: shortest path across the rim)');
// plane: a NORMAL pair 4 cells apart stays 4 cells apart
const e4=pvEdgeEnds(10*144+50,10*144+54);
if(!near((e4[1][0]-e4[0][0])/8,4,0.01))fails.push('plane normal pair gap='+((e4[1][0]-e4[0][0])/8));
// window: pair 4 cells apart stays 4; a pair that straddles the origin seam
// takes the short way (-2), never the long way (+142). PV.w/PV.h must follow the
// mode: the page's pvMode() sets it, and this probe has no pvMode.
PV.mode='window';PV.w=24;PV.h=24;PV.origin={wedge:100,ring:100};
const ew=pvEdgeEnds(100*144+100,100*144+104);
if(!near((ew[1][0]-ew[0][0])/8,4,0.01))fails.push('window normal pair gap='+((ew[1][0]-ew[0][0])/8));
const ws=pvEdgeEnds(100*144+102,100*144+((98)%144));
if(!near((ws[1][0]-ws[0][0])/8,-4,0.01))fails.push('window seam pair gap='+((ws[1][0]-ws[0][0])/8)+' (want -4)');
// window: an edge whose far end is outside the frame must still RETURN ends
// (canvas clips the line) as long as some part can touch the view
const eo=pvEdgeEnds(100*144+112,100*144+118);
if(!eo)fails.push('window: edge leaving the frame was dropped instead of clipped');
// blocks: block (0,0) -> block (11,0) is ONE block across the seam, on-screen
PV.mode='blocks';PV.w=12;PV.h=12;PV.origin=null;
const eb=pvEdgeEnds(0,11*12);
const bg=(eb[1][0]-eb[0][0])/8;   // in the blocks branch positions are BLOCK units x pstep
if(!near(bg,11,0.01))fails.push('blocks seam pair gap='+bg+' blocks (want 11: the visible rim image)');
if(eb[0][0]<0||eb[1][0]<0)fails.push('blocks: seam image drawn off-canvas');
// re-entry: on the full plane an edge that runs off one rim must be painted a
// second time entering from the opposite rim — the torus promise. Back to
// plane mode FIRST: the blocks branch above left PV.w at 12.
PV.mode='plane';PV.w=144;PV.h=144;
e=pvEdgeEnds(10*144+0,11*144+143);
const ox=(Math.max(e[0][0],e[1][0])>144*8)?-144*8:(Math.min(e[0][0],e[1][0])<0?144*8:0);
const oy=(Math.max(e[0][1],e[1][1])>144*8)?-144*8:(Math.min(e[0][1],e[1][1])<0?144*8:0);
if(!(ox||oy))fails.push('plane: seam-crossing edge produced no far-rim re-entry image');
const r1=[e[0][0]+ox,e[0][1]+oy],r2=[e[1][0]+ox,e[1][1]+oy];
const inside=(p)=>p[0]>=-8&&p[0]<=144*8+8&&p[1]>=-8&&p[1]<=144*8+8;
if(!inside(r1)&&!inside(r2))fails.push('plane: re-entry image did not land on the canvas');
}
try{run()}catch(err){fails.push('probe crashed: '+err.message)}
console.log(fails.length?fails.join('\n')+' :: '+fails.length:'OK :: 0');
"""


def torus_edges(js):
    """Run pvEdgeEnds under node and check the torus arithmetic.

    The old path dropped every edge whose endpoint fell outside the frame, so a
    line whose far end slipped past the seam simply vanished — it never came
    back on the other side, which is exactly what a reader expects the plane's
    wrap to do. This probe RUNS the function: a seam pair 1 cell apart must be
    1 cell apart on screen (the old pairing drew it 140 cells away), a pair
    leaving a window frame must be clipped rather than dropped, and the block
    map's seam image must land on the visible rim, not off-canvas.
    """
    node = shutil.which("node")
    if not node:
        return ""
    need = ["function pvEdgeEnds(", "function pvCenterRaw(", "function torusNear(",
            "function pvB(v){", "function torusDx(", "function pvXY(rk){",
            "function pvWrap(v){"]
    parts = []
    for marker in need:
        i = js.find(marker)
        if i < 0:
            return ("%s must still be in the script: it is part of the edge "
                    "placement that decides whether a seam-crossing line wraps, "
                    "clips or vanishes" % marker[:-2])
        j = js.find("\nfunction ", i + 1)
        j2 = js.find("\n/* ", i + 1)
        ends = [x for x in (j, j2) if x > 0]
        parts.append(js[i:min(ends) if ends else len(js)].rstrip())
    d = tempfile.mkdtemp(prefix="ui-torus-")
    tmp = os.path.join(d, "torus.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n" + TORUS_PROBE)
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        return "the torus probe would not run: " + (r.stdout + r.stderr).strip()[:400]
    bad, n = _probe_count(r.stdout)
    if n != 0:
        return ("%s edge placements disagree with the torus the map draws: "
                "a line crossing the rim must take the shortest path and "
                "re-enter on the far side of the FULL plane\n    %s" % (n, bad[:600]))
    return ""


def frame_sizes(js, html):
    """(the pinch ladder, the frame picker's options) — two lists that must match.

    One control in two places: the pinch steps PV_WINS, the picker offers the
    <option>s. A step onto a size the picker does not offer leaves the reader in
    a frame they cannot choose again — and the picker then displays a stale
    number, which is a control that lies about the view.
    """
    m = re.search(r"PV_WINS\s*=\s*\[([^\]]*)\]", js)
    a = [int(v) for v in re.findall(r"\d+", m.group(1))] if m else None
    sel = re.search(r'id="pv-win"[^>]*>(.*?)</select>', html, re.S)
    b = ([int(v) for v in re.findall(r'<option value="(\d+)"', sel.group(1))]
         if sel else None)
    return a, b


# (label, test, why) — the why is printed on failure, because the point of the
# check is to hand back the reasoning, not just a red X. Each test gets the whole
# script; each is written to fail loudly if its marker disappears entirely, so a
# rename can never make a rule quietly stop applying. A `why` may be a callable,
# which is how a check that finds several things reports WHICH ones.
INVARIANTS = [
    ("composer keeps the draft on failure",
     lambda js, html: re.search(r"if\s*\(\s*r\.ok\s*\)\s*\{\s*el\.value\s*=\s*''", js),
     "send() must clear the textarea only INSIDE the if(r.ok) branch — clearing "
     "it first means any failed append erases what someone typed"),
    ("composer reports a failure",
     lambda js, html: re.search(r"ส่งไม่ถึงเซิร์ฟเวอร์", js),
     "send() needs a catch that says something; without it a rejected fetch is "
     "an unhandled rejection and the UI stays silent"),
    # a STATEMENT, not the word: the prose in that very function explains why
    # `listen()` is there, so a plain substring test was satisfied by the
    # comment and passed on code with the call deleted. Line-anchored and
    # semicolon-terminated, a comment cannot satisfy it.
    ("identity switch rebuilds the stream",
     lambda js, html: re.search(r"(?m)^\s*listen\s*\(\s*\)\s*;",
                                block(js, "window.setIdentity=", "async function loadInbox")),
     "setIdentity must call listen(), or the identity the header shows and the "
     "as= filter the stream carries disagree — the feed then shows lines the "
     "view is supposed to hide"),
    ("identity picker is built from identity",
     lambda js, html: re.search(r"isel\.value\s*=\s*identity", js),
     "#id-sel must be populated from `identity`, not from the loaded window: "
     "the window has no lines for an identity that has not spoken, so the "
     "picker falls back to displaying admin while the page is filtered as "
     "someone else"),
    # ── the plane: both of these fail INVISIBLY (the picture still looks right)
    ("popup and tooltip are in the top layer",
     lambda js, html: html.count('popover="manual"') >= 2,
     "#plane-pop and #plane-tip must both be popover elements. Their container "
     "is overflow:hidden on a phone and only ~165px tall, and a popup taller "
     "than its container cannot be clamped into it (measured: a 164px popup in "
     "a 165px box, bottom row of buttons cut off)"),
    ("backing store is only reallocated when the size changes",
     lambda js, html: (re.search(r"if\s*\(\s*pcv\.width\s*!==\s*bw", js)
                       and not re.search(r"pcv\.width\s*=\s*px", js)),
     "assigning canvas.width reallocates the backing store AND clears it, so an "
     "unguarded assignment is a full surface allocation per frame — measured at "
     "30 assignments for 30 draws, on a path where a zoom frame repaints the "
     "whole map"),
    ("the popup is clamped to the viewport, not the clipping box",
     lambda js, html: re.search(r"vw\s*-\s*pw\s*-\s*10", js),
     "pvPop must clamp against the viewport. Clamping to #plane-canvaswrap is "
     "what guaranteed the cut-off, and the failure is invisible on a wide "
     "screen — it only shows on the phone breakpoint"),
    # ── the modal's chrome, which is where "controls piled on controls" lives
    ("the header keeps a short form and a full tooltip",
     lambda js, html: re.search(r"info\.title\s*=\s*full", js),
     "#plane-info must write a SHORT string to the screen and the whole thing "
     "to its title. It was a single 166-character line, which on a 709px screen "
     "took a row of its own — the header measured 108px, three rows, for a "
     "caption. On a wide screen the long form fits, so this regresses silently"),
    ("the drawing options start behind their button",
     lambda js, html: re.search(r'id="plane-bar"\s+class="off"', html),
     "the options bar must start hidden: 14 controls in a wrapping row measured "
     "115px, above a canvas that then had 175px left, and it held the scrubber "
     "that belonged with the strip. Total chrome was 272px of a 709px screen "
     "(38%); with the options collapsed it is 121px (17%)"),
    # ── the words, which fail invisibly in the same way
    ("no internal vocabulary on screen",
     lambda js, html: not vocabulary_on_screen(html),
     lambda js, html: ("these words are the name the wire and the code use, not "
                       "a name a reader has; the screen has a plainer one:\n    "
                       + "\n    ".join("%-16s in: %s" % (t, s[:96])
                                     for t, s in vocabulary_on_screen(html)[:8]))),
    ("server errors are translated, not printed",
     lambda js, html: not error_leaks(js),
     lambda js, html: ("the server's `error` field may only be READ inside "
                       "sayError()/sayRaw(); everywhere else it must come out "
                       "as a Thai sentence with the original wording in the "
                       "toast's tooltip. This is the exact shape that shipped: "
                       "`toast('ยังไม่ส่ง: '+r.error)` — an English string and an "
                       "HTTP code, shown to someone who can act on neither.\n    "
                       + "\n    ".join(error_leaks(js)[:6]))),
    ("every filter rule has a Thai word and every Thai word has a rule",
     lambda js, html: not vocabulary_gaps(js),
     lambda js, html: ("a rule the server can fire has no word in the dashboard "
                       "(CODE NAME on screen), or a Thai word exists for a rule "
                       "that was deleted (orphaned translation). Fix RULE_TH / "
                       "PROTECT_TH:\n    "
                       + "\n    ".join("%s: %s" % (l, n) for l, n in vocabulary_gaps(js)))),
    # ── the gestures: invisible on a desktop, where there is no second finger
    ("the pinch steps the sizes the frame picker offers",
     lambda js, html: frame_sizes(js, html)[0] == frame_sizes(js, html)[1]
                      and frame_sizes(js, html)[0] is not None,
     lambda js, html: ("the pinch ladder and #pv-win are one control in two "
                       "places: ladder=%s picker=%s — a step onto a size the "
                       "picker cannot show is a frame that cannot be chosen back"
                       % frame_sizes(js, html))),
    ("a two-finger gesture is not a drag or a tap",
     lambda js, html: all(k in block(js, "pcv.onpointerdown=ev=>{",
                                     "const rect=pcv.getBoundingClientRect()")
                         for k in ("PV.panDrag=null", "PV.panSwallow=Date.now()")),
     "the second finger must drop the drag the first one started (one pointer "
     "that is both panning and pinching moves the frame twice per frame) and "
     "must swallow the click (lifting two fingers would otherwise open whatever "
     "cell happened to be under them). Both failures look like 'the map jumped' "
     "— never like a missing line of code, and never on a desktop"),
    # ── the tour: a guide that reappears is a nag, and one that cannot be
    # dismissed is a trap — both fail invisibly on a machine that has already
    # used the app once
    ("the tour is shown once, then never again",
     lambda js, html: all(re.search(p, js) for p in (
         r"TOUR_KEY\s*=\s*'pool_tour_done'",
         r"localStorage\.getItem\(TOUR_KEY\)",
         r"localStorage\.setItem\(TOUR_KEY",
         r"if\s*\(\s*!\s*localStorage\.getItem\(TOUR_KEY\)\s*\)")),
     "the three-step guide must read pool_tour_done from localStorage before "
     "showing and write it on dismiss. Without the read, every refresh walks a "
     "returning reader through the basics again; without the write, there is no "
     "way to ever be done with it"),
    ("the tour advances on the act, not only the Next button",
     lambda js, html: re.search(r"function tourAdvance", js) and
                      re.search(r"tourFlags\.searched", js) and
                      re.search(r"tourFlags\.map", js) and
                      re.search(r"tourFlags\.pan", js),
     "each step must have a `when()` watching the real action (a search typed, "
     "the map opened, a pan performed). A tour that only moves on its own Next "
     "button is a slideshow: it tells the reader about the search box without "
     "ever noticing they used it"),
    ("the tour has a way out that never returns",
     lambda js, html: re.search(r"id=\"tour-skip\"", html) and
                      re.search(r"\$\('#tour-skip'\)\.onclick", js),
     "ข้าม must exist in the markup AND be wired — a dismiss link that renders "
     "but does nothing leaves the guide over the feed with no escape"),
    ("the tour never owns the screen it is teaching",
     lambda js, html: re.search(r"#tour\{[^}]*pointer-events:none", html) and
                      re.search(r"#tour\[hidden\]\{display:none\}", html) and
                      not re.search(r"#tour\{[^}]*background:rgba", html),
     "the guide is a CARD, not a scrim. Shipped once as a full-screen backdrop: "
     "hidden did nothing (display:flex beat it) and the backdrop swallowed "
     "every click, so the acts the tour waits for were unreachable and the "
     "whole page felt dead — กดอะไรไม่ได้. pointer-events:none on the shell, "
     "auto only on the card; #tour[hidden] must beat display:flex"),
    ("a held glass moves alone — the frame never follows, during or after",
     lambda js, html: ("pvLensFollow()" not in block(js, "pcv.onpointermove=ev=>{",
                                                      "if(!PV.panDrag)return;")
                       and re.search(r"if\s*\(\s*PV\.lens\.drag\s*\)\s*return", js)
                       and "pvLensReclaim" not in js
                       and "pvLensFollow" not in block(js, "pcv.onpointerup=ev=>{",
                                                       "if(!PV.panDrag)return;")),
     "two measured ways the viewport fought the hand: (1) pvLensFollow ran on "
     "every pointermove of a glass drag — hand moves the glass, edge band moves "
     "the frame, paint between them flickers and snaps back; (2) a post-drag "
     "reclaim slid the frame once on release — the same jump, one frame later. "
     "Both are frame movement the reader did not ask for. The plane is a torus: "
     "a glass near a rim re-enters from the far side via pvWrap, so the frame "
     "never needs to move for the glass's sake — not while held, not after"),
    ("the glass drag is never rim-clamped",
     lambda js, html: "Math.max(lo,Math.min(hi" not in block(js, "pcv.onpointermove=ev=>{",
                                                      "if(!PV.panDrag)return;"),
     "a rim clamp on the drag was measured SNAPPING the glass 51px against the "
     "hand the moment it crossed the rim zone (x=16 dragged 30px left landed at "
     "x=67). A straight-line clamp is wrong on a torus anyway — the honest "
     "motion is pvWrap, re-entering from the far rim, 1:1 with the hand"),
    ("the zoom dial is a small vertical column",
     lambda js, html: re.search(r"#pv-zoom\{[^}]*flex-direction:column", html)
                      and re.search(r"#pv-zoom button\{width:30px", html)
                      and not re.search(r"#pv-zoom\{flex-direction:row", html),
     "the dial used to grow to 46px buttons and, on a phone, lie down into a "
     "row beside the very canvas it zoomed — chrome crowding the thing it "
     "controls. A compact always-vertical column reads as the up/down ladder "
     "it is, and stays clear of the map"),
    ("a pan redraws lightly and rides the hand between whole cells",
     lambda js, html: (not re.search(r"pvMode\(\);", block(js, "if(!PV.panDrag)return;",
                                                      "/* The map moves in WHOLE CELLS"))
                       and "function pvPanRide(" in js
                       and "pvPanRide(d,ev,dw,dr,step);" in block(js, "if(!PV.panDrag)return;",
                                                                 "/* The map moves in WHOLE CELLS")
                       and all("pvPanRideClear()" in block(js, a, b)
                               for a, b in (("pcv.onpointerdown=ev=>{", "pvPts.size>=2"),
                                            ("pcv.onpointerup=ev=>{", "if(PV.lens.on&&PV.lens.drag)"),
                                            ("pcv.onpointercancel=ev=>{", "};")))),
     "two measured pan defects: (1) every whole-cell step ran pvMode() — full "
     "re-derive + base repaint + a recount of all 25k timeline rows, 11.5ms per "
     "step, the twitch of a dragged map; (2) no between-cell motion — a hand "
     "hovering near a cell boundary flipped dw between n and n+1 with every 1px "
     "wiggle, so the map JUMPED A WHOLE CELL back and forth (ลากแล้วเด้งไปมา). "
     "The pan must take the light path (pvDraw+pvInfo+pvPop, no pvMode) and "
     "paint the pixel remainder via the pvPanRide CSS translate, cleared on "
     "down/up/cancel/blur so a new drag always starts its ride from zero"),
    ("replay scales its step, pauses where it is, restarts only at the live edge",
     lambda js, html: (re.search(r"pvPlayStep\s*=\s*Math\.max\(1,\s*Math\.round\(PV\.seqMax\s*/\s*\(PV_PLAY_TARGET/PV_PLAY_MS\)\)\)", js)
                       and re.search(r"const at=PV\.seq===null\?0:PV\.seq;", js)
                       and re.search(r"\(at>=PV\.seqMax-1\)\s*\?\s*0\s*:\s*at", js)
                       and not re.search(r"textContent='⏸ หยุด';\s*PV\.seq=0", js, re.S)),
     "two measured defects in ▶ เล่นย้อน: (1) the timer squeezed the INTERVAL "
     "to fit 2.4s but hit its 60ms floor, so a 25k-line log crawled one line "
     "per 60ms — a 25-MINUTE replay; the step SIZE must scale instead "
     "(PV_PLAY_TARGET ms total). (2) every press reset PV.seq=0 — pausing a "
     "replay at line 20k and pressing again threw the reader back to the "
     "start. ⏸/▶ is a PAUSE: resume continues from the line on screen; only "
     "a replay that reached the live edge (or a view parked there) starts over"),
    ("edges cross the seam by the shortest path and re-enter on the far rim",
     lambda js, html: torus_edges(js) == ""
                       and "[a[0]+ox,a[1]+oy],[z[0]+ox,z[1]+oy]" in js,
     lambda js, html: torus_edges(js) or
                      ("the seam re-entry push is gone from pvEdgeStrokesOf — a "
                       "line that runs off the full plane's rim is no longer "
                       "painted a second time entering from the opposite rim")
                      if "[a[0]+ox,a[1]+oy],[z[0]+ox,z[1]+oy]" not in js
                      else torus_edges(js)),
    ("the block map speaks one address for its keys, picture, clicks and glass",
     lambda js, html: seam_roundtrip(js) == "",
     lambda js, html: seam_roundtrip(js)),
    ("the popup only opens on a deliberate read",
     lambda js, html: (re.search(r"if\s*\(!PV\.popWant&&!pop\.matches\(':popover-open'\)\)return", js)
                       and "function pvPopRide(d,ev){" in js
                       and "d.ppx+(ev.clientX-d.x)" in js
                       and "d.ppy+(ev.clientY-d.y)" in js
                       and "if(anchored)pvPop();" not in js
                       and "pvDraw();pvInfo();pvPop();pvLensInfo();" not in js
                       and "if($('#plane-pop').matches(':popover-open'))pvPop();" in block(js, "pcv.onpointerup=ev=>{", "pcv.onpointercancel")
                       and js.count("PV.popWant=true") == 1
                       and "PV.popWant=true;" in block(js, "async function openPoint(rk,path){",
                                                              "// The shape of a node")),
     "the popup is a read's answer, not a refresh's side effect: without the "
     "PV.popWant gate every pan, resize and refetch re-opened #plane-pop over "
     "the map the reader was still looking at (ชอบขึ้นมาบัง). Only openPoint "
     "— a successful cell read — may set PV.popWant=true; and during a pan "
     "the open popup must RIDE the hand by raw delta (pvPopRide), never be "
     "rebuilt per move — a rebuild wipes its buttons, loses its scroll and "
     "jumps whole cells while the canvas glides"),
    ("every kind the server writes has a plain word, and none is the wire name",
     lambda js, html: not kind_words(js),
     lambda js, html: "every kind on screen needs a plain word, and no word may be "
                      "the wire name:\n    "
                      + "; ".join("%s: %s" % g for g in kind_words(js))
                      + "\n  The kind names ride the wire (`kind=\"bond\"`) and the "
                        "words live in the dashboard's KIND_TH — two files, so "
                        "only a check that reads both can see them disagree. "
                        "`bond` and `pointer` are already ruled off the screen "
                        "by the vocabulary rule above, so an untranslated entry "
                        "puts a banned word back into the legend"),
    ("a kind is read by value, never by spelling",
     lambda js, html: (re.search(r"const kindIsChat=k=>String\(k\|\|'chat'\)", js)
                       and "kindIsChat(kind)?''" in js
                       # exactly ONE literal compare against 'chat' may exist in
                       # the whole script, and it is inside kindIsChat itself. A
                       # second one is a branch that quietly matches nothing.
                       and len(_code_matches(js, r"(?<![!<>=])===\s*'chat'")) == 1),
     "the corpus writes CHAT in caps and bond/pointer/block in lower, so a "
     "`kind==='chat'` compare matched none of the 13,746 chat lines — every one "
     "of them grew a badge reading \"CHAT\" on the feed, the one wire name the "
     "screen has a plain word for. Same defect §6 already paid for in "
     "curator_protect(): a kind is a value, never a spelling"),
    ("the kind filter may show less, never more",
     lambda js, html: kind_filter(js) == "",
     lambda js, html: kind_filter(js)),
    ("a filter on the time scrub is the same filter as on the live plane",
     lambda js, html: re.search(r"if\s*\(\s*ki\s*&&\s*row\[3\]\s*!==\s*ki\s*\)\s*continue;", js),
     "pvCellsUpTo counts the log's own order, so without the kind check it "
     "would rebuild a plane the filter is not describing — drag time back with "
     "'bonds only' on and the filtered kinds come straight back"),
    ("a cell count never stands in for a mode",
     lambda js, html: (not _code_matches(js, r"===\s*144")
                       and re.search(r"PV\.mode\s*===\s*'plane'\s*\?\s*\[2,8\]", js)),
     "pstep read its pitch range off the cell count (w===144&&h===144 picks "
     "2..8 instead of 8..84), so the plane's rule was really 'if there are many "
     "cells, make "
     "them tiny'. Any mode that was not 144 wide inherited the plane's range by "
     "accident, and any future mode that WAS 144 wide would have been told it "
     "was the plane. How large a cell may be drawn is a property of the mode; "
     "the mode is a name, and this is the only place that was guessing it from "
     "a number. pvCenter re-tested the count after already testing the mode — "
     "harmless today and wrong the day a plane mode is not 144 wide"),
    ("the plane's grid names both of its sides",
     lambda js, html: (not _code_matches(js, r"\bPV\.n\b")
                       and len(_code_matches(js, r"PV\.w\s*=")) == len(_code_matches(js, r"PV\.h\s*="))),
     "one PV.n stood for 'the cell count' on both axes at once, which held only "
     "because every mode happens to be square — so pstep inferred the mode from "
     "it (n===144), and a future non-square mode would have inherited a "
     "coincidence instead of a contract. PV.w/PV.h make it explicit. The guard "
     "is the assignment count: the two are always set as a pair, so a new mode "
     "that sets one and forgets the other fails here. Read through _code_matches "
     "because pstep's own comment explains the old name — a plain regex is "
     "satisfied by the sentence documenting the fix, which is §13.5 for the "
     "fifth time in this file"),
    ("the kind list is built, not only computed",
     lambda js, html: kind_list(js) == "",
     lambda js, html: kind_list(js)),
    ("a control that needs a layer can ask for it",
     lambda js, html: ("__load" in js
                       and re.search(r"if\s*\(\s*val\s*===\s*'__load'\s*\)", js)
                       and not re.search(r"sel\.disabled\s*=\s*!ks", js)
                       and not re.search(r"sel\.disabled\s*=\s*!as", js)),
     "#pv-kind shipped DISABLED with a single option until the meta layer was "
     "loaded — so it could never fire the change handler that loads the very "
     "layer it needs, and the only way in was to first pick a meta-backed "
     "COLOUR, a control the reader has no reason to touch. A disabled control "
     "cannot ask for what it is missing; both filter controls offer to, and say "
     "what it costs"),
    ("a filter that matches nothing says so",
     lambda js, html: (re.search(r"' — ยังไม่มีข้อมูล'\+pvFiltWord\(\)\+'ในคลัง'", js)
                       and re.search(r"' — ยังไม่มีข้อมูล'\+pvFiltWord\(\)\+'ในมุมมองนี้'", js)
                       and re.search(r"const filt=PV\.filt", js)),
     "a filter naming a kind or a writer with no cells here must be reported as "
     "empty. Falling back to the unfiltered map would draw the whole plane "
     "under a filter that claims to be on — the one answer this layer must "
     "never give. The sentence is now built from pvFiltWord() so the WRITER "
     "axis says the same thing in its own words instead of claiming a kind"),
    ("the filter is one axis, not two",
     lambda js, html: (not _code_matches(js, r"PV\.(kindOnly|appOnly)")
                       and len(_code_matches(js, r"PV\.filt\s*=")) == 1
                       and re.search(r"PV\.filt=val\?\{dim:dim,key:val\}:null", js)),
     "two <select>s writing two fields is how a reader ends up looking at "
     "'ทุกชนิด' AND 'ทุกผู้เขียน' while a live filter is dimming half the map. "
     "It is one field with a dimension because 'bond AND chatdrop' is an "
     "intersection NEITHER by_kind nor by_app can answer — the lines that are "
     "both live in a third map nobody has kept — and two fields would have "
     "printed a number the plane cannot compute. So choosing one axis "
     "REPLACES the other, and both controls are a view of PV.filt"),
    ("the writer axis is a partition like the kind axis",
     lambda js, html: (re.search(r"PV\.apps=m\.by_app\|\|\{\}", js)
                       and _js_function(js, "pvAppOptions")
                       and re.search(r"nLit=a=>PV\.apps&&PV\.apps\[a\]", js)),
     "by_app is the same fold as by_kind with a different key, so the writer "
     "filter has to owe the same promises: cells that HOLD this writer, "
     "counted from by_app and not borrowed from the colour layer "
     "(cellApp, which is who wrote there LAST — the mistake that shipped as "
     "bug 5 for kinds). A control built over the wrong map shows a number the "
     "filter does not light"),
    ("the scrub honours the writer axis too",
     lambda js, html: (re.search(r"if\(ap&&row\[5\]!==ap\)continue;", js)
                       and re.search(r"ap=f&&f\.dim==='app'\?f\.key:null", js)),
     "pvCellsUpTo walks the log's own order, so it has to apply whatever axis "
     "the live plane is applying. A scrub that honoured the kind axis but not "
     "the writer axis would repaint an unfiltered plane under a filter that "
     "claims to be on — bug 6 for kinds, reproduced by adding a second axis "
     "and forgetting one of the two comparisons. The column is 5, not 7: the "
     "fold's row has eight entries and the WIRE row has six, and row[7] reads "
     "undefined here, which compares unequal to everything and lights nothing"),
    ("the filter's figures describe the frame on screen, not the whole log",
     lambda js, html: (re.search(r"scrubbed\?Object\.keys\(src\):Object\.keys\(src\)\.filter\(k=>f\[k\]\)", js)
                       and re.search(r"scrubbed\?Object\.values\(src\)", js)),
     "the status line counted the filter over PV.kinds, which is the WHOLE log. "
     "Correct on the live plane, wrong the moment time is scrubbed: the picture "
     "fell to 1,421 bond lines and the line under it still said 13,572. The "
     "FIRST fix then over-corrected the other way — it intersected src with the "
     "whole-log f and summed f[k], but while scrubbing pvCellsUpTo has already "
     "applied the filter, so src IS the answer and summing f[k] over it counts "
     "bonds that have not happened yet (7,938 against a picture holding 1,421). "
     "`src` means two different things in the two frames, so it takes two "
     "branches, not one rule. Both must stay ARRAYS: the second version made the "
     "scrubbed branch `lit=src` — an object — and then asked `lit.length`, which "
     "is undefined, so the one frame the fix existed for reported itself EMPTY"),
    ("a blocks drag keeps the pointerdown anchor",
     lambda js, html: ("PV.bOrig={x:pvB(d.bx+dw),y:pvB(d.by+dr)};" in js
                       and "bx:" not in block(js, "if(d.blocks){", "}else{")
                       and "PV.panDrag={...d};" in block(js, "if(d.blocks){", "}else{")),
     "the blocks branch wrote the just-moved origin back into the drag anchor "
     "while still measuring dw from the original pointerdown, so every step "
     "added the absolute delta onto an already-shifted base — a hand that had "
     "moved +2 blocks slid the map +3, then further with every step "
     "(ลากแล้วเด้ง). The anchor must stay the pointerdown's own bx/by; only "
     "bOrig moves"),
]


KIND_PROBE_HEAD = r"""
// Stubs only. The four functions under test are EXTRACTED from the served
// script below — a probe carrying its own copies of them would pass while the
// page was broken, which is the one thing §13.5 says a test must not do.
const PV={filt:null,kinds:{},apps:{},cells:{a:5,b:2,c:9}};
function pvCellInk(rk,rel){return 'INK:'+rk+':'+rel}
const esc=s=>s;
"""

KIND_PROBE_TAIL = r"""
const bad=[];
// 1. no filter: the picture is the plane, and no cell claims a filtered count
if(pvFilt()!==null)bad.push('an unfiltered plane returned a filter');
if(pvFiltN('a')!==0)bad.push('an unfiltered plane reported a filtered count');
if(pvDrawMax()!==9)bad.push('unfiltered ramp ceiling = '+pvDrawMax()+' (want the busiest cell = 9)');
// 2. filter on: the ramp ceiling moves to the BUSIEST CELL OF THE FILTER, or the
//    filter tints the map without shifting a single shade
PV.kinds={bond:{a:5,b:2}};
PV.filt={dim:'kind',key:'bond'};
if(pvDrawMax()!==5)bad.push('filtered ramp ceiling = '+pvDrawMax()+' (want 5, the busiest bond cell)');
if(pvFiltN('a')!==5||pvFiltN('b')!==2)bad.push('filtered counts are wrong');
if(pvFiltN('c')!==0)bad.push('a cell holding none of the kind must count 0, not the total');
// 3. THE contract: a filtered count can never exceed the total. A picture that
//    shows more of one kind than the plane holds is worse than no filter at all
for(const rk in PV.cells){
  if(pvFiltN(rk)>PV.cells[rk])
    bad.push('cell '+rk+' claims '+pvFiltN(rk)+' of a kind it holds only '+PV.cells[rk]);
}
// 4. filtered-out cells are drawn in the neutral grey, never a kind hue: a hue
//    there would look like a seventh kind in the legend
if(pvInkAt('c',0)!=='rgba(139,148,158,0.13)')bad.push('a filtered-out cell was painted as if it matched');
if(pvInkAt('a',1)!=='INK:a:1')bad.push('a matching cell was greyed out');
// 5. a filter naming a kind that is ABSENT stays absent - it never silently
//    becomes "no filter", which would draw the unfiltered map under a filter
//    that claims to be on
PV.filt={dim:'kind',key:'nosuchkind'};
if(pvFilt()!==null)bad.push('an absent kind produced a filter');
// 6. the WRITER axis runs through the same four functions. It is a partition of
//    the same plane, so it owes the same contract — and if it did NOT share the
//    code, "cells holding none of this app" would quietly mean something else
PV.kinds={};PV.apps={chatdrop:{a:5,b:1},index:{a:3,c:9}};
PV.filt={dim:'app',key:'chatdrop'};
if(pvFilt()!==PV.apps.chatdrop)bad.push('the writer axis did not read the writer map');
if(pvDrawMax()!==5)bad.push('writer ramp ceiling = '+pvDrawMax()+' (want 5, the busiest chatdrop cell)');
if(pvFiltN('a')!==5||pvFiltN('b')!==1||pvFiltN('c')!==0)bad.push('writer counts are wrong');
if(pvInkAt('c',1)!=='rgba(139,148,158,0.13)')bad.push('a cell no writer wrote in was not greyed out');
for(const rk in PV.cells){
  if(pvFiltN(rk)>PV.cells[rk])
    bad.push('cell '+rk+' claims '+pvFiltN(rk)+' lines of a writer it holds only '+PV.cells[rk]);
}
PV.filt={dim:'app',key:'nosuchapp'};
if(pvFilt()!==null)bad.push('an absent writer produced a filter');
PV.filt=null;
if(pvFilt()!==null)bad.push('clearing the filter left one behind');
console.log(bad.join(' | ')+' :: '+bad.length);
"""


def _js_function(js, name):
    """The source of one `function name(...)` from the served script.

    Same slicing as torus_edges(): from the declaration to whatever comes next
    at top level. Used here because a probe that carries its own COPY of the
    code under test is a probe of the copy.
    """
    marker = "function %s(" % name
    i = js.find(marker)
    if i < 0:
        return ""
    nl = chr(10)
    ends = [x for x in (js.find(nl + "function ", i + 1),
                        js.find(nl + "const ", i + 1),
                        js.find(nl + "/* ", i + 1)) if x > 0]
    return js[i:min(ends) if ends else len(js)].rstrip().rstrip(";")





def kind_filter(js):
    """Run the filter's REAL functions in node and check what they promise.

    The filter has one contract a reader can be lied to about: the picture may
    show less than the plane holds, never more, and the number printed stays the
    total. A regex cannot see `pvKindN(rk) > PV.cells[rk]`, and that is the one
    comparison that would let the map claim 12 bonds in a cell holding 3 — so
    the functions are lifted out of the served script and run, like the block-map
    seam. The FIRST version of this probe pasted its own copies of the four
    functions, and three mutations of the real code passed it: a test that
    cannot fail is not a test, and neither can one that tests a copy.
    """
    node = shutil.which("node")
    if not node:
        return ""
    parts = [_js_function(js, n) for n in ("pvFilt", "pvFiltN", "pvInkAt", "pvDrawMax")]
    gone = [n for n, src in zip(("pvFilt", "pvFiltN", "pvInkAt", "pvDrawMax"), parts)
            if not src]
    if gone:
        return ("%s must still be in the script: it is the only thing deciding "
                "what the picture counts when a kind filter is on" % ", ".join(gone))
    d = tempfile.mkdtemp(prefix="ui-kind-")
    tmp = os.path.join(d, "kind.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(chr(10).join([KIND_PROBE_HEAD] + parts + [KIND_PROBE_TAIL]))
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        return "the kind-filter probe would not run: " + (r.stdout + r.stderr).strip()[:400]
    bad, n = _probe_count(r.stdout)
    if n != 0:
        return ("the filter's numbers do not hold (%s). Each clause names what " % n
                + "disagreed:" + chr(10) + "    " + bad[:600])
    return ""


LIST_PROBE_HEAD = r"""
// The four maths functions can all be right and the filter still never open:
// what broke was the LIST, and a probe that only computes cannot see a list.
const PV={filt:null,kinds:{},apps:{},cellKind:{}};
const mk=()=>({value:'',disabled:false,options:[],_kindHtml:null,_appHtml:null,_sets:0,set innerHTML(h){
  this._sets++;
  this.options=h.split('</option>').filter(Boolean).map(s=>{
    const v=/value="([^"]*)"/.exec(s);const t=/>([^<]*)</.exec(s);
    return {value:v?v[1]:'',text:t?t[1]:''};});}});
const sel=mk(),selApp=mk();
// note the '#': the real $ receives it WITH the hash. A stub that compares
// against 'pv-kind' returns null, the real code bails at `if(!sel)return`, and
// the probe passes nothing while reporting every clause as broken — a check
// that cannot see its own harness is worse than no check.
const $=(id)=>id==='#pv-kind'?sel:(id==='#pv-app'?selApp:null);
const esc=s=>s;
const kindLabel=k=>'K:'+k;
const appLabel=a=>'A:'+a;
"""
LIST_PROBE_TAIL = r"""
const bad=[];
const run=()=>{try{pvKindOptions();return null}catch(e){return String(e)}};
const runApp=()=>{try{pvAppOptions();return null}catch(e){return String(e)}};
// 1 the layer is missing: the control must ASK for it, not sit there disabled
//    with one option. A disabled <select> cannot fire the handler that loads
//    the very layer it needs, so the filter is unreachable forever.
PV.cellKind={};PV.kinds={};
sel.innerHTML='<option value="">ทุกชนิด</option>';
sel.disabled=true;
const e0=run();
if(e0)bad.push('building the empty prompt threw: '+e0);
if(sel.disabled)bad.push('the unloaded filter is disabled, so it can never be opened');
if(sel.options.length<2)bad.push('the unloaded filter offers nothing to choose');
if(!sel.options.some(o=>o.value==='__load'))bad.push('nothing in the list asks for the layer');
// 2 the layer arrived: the list must ACTUALLY be built. pvTopKinds returns a
//    COUNT OBJECT, so `ks.join('|')` is undefined and the guard line threw a
//    TypeError on every path that had kinds to show — the filter was never
//    openable and every check stayed green because they all do arithmetic.
//    cellKind and kinds are given DIFFERENT numbers on purpose: the colour layer
//    counts cells whose LAST line was the kind, the filter counts cells that
//    HOLD one, and putting the wrong one in the list puts two different numbers
//    on screen about a single click.
PV.cellKind={a:'CHAT',b:'CHAT',c:'bond'};
PV.kinds={CHAT:{a:1,b:1},bond:{a:1,b:1,c:1,d:1,e:1}};
sel.innerHTML='<option value="">ทุกชนิด</option>';
sel.disabled=true;
const e=run();
if(e)bad.push('building the list THREW once the kinds were known: '+e);
const vals=sel.options.map(o=>o.value);
if(vals.length!==3)bad.push('expected ทุกชนิด + 2 kinds, got '+vals.length+' ('+vals.join(',')+')');
if(vals.indexOf('CHAT')<0||vals.indexOf('bond')<0)bad.push('the kinds never made it into the list: '+vals.join(','));
if(sel.disabled)bad.push('the loaded filter is still disabled');
// 2b the NUMBERS beside each kind must be the ones the filter will LIGHT UP
if(!/bond \(5\)/.test(sel._kindHtml||''))bad.push('the list must promise bond lights 5 cells, got '+String(sel._kindHtml).match(/bond \([0-9]+\)/));
if(!/CHAT \(2\)/.test(sel._kindHtml||''))bad.push('the list must promise CHAT lights 2 cells, got '+String(sel._kindHtml).match(/CHAT \([0-9]+\)/));
if(/bond \(1\)/.test(sel._kindHtml||''))bad.push('the list is quoting the COLOUR LAYER count (last kind) — that is a different number from what the filter lights');
// 2c and sorted by that same promise: bond 5 must come before CHAT 2 even
//     though the colour layer says CHAT is the bigger mass
if(vals[1]!=='bond')bad.push('the kind that lights the most cells must come first, got '+vals[1]);
// 3 no churn: this runs on every mode change, so an unchanged list must not be
//    rebuilt — rebuilding resets the open dropdown and eats the reader's click
const sets=sel._sets;pvKindOptions();
if(sel._sets!==sets)bad.push('an unchanged list was rebuilt ('+sets+' -> '+sel._sets+')');
// 4 a filter that has gone (or an absent kind) must not leave the control
//    showing a value that is no longer in it
PV.filt={dim:'kind',key:'nosuchkind'};pvKindOptions();
if(sel.value!=='')bad.push('a filter with no cells left the control on "'+sel.value+'"');
PV.filt={dim:'kind',key:'bond'};pvKindOptions();
if(sel.value!=='bond')bad.push('a live filter was not restored, got "'+sel.value+'"');
// 5 the WRITER list, which is the same contract over a different partition
PV.apps={};selApp.innerHTML='<option value="">ทุกผู้เขียน</option>';selApp.disabled=true;
let eA=runApp();
if(eA)bad.push('building the empty writer prompt threw: '+eA);
if(selApp.disabled)bad.push('the unloaded writer filter is disabled, so it can never be opened');
if(!selApp.options.some(o=>o.value==='__load'))bad.push('nothing in the writer list asks for the layer');
PV.apps={chatdrop:{a:1,b:1,c:1},index:{a:1,b:1,c:1,d:1,e:1}};
selApp.innerHTML='<option value="">ทุกผู้เขียน</option>';selApp.disabled=true;
eA=runApp();
if(eA)bad.push('building the writer list THREW once the writers were known: '+eA);
const av=selApp.options.map(o=>o.value);
if(av.length!==3)bad.push('expected ทุกผู้เขียน + 2 writers, got '+av.length+' ('+av.join(',')+')');
if(av.indexOf('chatdrop')<0||av.indexOf('index')<0)bad.push('the writers never made it into the list: '+av.join(','));
if(!/index \(5\)/.test(selApp._appHtml||''))bad.push('the writer list must promise index lights 5 cells, got '+String(selApp._appHtml).match(/index \([0-9]+\)/));
if(av[1]!=='index')bad.push('the writer that lights the most cells must come first, got '+av[1]);
if(selApp.disabled)bad.push('the loaded writer filter is still disabled');
// 6 ONE state, TWO controls. A filter belongs to its own axis and the other
//    control must read "no filter" — two selects syncing themselves is how a
//    reader ends up with "ทุกชนิด" AND "ทุกผู้เขียน" showing while a live
//    filter is dimming half the map.
PV.filt={dim:'app',key:'chatdrop'};pvKindOptions();pvAppOptions();
if(sel.value!=='')bad.push('the kind control shows "'+sel.value+'" while the WRITER axis is filtered');
if(selApp.value!=='chatdrop')bad.push('the writer control lost the filter, got "'+selApp.value+'"');
PV.filt={dim:'kind',key:'bond'};pvKindOptions();pvAppOptions();
if(sel.value!=='bond')bad.push('the kind control lost the filter, got "'+sel.value+'"');
if(selApp.value!=='')bad.push('the writer control shows "'+selApp.value+'" while the KIND axis is filtered');
PV.filt=null;pvKindOptions();pvAppOptions();
if(sel.value!==''||selApp.value!=='')bad.push('clearing the filter left a control showing "'+(sel.value||selApp.value)+'"');
console.log(bad.join(' | ')+' :: '+bad.length);
"""


def _js_arrow(js, name):
    """The source of a top-level `const NAME=...` — an arrow, not a `function`.

    Same "slice the real thing" rule as _js_function(); it cannot reuse that one
    because it looks for `function NAME(`. pvTopKinds is the arrow form, and it
    is the one that decides what the filter's list and the legend PRINT — a
    stubbed count would have passed a function that returns all zeros.
    """
    marker = "const %s=" % name
    i = js.find(marker)
    if i < 0:
        return ""
    nl = chr(10)
    ends = [x for x in (js.find(nl + "const ", i + 1),
                        js.find(nl + "function ", i + 1),
                        js.find(nl + "/* ", i + 1)) if x > 0]
    return js[i:min(ends) if ends else len(js)].rstrip().rstrip(";")


def kind_list(js):
    """Run the REAL pvKindOptions and check it builds a LIST a reader can use.

    A filter has two halves: four functions that do the arithmetic, and one
    that fills the <select>. The arithmetic was proved correct by a node probe
    and the control was still unreachable — `ks.join` on a count OBJECT threw
    inside the one-line no-churn guard, on every path, and the select sat on a
    single option forever. Nothing was red. A check that only computes cannot
    see a list, so this one builds one.

    It now builds BOTH lists. The writer control is the same shape over a
    different partition, and "the same shape" is a claim that decays the first
    time someone adds a guard to one of them.
    """
    node = shutil.which("node")
    if not node:
        return ""
    src = _js_function(js, "pvKindOptions")
    if not src:
        return ("pvKindOptions must still be in the script: it is the only thing "
                "that makes the filter reachable at all")
    src2 = _js_function(js, "pvAppOptions")
    if not src2:
        return ("pvAppOptions must still be in the script: without it the writer "
                "filter has no list, and an <select> with one option is a "
                "control that cannot be chosen")
    top = _js_arrow(js, "pvTopKinds")
    if not top:
        return ("pvTopKinds must still be in the script: it is what the filter's "
                "list and the legend print next to every kind")
    d = tempfile.mkdtemp(prefix="ui-kindlist-")
    tmp = os.path.join(d, "kindlist.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(chr(10).join([LIST_PROBE_HEAD, top, src, src2, LIST_PROBE_TAIL]))
    try:
        r = subprocess.run([node, tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        return "the kind-list probe would not run: " + (r.stdout + r.stderr).strip()[:400]
    bad, n = _probe_count(r.stdout)
    if n != 0:
        return ("the filter's list does not open (%s). Each clause names what "
                "disagreed:" % n + chr(10) + "    " + bad[:600])
    return ""


def broken_invariants(js, html):
    return [(label, why(js, html) if callable(why) else why)
            for label, test, why in INVARIANTS if not test(js, html)]
    return [(label, why(js, html) if callable(why) else why)
            for label, test, why in INVARIANTS if not test(js, html)]


def main(argv):
    url = argv[argv.index("--url") + 1] if "--url" in argv else None
    html = page_html(url)
    js = extract_script(html)
    code, out, note = parse_js(js)
    if code != 0:
        print("ui_check: FAILED — the dashboard will not run as served")
        print(out.strip()[-1500:])
        return 1
    if code == 2:
        print(out)
    gone = missing_ids(html, js)
    if gone:
        print("ui_check: FAILED — the script grabs elements that are not in the page:")
        for i in gone:
            print("  $('#%s') — no such id in the markup" % i)
        print("  (the first one throws at load, so every handler wired after it stays dead)")
        return 1
    broken = broken_invariants(js, html)
    if broken:
        print("ui_check: FAILED — the dashboard parses, but these no longer hold:")
        for label, why in broken:
            print("  ✗ %s" % label)
            print("    %s" % why)
        return 1
    print("ui_check: OK — %s, every $('#id') it uses exists, and all %d "
          "structural invariants hold" % (note, len(INVARIANTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
