"""bond_real.py — derive REAL bonds from relations the source data states.

The pool had 11,656 lines and zero edges, so the plane drew dots with no
graph. The fix is not to invent pairings: it is to find the relations the
source metadata already asserts and transcribe them as bonds. Every edge
written here is checkable against the source, and the RELATION VERB names
the evidence, so a reader can audit an edge instead of trusting it:

  x-next-in-session  two rows that were consecutive in one session file
                     (facts.jsonl `source_file`, in file order)
  x-answers          an `exchange` and the `embedded_document` that came
                     back for the same session file AND the same term_hint
  x-same-hint        two rows carrying the same `term_hint` (same subject)
  x-related-term     a dictionary term whose OWN definition names another
                     dictionary term — a fact about the text, not a guess
  x-in-session       a pool.db chunk and the session summary it belongs to
                     (real containment: chunks.session_id -> sessions.id)
  x-next-chunk       two chunks consecutive by chunks.seq in one session
  x-same-workspace   two cloud_ws state rows in the same workspace_id
  x-hint-is-term     a fact's own subject label IS a term hermes defined
  x-uses-term        a line names a term another platform defined
                     (both of the last two join opencode/dashboard to hermes)

Nothing is inferred from vibes. Fan-out is bounded by CHAINING (row n to
row n+1) rather than by making a clique, because a clique over a 359-row
session is 64,000 edges and would bury the real structure. Every cap is
reported so a truncated relation is visible, never silent.

Usage:
  python bond_real.py --dry-run     # what it WOULD write, per relation
  python bond_real.py               # write the bonds through pool.bond()
  python bond_real.py --report      # write, then print the plane's edge stats

Idempotent: an edge that already exists (same src+rel+dst) is skipped, so
running it twice does not double the graph.
"""
import argparse
import collections
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool  # noqa: E402

TOOLS = os.path.dirname(HERE)
LOG_PATH = os.path.join(HERE, "log.jsonl")

MAX_TEXT = 1200          # reimport_real.py's cap: match it or nothing matches
BOND_APP = "index"       # who writes derived edges — visible in the feed
SAME_HINT_CAP = 24       # chained edges per term_hint
TERM_REL_CAP = 6         # related terms kept per dictionary term
USES_TERM_CAP = 8        # terms kept per cross-platform line (max seen: 21)
# A relation is only worth drawing when it DISCRIMINATES. "This definition
# mentions the word model" says almost nothing when 400 other definitions do,
# and those generic pairs were 65% of the graph. A referenced term is kept
# only while it stays rare as a reference; past this it is ordinary vocabulary.
TERM_REF_MAX = 10


# ── the log, addressed by text ───────────────────────────────────────────
def text_index():
    """text -> hash. A text can legitimately repeat, so the FIRST writer wins
    and the collision count is reported. This must be a LOOKUP, never a
    consume: every relation reads the same rows, so a generator that popped
    its hash would leave the next one staring at an empty index."""
    idx, dupes = {}, 0
    with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = m.get("hash")
            if not h:
                continue
            text = m.get("text", "")
            if text in idx:
                dupes += 1
                continue
            idx[text] = h
    return idx, dupes


def taker(idx):
    """Address one row by its text. None when the line is not in the log (the
    importer skipped it) — a missing end is skipped, never faked."""
    def take(text):
        return idx.get(text)
    return take


# ── relation sources ─────────────────────────────────────────────────────
def facts_rows():
    """pre_embedding_filter/facts.jsonl — real session-derived rows."""
    path = os.path.join(TOOLS, "pre_embedding_filter", "facts.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (r.get("content") or "").strip()
            if len(content) < 20:
                continue
            hint = r.get("term_hint") or r.get("pattern") or "fact"
            yield {"text": ("%s: %s" % (hint, content))[:MAX_TEXT],
                   "src": r.get("source_file") or "",
                   "hint": hint,
                   "kind": r.get("kind") or ""}


def dict_rows():
    """pre_embedding_filter/dictionary.jsonl — real term definitions."""
    path = os.path.join(TOOLS, "pre_embedding_filter", "dictionary.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            term = (r.get("term") or "").strip()
            definition = (r.get("definition") or "").strip()
            if not term or len(definition) < 20:
                continue
            yield {"term": term,
                   "text": ("DECISION: %s\n%s" % (term, definition))[:MAX_TEXT],
                   "definition": definition}


def pool_db_rows():
    """cloud-workspace/pool.db — chunks, sessions."""
    path = os.path.join(TOOLS, "cloud-workspace", "pool.db")
    if not os.path.exists(path):
        return None
    return sqlite3.connect(path)


def local_db_rows():
    """cloud_ws/mcp/local.db — workspace state rows."""
    path = os.path.join(TOOLS, "cloud_ws", "mcp", "local.db")
    if not os.path.exists(path):
        return None
    return sqlite3.connect(path)


# ── relation generators (each yields (src_hash, rel, dst_hash, note)) ────
def rel_next_in_session(idx, stats):
    """Two rows consecutive in the same session file. The session is the
    source's own grouping; adjacency inside it is the source's own order."""
    take = taker(idx)
    rows = list(facts_rows())
    for a, b in zip(rows, rows[1:]):
        if not a["src"] or a["src"] != b["src"]:
            continue
        h1, h2 = take(a["text"]), take(b["text"])
        if h1 and h2:
            yield h1, "x-next-in-session", h2, a["src"]
        else:
            stats["skipped_missing"] += 1


def rel_answers(idx, stats):
    """An exchange (the ask) and the embedded_document that came back for the
    same session file AND the same term_hint. The source's own pairing."""
    take = taker(idx)
    groups = collections.defaultdict(
        lambda: {"exchange": [], "embedded_document": []})
    for r in facts_rows():
        if r["kind"] in ("exchange", "embedded_document"):
            groups[(r["src"], r["hint"])][r["kind"]].append(r["text"])
    for (_src, _hint), g in groups.items():
        pairs = zip(g["exchange"], g["embedded_document"])
        for ask, answer in pairs:
            h1, h2 = take(ask), take(answer)
            if h1 and h2:
                yield h1, "x-answers", h2, _hint
            else:
                stats["skipped_missing"] += 1


def rel_same_hint(idx, stats):
    """Same subject line, chained and capped so a 359-row hint cannot bury
    the rest of the graph. The cap is reported, not silent."""
    take = taker(idx)
    buckets = collections.defaultdict(list)
    for r in facts_rows():
        buckets[r["hint"]].append(r["text"])
    for hint, texts in buckets.items():
        if len(texts) < 2:
            continue
        if len(texts) - 1 > SAME_HINT_CAP:
            stats["capped"] += (len(texts) - 1) - SAME_HINT_CAP
            texts = texts[:SAME_HINT_CAP + 1]
        for a, b in zip(texts, texts[1:]):
            h1, h2 = take(a), take(b)
            if h1 and h2:
                yield h1, "x-same-hint", h2, hint
            else:
                stats["skipped_missing"] += 1


def rel_related_term(idx, stats):
    """Term A's own definition names term B, and B is also a term. Indexed by
    token instead of scanning every pair (5,019^2 words is not a read).

    Then filtered by DISCRIMINATION: a reference to a term that half the
    dictionary also mentions is vocabulary, not a relationship. Without this
the relation was 65% of the whole graph and buried everything else."""
    take = taker(idx)
    rows = list(dict_rows())
    by_name = {}
    for r in rows:
        key = r["term"].lower()
        if len(key) > 3:
            by_name.setdefault(key, r)

    # first pass: how many definitions name each term at all
    refs = collections.Counter()
    named = []
    for r in rows:
        hits = []
        for tok in _tokens(r["definition"]):
            other = by_name.get(tok)
            if other is None or other["term"] == r["term"]:
                continue
            if other["term"] not in hits:
                hits.append(other["term"])
        named.append((r, hits))
        for name in hits:
            refs[name] += 1

    generic = {n for n, c in refs.items() if c > TERM_REF_MAX}
    stats["generic_terms_dropped"] = len(generic)
    kept = 0
    for r, hits in named:
        specific = [n for n in hits if n not in generic]
        stats["capped"] += len(hits) - len(specific) if len(hits) > TERM_REL_CAP else 0
        if len(specific) > TERM_REL_CAP:
            stats["capped"] += len(specific) - TERM_REL_CAP
            specific = specific[:TERM_REL_CAP]
        h1 = take(r["text"])
        if not h1:
            continue
        for name in specific:
            h2 = take(by_name[name.lower()]["text"])
            if h2:
                kept += 1
                yield h1, "x-related-term", h2, name
            else:
                stats["skipped_missing"] += 1
    stats["generic_pairs_dropped"] = sum(
        len([n for n in hits if n in generic]) for _r, hits in named)


def _term_names(min_len=3):
    """term(lower) -> its dictionary row. min_len 3 keeps 'CTD' — a short
    acronym is exactly the kind of term two platforms share."""
    out = {}
    for r in dict_rows():
        k = r["term"].lower()
        if len(k) >= min_len:
            out.setdefault(k, r)
    return out


def rel_hint_is_term(idx, stats):
    """CROSS-PLATFORM. A fact's OWN subject label is a term the other platform
    defined by name: the row was filed under that subject, and the dictionary
    entry is that subject's definition. The strongest cross edge available,
    because a person put the label there rather than a token matcher finding it.

    Note this relation deliberately does NOT use the rarity filter that
    x-uses-term needs. A hint shared by 273 rows is not ordinary vocabulary —
    it is a HUB, and how many rows point at it is the whole signal."""
    take = taker(idx)
    names = _term_names()
    for r in facts_rows():
        h = (r["hint"] or "").lower()
        other = names.get(h)
        if other is None:
            continue
        h1, h2 = take(r["text"]), take(other["text"])
        if h1 and h2:
            yield h1, "x-hint-is-term", h2, other["term"]
        else:
            stats["skipped_missing"] += 1


def _cross_corpus():
    """Every line from the OTHER platforms that could name a term, with the
    body the match is made against. The body is the content WITHOUT the hint
    prefix, so a term match means the term is used, not merely filed under."""
    for r in facts_rows():
        yield r["text"], r["text"].split(": ", 1)[-1], "opencode"
    db = local_db_rows()
    if db is None:
        return
    try:
        for category, key, value in db.execute(
                "SELECT category, key, value FROM state ORDER BY rowid"):
            v = (value or "").strip()
            if len(v) < 12:
                continue
            yield ("WS %s/%s: %s" % (category, key, v))[:MAX_TEXT], v, "dashboard"
    finally:
        db.close()


def rel_uses_term(idx, stats):
    """CROSS-PLATFORM. A line from one platform names a term another platform
    defined. Evidence is a whole-word match in the line's BODY, kept only while
    the term is still rare ACROSS the whole corpus (TERM_REF_MAX) — a term half
    the rows mention is vocabulary, and without that filter this relation buries
    the specific ones exactly the way x-related-term did."""
    take = taker(idx)
    names = _term_names()
    rows = list(_cross_corpus())
    bodies = [{names[t]["term"] for t in _tokens(body) if t in names}
              for _text, body, _app in rows]
    df = collections.Counter()
    for hits in bodies:
        for n in hits:
            df[n] += 1
    generic = {n for n, c in df.items() if c > TERM_REF_MAX}
    for (text, _body, _app), hits in zip(rows, bodies):
        specific = sorted(n for n in hits if n not in generic)
        if len(specific) > USES_TERM_CAP:
            stats["capped"] += len(specific) - USES_TERM_CAP
            specific = specific[:USES_TERM_CAP]
        if not specific:
            continue
        h1 = take(text)
        if not h1:
            stats["skipped_missing"] += 1
            continue
        for name in specific:
            h2 = take(names[name.lower()]["text"])
            if h2:
                yield h1, "x-uses-term", h2, name
            else:
                stats["skipped_missing"] += 1


def _tokens(text):
    """Word tokens, lowercased. Splitting on non-word characters means a
    hyphenated term matches either spelling rather than neither."""
    out, cur = [], []
    for ch in text:
        if ch.isalnum() or ch in "_-":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur).lower())
                cur = []
    if cur:
        out.append("".join(cur).lower())
    return out


def _chunks():
    """(session_id, ordered chunk texts) straight from pool.db."""
    db = pool_db_rows()
    if db is None:
        return
    try:
        for (sid,) in db.execute("SELECT DISTINCT session_id FROM chunks"):
            texts = []
            for (ctext,) in db.execute(
                    "SELECT text FROM chunks WHERE session_id=? ORDER BY seq", (sid,)):
                c = (ctext or "").strip()[:MAX_TEXT]
                if len(c) >= 10:
                    texts.append(c)
            if texts:
                yield sid, texts
        for summary, sid, label in db.execute(
                "SELECT summary, id, label FROM sessions WHERE summary IS NOT NULL"):
            yield ("hub", sid, label, summary)
    finally:
        db.close()


def rel_chunks_session(idx, stats):
    """A chunk and the session summary it belongs to. Real containment:
    chunks.session_id -> sessions.id is a column relationship."""
    take = taker(idx)
    hubs = {}
    plain = {}
    for item in _chunks():
        if item[0] == "hub":
            _tag, sid, label, summary = item
            hubs[sid] = take(("SESSION: %s (%s)\n%s" % (sid, label, summary))[:MAX_TEXT])
        else:
            plain[item[0]] = item[1]
    for sid, texts in plain.items():
        hub = hubs.get(sid)
        if not hub:
            continue
        for c in texts:
            h = take(c)
            if h:
                yield h, "x-in-session", hub, sid
            else:
                stats["skipped_missing"] += 1


def rel_chunks_seq(idx, stats):
    """Two chunks consecutive by chunks.seq in one session — the source's own
    order inside the session it filed them under."""
    take = taker(idx)
    for item in _chunks():
        if item[0] == "hub":
            continue
        _sid, texts = item
        prev = None
        for c in texts:
            h = take(c)
            if h and prev:
                yield prev, "x-next-chunk", h, _sid
            elif not h:
                stats["skipped_missing"] += 1
            prev = h


def rel_same_workspace(idx, stats):
    """cloud_ws state rows sharing a workspace_id — the column IS the group."""
    db = local_db_rows()
    if db is None:
        return
    try:
        take = taker(idx)
        for (ws_id,) in db.execute("SELECT DISTINCT workspace_id FROM state"):
            prev = None
            for category, key, value in db.execute(
                    "SELECT category, key, value FROM state WHERE workspace_id=?"
                    " ORDER BY rowid", (ws_id,)):
                v = (value or "").strip()
                if len(v) < 12:
                    continue
                text = ("WS %s/%s: %s" % (category, key, v))[:MAX_TEXT]
                h = take(text)
                if h and prev:
                    yield prev, "x-same-workspace", h, ws_id
                prev = h
    finally:
        db.close()


RELATIONS = [
    ("x-next-in-session  facts.jsonl rows consecutive in one session file", rel_next_in_session),
    ("x-answers          exchange + the document it produced (same file+hint)", rel_answers),
    ("x-same-hint        rows carrying the same term_hint (capped)", rel_same_hint),
    ("x-related-term     a term's own definition names another term", rel_related_term),
    ("x-hint-is-term     a fact's OWN subject label is a term hermes defined", rel_hint_is_term),
    ("x-uses-term        a line names a term another platform defined", rel_uses_term),
    ("x-in-session       pool.db chunk contained in its session summary", rel_chunks_session),
    ("x-same-workspace   cloud_ws rows in one workspace_id", rel_same_workspace),
    ("x-next-chunk       pool.db chunks consecutive by seq", rel_chunks_seq),
]


# ── main ─────────────────────────────────────────────────────────────────
def collect():
    """Every relation, deduped by (src, rel, dst)."""
    idx, dupes = text_index()
    stats = collections.Counter()
    stats["text_collisions"] = dupes
    edges, seen = [], set()
    for label, gen in RELATIONS:
        n = 0
        for src, rel, dst, note in gen(idx, stats):
            key = (src, rel, dst)
            if key in seen or src == dst:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            edges.append((src, rel, dst, note, label))
            n += 1
        stats["by_relation:" + rel] = n
    return edges, stats


def verify():
    """Re-check what the LOG claims, not what the deriver intended.

    The two cross-platform relations state something falsifiable about the
    lines they join, so they can be audited after the fact and from the log
alone: x-uses-term says the term appears in the source's body, and
    x-hint-is-term says the source's subject label IS the term. If a rule
    ever drifts, this is how it gets caught rather than trusted.

    The session/workspace relations are NOT checked here — they need the
    source files, which is what `--dry-run` re-derives from."""
    have, bonds = {}, []
    with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            have[m["hash"]] = m
            if (m.get("kind") or "").lower() == "bond":
                bonds.append(m)
    checked = collections.Counter()
    bad = collections.Counter()
    samples = []
    for b in bonds:
        p = (b.get("text") or "").split()
        if len(p) != 3:
            continue
        rel = p[1]
        if rel not in ("x-uses-term", "x-hint-is-term"):
            continue
        s, d = have.get(p[0]), have.get(p[2])
        if not s or not d:
            bad[rel] += 1
            checked[rel] += 1
            continue
        term = d["text"].split("\n", 1)[0].replace("DECISION:", "").strip().lower()
        checked[rel] += 1
        ok = (term in set(_tokens(s["text"].split(": ", 1)[-1]))
              if rel == "x-uses-term"
              else s["text"].split(": ", 1)[0].strip().lower() == term)
        if not ok:
            bad[rel] += 1
            if len(samples) < 3:
                samples.append((rel, term, s["text"][:80]))
    print("bond_real --verify: re-reading the log, not the deriver")
    for rel in sorted(checked):
        print("  %-16s %5d edges · %d contradict their own claim"
              % (rel, checked[rel], bad[rel]))
    for rel, term, text in samples:
        print("   MISS %s: term=%r not evidenced by %r" % (rel, term, text))
    ok = sum(checked.values()) and not sum(bad.values())
    print("  %s" % ("every cross edge is evidence-backed" if ok
                    else "FAILED — some edges claim what the lines do not show"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only")
    ap.add_argument("--report", action="store_true", help="print plane stats after")
    ap.add_argument("--verify", action="store_true",
                    help="re-check every cross-platform edge against the log")
    args = ap.parse_args()

    if args.verify:
        return verify()

    edges, stats = collect()
    by_rel = collections.Counter(e[1] for e in edges)
    print("bond_real: %d edges from %d relations" % (len(edges), len(by_rel)))
    for rel, n in by_rel.most_common():
        print("  %-20s %d" % (rel, n))
    print("  skipped (line not in log): %d · duplicate/dropped: %d · capped: %d"
          % (stats["skipped_missing"], stats["duplicate"], stats["capped"]))
    print("  log texts that repeat (first writer wins): %d" % stats["text_collisions"])
    if stats["generic_terms_dropped"]:
        print("  x-related-term: dropped %d terms named by >%d definitions → %d"
              " generic PAIRS" % (stats["generic_terms_dropped"], TERM_REF_MAX,
                                   stats["generic_pairs_dropped"]))

    # which apps the edges connect — a graph that only joins one app is a
    # smell, so say it before writing anything
    idx, _dupes = text_index()
    owner = {}
    with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            owner[m["hash"]] = m.get("app", "?")
    pairs = collections.Counter(
        tuple(sorted((owner.get(s, "?"), owner.get(d, "?")))) for s, _r, d, _n, _l in edges)
    print("  connects:", ", ".join("%s<->%s %d" % (a, b, n) for (a, b), n in pairs.most_common(6)))

    if args.dry_run:
        print("\n(--dry-run: nothing written)")
        return

    # idempotent: an edge that already exists is skipped, so a second run
    # does not double the graph
    live = {(b["src"], b["rel"], b["dst"]) for b in pool.all_bonds(raw=True)}
    new = [e for e in edges if (e[0], e[1], e[2]) not in live]
    print("\n  already present: %d · to write: %d" % (len(edges) - len(new), len(new)))

    written = failed = 0
    for src, rel, dst, _note, _label in new:
        try:
            pool.bond(src, rel, dst, app=BOND_APP)
            written += 1
        except Exception as exc:      # noqa: BLE001 — report, keep going
            failed += 1
            if failed <= 3:
                print("   skip: %s" % exc)
    print("  wrote %d bonds (failed %d)" % (written, failed))

    if args.report:
        d = pool.derived()
        every = pool.all_bonds(raw=True)
        print("\n  plane: %d lines · %d cells · %d edges · %d dangling"
              % (len(pool.tail(1 << 30)), len(d["cells"]), len(every),
                 sum(1 for e in every if e["dangling"])))
        homed = sum(1 for e in every if e["src_rkey"] is not None
                    and e["dst_rkey"] is not None)
        print("  edges with both ends on the plane: %d (%.1f%%)"
              % (homed, 100.0 * homed / max(1, len(every))))
        # an edge whose ends land in one cell is invisible on the plane, so
        # say how many actually span distance — that is the number you SEE
        span = sum(1 for e in every if e["src_rkey"] is not None
                   and e["dst_rkey"] is not None and e["src_rkey"] != e["dst_rkey"])
        print("  edges spanning two different cells: %d" % span)


if __name__ == "__main__":
    sys.exit(main() or 0)
