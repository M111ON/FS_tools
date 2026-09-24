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
const PV={mode:'blocks',n:12,seq:null,seqMax:9,budget:64,win:12,over:[],
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
    bad, _, n = r.stdout.strip().rpartition(" :: ")
    if n != "0":
        return ("%s addresses read two different ways. Each line names what "
                "disagreed, so the same square is drawn through one expression "
                "and reported through another:\n    %s" % (n, bad[:600]))
    return ""


TORUS_PROBE = r"""
const T144=144;
const PV={mode:'plane',n:144,origin:null,bOrig:{x:0,y:0},win:24};
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
// takes the short way (-2), never the long way (+142). PV.n must follow the
// mode: the page's pvMode() sets it, and this probe has no pvMode.
PV.mode='window';PV.n=24;PV.origin={wedge:100,ring:100};
const ew=pvEdgeEnds(100*144+100,100*144+104);
if(!near((ew[1][0]-ew[0][0])/8,4,0.01))fails.push('window normal pair gap='+((ew[1][0]-ew[0][0])/8));
const ws=pvEdgeEnds(100*144+102,100*144+((98)%144));
if(!near((ws[1][0]-ws[0][0])/8,-4,0.01))fails.push('window seam pair gap='+((ws[1][0]-ws[0][0])/8)+' (want -4)');
// window: an edge whose far end is outside the frame must still RETURN ends
// (canvas clips the line) as long as some part can touch the view
const eo=pvEdgeEnds(100*144+112,100*144+118);
if(!eo)fails.push('window: edge leaving the frame was dropped instead of clipped');
// blocks: block (0,0) -> block (11,0) is ONE block across the seam, on-screen
PV.mode='blocks';PV.n=12;PV.origin=null;
const eb=pvEdgeEnds(0,11*12);
const bg=(eb[1][0]-eb[0][0])/8;   // in the blocks branch positions are BLOCK units x pstep
if(!near(bg,11,0.01))fails.push('blocks seam pair gap='+bg+' blocks (want 11: the visible rim image)');
if(eb[0][0]<0||eb[1][0]<0)fails.push('blocks: seam image drawn off-canvas');
// re-entry: on the full plane an edge that runs off one rim must be painted a
// second time entering from the opposite rim — the torus promise. Back to
// plane mode FIRST: the blocks branch above left PV.n at 12.
PV.mode='plane';PV.n=144;
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
    bad, _, n = r.stdout.strip().rpartition(" :: ")
    if n != "0":
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


def broken_invariants(js, html):
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
