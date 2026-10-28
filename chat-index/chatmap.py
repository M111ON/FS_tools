#!/usr/bin/env python3
"""chatmap — query the unified order map (chatmap.db).

  python chatmap.py stats
  python chatmap.py ask "streaming capo load"      # one query across every layer
  python chatmap.py art "tesspack"                 # artifact / symbol lookup
  python chatmap.py session ses_f9cba8fe0         # one session: blocks + span
  python chatmap.py block 454                     # one block: title, p1..p4, span
  python chatmap.py timeline --project git:29b08  # sessions in date order
  python chatmap.py ds 201                        # deepseek chunk -> conversation + turn
"""
import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path
import time
from pathlib import Path

DB = Path(r"I:\tools\chat-index\chatmap.db")


def con():
    c = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    c.row_factory = sqlite3.Row
    return c


def ts(ms):
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def snip(s, n=160):
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _like(s):
    """Escape a value for use inside a LIKE pattern (callers pass `escape '\\'`)."""
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _digit_bound(form):
    """Regex that accepts Q only as a standalone number; None for decimals (substring is safe)."""
    if not re.fullmatch(r"\d+", form or ""):
        return None
    return re.compile(r"(?<![\w.])" + re.escape(form) + r"(?![\w.])")



def window(s, terms, width=200):
    """Head of the match: the text around the first query term, not the head of the row."""
    if not s:
        return ""
    low = s.lower()
    pos = -1
    for t in terms:
        t = t.strip().lower()
        if len(t) < 2:
            continue
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return snip(s, width)
    start = max(0, pos - width // 3)
    chunk = " ".join(s[start:start + width].split())
    return ("…" if start else "") + chunk + "…"


def cmd_stats(db):
    print("layer                rows")
    for t in ("session", "msg", "vault_turn", "block", "fact", "gitcommit",
              "ds_conv", "ds_turn", "ds_chunk", "ann", "artifact"):
        n = db.execute("select count(*) from %s" % t).fetchone()[0]
        print("  %-18s %7d" % (t, n))
    span = db.execute("select min(first_ms), max(last_ms) from session").fetchone()
    print("  session span       %s .. %s" % (ts(span[0]), ts(span[1])))
    print("\nsessions by harness")
    for r in db.execute("select coalesce(harness,'<none>') h, count(*) c, sum(n_block) b "
                        "from session group by 1 order by 2 desc"):
        print("  %-12s %5d sessions  %3d blocks" % (r["h"], r["c"], r["b"] or 0))
    print("\nprojects")
    for r in db.execute("select project, count(*) c from session where project is not null "
                        "group by 1 order by 2 desc"):
        print("  %-42s %d" % (r["project"], r["c"]))
    print("\nann kinds")
    for r in db.execute("select coalesce(kind,'<none>'), count(*) from ann group by 1 order by 2 desc"):
        print("  %-16s %6d" % (r[0], r[1]))


def session_exists(db, sid):
    if not sid:
        return False
    return db.execute("select 1 from session where sid=? limit 1", (sid,)).fetchone() is not None


SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def commit_of(db, q):
    """Authoritative commit lookup. Anything else shaped like a sha is only text."""
    q = q.lower()
    return db.execute("select sha, short, message, author, committed_ms, project from gitcommit "
                      "where sha=? or short=? or sha like ? order by committed_ms desc limit 1",
                      (q, q, q + "%")).fetchone()


def cmd_ask(db, q, k):
    q = q.strip()
    terms = [t for t in q.replace('"', "").split() if t]
    print("query: %r\n" % q)

    def quote(t):
        return '"%s"' % t.replace('"', "")

    and_expr = " ".join(quote(t) for t in terms) or '""'
    or_expr = " OR ".join(quote(t) for t in terms) or '""'
    exprs = [and_expr] if and_expr == or_expr else [and_expr, or_expr]

    def fts(sql, n):
        """AND first (precise), then OR (recall). A broken layer must shout, not look empty."""
        for expr in exprs:
            try:
                rows = db.execute(sql, (expr, n)).fetchall()
            except sqlite3.OperationalError as e:
                warns.append("query error (%s) — layer unreadable: %s" % (expr, e))
                return []
            if rows:
                return rows
        return []

    seen = []
    warns = []

    def show(head, rows):
        if not rows:
            return
        print("%s" % head)
        for r in rows:
            seen.append(1)
            print("  " + r)
        print()

    # 0. sha query: a commit hash is decided by gitcommit, not by text that looks like hex
    if SHA_RE.match(q.lower()):
        g = commit_of(db, q)
        if g:
            print("commit (authoritative)")
            print("  %s %s  %s" % (g["short"], ts(g["committed_ms"]), snip(g["message"], 110)))
            print("  %s  by %s  project %s" % (g["sha"], g["author"], g["project"]))
            print()
            seen.append(1)
        else:
            msg = ("[!] %r is hex-shaped but matches NO gitcommit — hits below are text artifacts, "
                   "not a commit" % q)
            print(msg + "\n")
            warns.append(msg)

    # 1. exact artifact
    art = db.execute("select name, kind, source, ref, count(*) c, max(ts_ms) t from artifact "
                     "where name = ? or name like ? group by 1,2,3,4 order by c desc limit ?",
                     (q, q + "%", k)).fetchall()
    if art:
        for a in art:
            if a["source"] == "block":
                b = db.execute("select title, sid, seq, start_ord, end_ord, ts_ms from block "
                               "where block_id=?", (a["ref"],)).fetchone()
                show("artifact %r (%s)" % (a["name"], a["kind"]),
                     ["block %s [%s] seq=%s ord %s..%s — %s | %s" %
                      (a["ref"], b["sid"], b["seq"], b["start_ord"], b["end_ord"],
                       ts(b["ts_ms"]), snip(b["title"], 90))] if b else [])
            elif a["source"] == "fact":
                f = db.execute("select category, source_sid, body from fact where fact_id=?",
                               (a["ref"],)).fetchone()
                if f:
                    line = "fact #%s %s <- %s | %s" % (a["ref"], f["category"], f["source_sid"],
                                                       snip(f["body"], 110))
                    if not session_exists(db, f["source_sid"]):
                        line += "\n      [!] ORPHAN fact — source session %r not found in MC or vault" \
                                % f["source_sid"]
                        warns.append("orphan fact #%s" % a["ref"])
                    show("artifact %r (%s)" % (a["name"], a["kind"]), [line])
            else:
                g = db.execute("select short, message, committed_ms from gitcommit where sha=?",
                               (a["ref"],)).fetchone()
                if g:
                    show("artifact %r (%s)" % (a["name"], a["kind"]),
                         ["commit %s %s %s" % (g["short"], ts(g["committed_ms"]),
                                               snip(g["message"], 90))])
                else:
                    line = "hex artifact %r — no such commit in gitcommit" % a["name"]
                    show("artifact %r (%s)" % (a["name"], a["kind"]), [line])
                    warns.append(line)

    # 2. blocks by title/p1..p4
    rows = fts("""select b.block_id, b.sid, b.seq, b.title, b.ts_ms, b.start_ord, b.end_ord,
                                b.p1 p1, b.p2 p2, b.title s
                         from block_fts f join block b on b.block_id=f.rowid
                         where block_fts match ? order by rank limit ?""", k)
    show("blocks", ["block %s [%s] seq=%s ord %s..%s %s — %s" %
                    (r["block_id"], r["sid"], r["seq"], r["start_ord"], r["end_ord"],
                     ts(r["ts_ms"]), snip(r["title"], 80)) + "\n      " +
                    window((r["p1"] or "") + " " + (r["p2"] or ""), terms, 220)
                    for r in rows])

    # 3. facts
    rows = fts("""select x.fact_id, x.category, x.source_sid, x.importance, x.created_ms,
                                x.body body
                         from fact_fts f join fact x on x.fact_id=f.rowid
                         where fact_fts match ? order by rank limit ?""", k)
    fact_lines = []
    for r in rows:
        line = "fact #%s %s imp=%s <- %s %s\n      %s" % (
            r["fact_id"], r["category"], r["importance"],
            r["source_sid"] or "<none>", ts(r["created_ms"]),
            window(r["body"], terms, 220))
        if not r["source_sid"]:
            line += "\n      [!] NO SOURCE recorded — this fact cannot be traced to a session"
            warns.append("fact #%s has no source_sid" % r["fact_id"])
        else:
            s = db.execute("select n_msg, in_source, in_mc_meta, archived, has_vault_md, "
                           "n_block, title, has_content, src_msg, src_v2_msg "
                           "from session where sid=?", (r["source_sid"],)).fetchone()
            if s is None:
                line += "\n      [!] ORPHAN fact — session id is unknown to MC, the vault and opencode.db"
                warns.append("orphan fact #%s (%s)" % (r["fact_id"], r["source_sid"]))
            elif not s["has_content"]:
                line += ("\n      [!] NO TRANSCRIPT ANYWHERE — session is a bare id "
                         "(source=%d meta=%d archived=%d, 0 messages, 0 blocks, no vault md)"
                         % (s["in_source"], s["in_mc_meta"], s["archived"]))
                warns.append("fact #%s points at an empty session (%s)"
                             % (r["fact_id"], r["source_sid"]))
        fact_lines.append(line)
    show("facts", fact_lines)

    # 4. opencode messages
    rows = fts("""select m.sid, m.ordinal, m.role, m.ts_ms, m.text body
                         from msg_fts f join msg m on m.rowid=f.rowid
                         where msg_fts match ? order by rank limit ?""", k)
    show("opencode messages", ["%s#%s %s %s\n      %s" %
                               (r["sid"], r["ordinal"], r["role"], ts(r["ts_ms"]),
                                window(r["body"], terms, 220)) for r in rows])

    # 5. vault archive (text MC no longer holds)
    rows = fts("""select v.sid, v.file, v.turn_no, v.role, v.body body
                         from vault_fts f join vault_turn v on v.rowid=f.rowid
                         where vault_fts match ? order by rank limit ?""", k)
    show("vault archive", ["%s turn %s %s (%s)\n      %s" %
                           (r["sid"], r["turn_no"], r["role"], r["file"],
                            window(r["body"], terms, 220)) for r in rows])

    # 6. deepseek
    rows = fts("""select t.cid, t.turn_no, t.role, t.body body
                         from ds_fts f join ds_turn t on t.rowid=f.rowid
                         where ds_fts match ? order by rank limit ?""", k)
    show("deepseek turns", ["%s turn %s %s\n      %s" %
                            (r["cid"][:8], r["turn_no"], r["role"],
                             window(r["body"], terms, 220)) for r in rows])

    # 7. commits
    rows = db.execute("select short, message, committed_ms from gitcommit where message like ? "
                      "order by committed_ms desc limit ?", ("%" + q + "%", k)).fetchall()
    show("commits", ["%s %s %s" % (r["short"], ts(r["committed_ms"]), snip(r["message"], 110))
                     for r in rows])

    if not seen:
        print("no hit in any layer")

    if warns:
        uniq = list(dict.fromkeys(warns))
        print("warnings (%d)" % len(uniq))
        for w in uniq[:12]:
            print("  [!] " + w)
        if len(uniq) > 12:
            print("  ... %d more" % (len(uniq) - 12))
        print()


def cmd_when(db, q, k=25, as_json=False, window_days=3):
    """Intersections, not a causal chain: time + fact + commit + artifact + doc + block.

    Every layer reports how many rows it actually looked at, so "found nothing" and "never
    searched" can never be confused. A number is looked up through the normalised metric layer
    (".9627" == "0.9627"), and each hit carries the raw spelling it matched.
    """
    q = (q or "").strip()
    if not q:
        print("usage: when <term|number|path|sha>")
        return 1
    k = max(1, min(int(k), 200))   # a negative -k used to reach SQLite as LIMIT -1 (= no limit)
    sha = bool(re.fullmatch(r"[0-9a-f]{7,40}", q.lower()))
    num = None
    if not sha:
        try:
            num = round(float(q.replace(",", "")), 6)
        except ValueError:
            num = None
    kind = "sha" if sha else ("number" if num is not None else "term")
    # spellings that mean the same number: the query as typed, the normalised value, and the
    # leading-zero-less form people actually write (".9627" for 0.9627). An INTEGER query has
    # no fraction, so it must not produce a "%.0%" pattern — that matched 2,212 vault turns
    # for `when 454` and every one of them was a false positive.
    num_forms = []
    if num is not None:
        norm = ("%f" % num).rstrip("0").rstrip(".")
        for c in (q, norm, (norm[1:] if norm.startswith("0.") else "")):
            if c and c not in num_forms:
                num_forms.append(c)

    r = db.execute("select built_at from src where kind='receipts'").fetchone()
    build = ts(r[0]) if r else "?"

    out = {"tool": "chatmap", "schema": 1, "build": build,
           "query": q, "query_kind": kind, "value_num": num,
           "layers": {}, "hits": [], "flags": [], "next": []}
    hits = out["hits"]

    def layer(name, n, searched=True, why=""):
        out["layers"][name] = {"searched": searched, "n": n, "why": why}

    # ---- 1. time: the normalised metric layer for numbers, full text for anything else
    ts_seen = []
    raw_forms = set()
    time_total = 0
    time_span = None      # (min_ms, max_ms) over ALL matches, not the k that happen to be shown
    if num is not None:
        rows = db.execute("select src, sid, ordinal, ts_ms, raw, ctx from metric "
                          "where value_num=? order by ts_ms is null, ts_ms", (num,)).fetchall()
        agg = db.execute("select count(*), min(ts_ms), max(ts_ms) from metric where value_num=?",
                         (num,)).fetchone()
        per = {}
        for r in rows:
            raw_forms.add(r["raw"])
            per.setdefault((r["src"], r["sid"], r["ordinal"]), []).append(r)
        # the vault md and the MC message row are the same words twice: keep the one that has a
        # clock, and remember the other as a mirror instead of printing it as a second event
        merged = {}
        for (src, sid, ordi), group in per.items():
            v = group[0]
            key = (sid, " ".join(v["ctx"].split())[:90])
            if key in merged:
                cur = merged[key]
                if v["ts_ms"] and not cur["ts_ms"]:
                    cur["alt"] = (cur["src"], cur["ordinal"])
                    cur.update(src=src, ordinal=ordi, ts_ms=v["ts_ms"], raw=v["raw"])
                continue
            merged[key] = {"src": src, "sid": sid, "ordinal": ordi, "ts_ms": v["ts_ms"],
                           "raw": v["raw"], "ctx": v["ctx"], "n": len(group), "alt": None}
        items = sorted(merged.values(), key=lambda d: (d["ts_ms"] is None, d["ts_ms"] or 0))
        time_total = agg[0] if agg else len(rows)
        if agg and agg[1]:
            time_span = (agg[1], agg[2])
        layer("time", time_total, True,
              "%d occurrences in %d coordinates (%d distinct events after vault/msg merge)"
              % (len(rows), len(per), len(items)))
        for v in items[:k]:
            ts_seen.append(v["ts_ms"])
            h = {"layer": "time", "src": v["src"], "sid": v["sid"], "ordinal": v["ordinal"],
                 "ts_ms": v["ts_ms"], "ts_iso": ts(v["ts_ms"]), "matched_via": v["raw"],
                 "occurrences": v["n"], "snippet": snip(v["ctx"], 190)}
            if v["alt"]:
                h["mirror"] = "%s#%s" % v["alt"]
            hits.append(h)
    else:
        # Unicode-aware: the old [A-Za-z0-9_-] tokenizer produced NO terms for a Thai query,
        # so every layer reported "searched, 0" about text that was never searched.
        terms = re.findall(r"[^\W_][\w.\-]*|[\w.\-]{2,}", q, re.UNICODE)[:8]
        expr = " AND ".join('"%s"' % t.replace('"', '""') for t in terms) if terms else None
        rows, agg_n = [], 0
        if expr:
            try:
                agg_n = db.execute("select count(*) from msg_fts where msg_fts match ?",
                                   (expr,)).fetchone()[0]
                rows = db.execute(
                    "select m.sid, m.ordinal, m.ts_ms, m.text from msg_fts f "
                    "join msg m on m.rowid=f.rowid where msg_fts match ? "
                    "order by m.ts_ms is null, m.ts_ms limit ?", (expr, k * 6)).fetchall()
            except sqlite3.OperationalError as e:
                out["flags"].append("layer_error:time_fts")
                rows = []
        # LIKE fallback: the FTS tokenizer keeps a whole Thai run as one token, so a substring
        # inside it is unreachable by MATCH even though the words are right there.
        if not rows:
            like = "%%%s%%" % _like(q)
            agg_n = db.execute("select count(*) from msg where text like ? escape '\\'",
                               (like,)).fetchone()[0]
            rows = db.execute("select sid, ordinal, ts_ms, text from msg where text like ? "
                              "escape '\\' order by ts_ms is null, ts_ms limit ?",
                              (like, k * 6)).fetchall()
        seen_txt = set()
        picked = []
        for r in rows:
            key = (r["sid"], " ".join((r["text"] or "").split())[:80])
            if key in seen_txt:
                continue
            seen_txt.add(key)
            picked.append(r)
            if len(picked) >= k:
                break
        stamped = [r["ts_ms"] for r in rows if r["ts_ms"]]
        if stamped:
            time_span = (min(stamped), max(stamped))
        time_total = agg_n or len(rows)
        layer("time", time_total, True,
              "%s %r (%d rows, %d distinct events)"
              % ("msg_fts" if expr else "msg like", expr or q, len(rows), len(picked)))
        for r in picked:
            ts_seen.append(r["ts_ms"])
            hits.append({"layer": "time", "src": "msg", "sid": r["sid"], "ordinal": r["ordinal"],
                         "ts_ms": r["ts_ms"], "ts_iso": ts(r["ts_ms"]),
                         "matched_via": q, "snippet": window(r["text"], terms)})

    # ---- 2. vault turns (they outlive MC's text truncation)
    # vault_turn has NO ts_ms column — asking for one raised OperationalError and the
    # except below turned the whole layer into "0 rows", which reads exactly like "this
    # number is not in the vault". Measured by scripts/eval_when.py: 0/15 on values that
    # provably are. Select only real columns, and make a failure visible in flags.
    vr = []
    vault_why = "exported md turns"
    vault_total = 0
    try:
        if num is not None:
            for form in num_forms:
                p = "%" + _like(form) + "%"
                if _digit_bound(form):
                    # a bare integer like 454 also occurs INSIDE ids, uuids and hashes
                    # ("84975945489f"), which inflated vault to 348 meaningless hits.  Count
                    # only real standalone numbers; report what the substring pass saw.
                    raw = db.execute("select sid, turn_no, body from vault_turn "
                                     "where body like ? escape '\\' limit 5001", (p,)).fetchall()
                    rx = _digit_bound(form)
                    kept = [r for r in raw if rx.search(r["body"] or "")]
                    vault_total = len(kept)
                    if len(raw) > len(kept):
                        out["flags"].append("substring_noise:vault:%d" % (len(raw) - len(kept)))
                    if vault_total:
                        vr = kept
                        vault_why = ("exported md turns (form %r, %d standalone of %d substring rows)"
                                     % (form, vault_total, len(raw)))
                        break
                    continue
                vault_total = db.execute("select count(*) from vault_turn where body like ? escape '\\'",
                                         (p,)).fetchone()[0]
                if vault_total:
                    vr = db.execute("select sid, turn_no, body from vault_turn "
                                    "where body like ? escape '\\' limit ?", (p, k)).fetchall()
                    vault_why = "exported md turns (form %r, %d rows)" % (form, vault_total)
                    break
            else:
                vault_why = "exported md turns (forms %s: 0 rows)" % ", ".join(map(repr, num_forms))
        else:
            like = "%%%s%%" % _like(q)
            vault_total = db.execute("select count(*) from vault_turn where body like ? escape '\\'",
                                     (like,)).fetchone()[0]
            vr = db.execute("select sid, turn_no, body from vault_turn "
                            "where body like ? escape '\\' limit ?", (like, k)).fetchall()
    except sqlite3.OperationalError as e:
        out["flags"].append("layer_error:vault")
        vr = []
        vault_why = "exported md turns (FAILED: %s)" % e
    layer("vault", vault_total or len(vr), True, vault_why)
    for r in vr[:k]:
        hits.append({"layer": "vault", "src": "vault", "sid": r["sid"], "ordinal": r["turn_no"],
                     "ts_ms": None, "ts_iso": "-",
                     "matched_via": next((f for f in (num_forms or [q]) if f in r["body"]), q),
                     "snippet": window(r["body"], num_forms or [q])})

    # ---- 3. facts (the main layer: 2,151 of them carry a source session)
    facts = []
    fact_total = 0
    fact_why = "memories with source id"
    if num is not None:
        for form in num_forms:
            p = "%" + _like(form) + "%"
            if _digit_bound(form):
                # same integer-inside-id noise as the vault layer: count standalone only
                rx = _digit_bound(form)
                raw = db.execute("select fact_id, project, category, status, source_sid, created_ms, body "
                                 "from fact where body like ? escape '\\' limit 5001", (p,)).fetchall()
                kept = [r for r in raw if rx.search(r["body"] or "")]
                fact_total = len(kept)
                if len(raw) > len(kept):
                    out["flags"].append("substring_noise:fact:%d" % (len(raw) - len(kept)))
                if fact_total:
                    facts = kept[:k]
                    fact_why = ("memories with source id (form %r, %d standalone of %d substring rows)"
                                % (form, fact_total, len(raw)))
                    break
                continue
            fact_total = db.execute("select count(*) from fact where body like ? escape '\\'",
                                    (p,)).fetchone()[0]
            if fact_total:
                facts = db.execute("select fact_id, project, category, status, source_sid, created_ms, body "
                                   "from fact where body like ? escape '\\' limit ?",
                                   (p, k)).fetchall()
                fact_why = "memories with source id (form %r, %d rows)" % (form, fact_total)
                break
    else:
        try:
            fact_total = db.execute("select count(*) from fact_fts where fact_fts match ?",
                                    (expr or q,)).fetchone()[0]
            facts = db.execute("select x.fact_id, x.project, x.category, x.status, x.source_sid, "
                               "x.created_ms, x.body from fact_fts f join fact x on x.fact_id=f.rowid "
                               "where fact_fts match ? limit ?", (expr or q, k)).fetchall()
        except sqlite3.OperationalError as e:
            out["flags"].append("layer_error:fact")
            facts = []
            fact_why = "memories (FAILED: %s)" % e
    layer("fact", fact_total or len(facts), True, fact_why)
    for r in facts:
        known = bool(r["source_sid"]) and db.execute(
            "select 1 from session where sid=?", (r["source_sid"],)).fetchone() is not None
        if r["source_sid"] and not known:
            out["flags"].append("orphan_fact:%d" % r["fact_id"])
        hits.append({"layer": "fact", "fact_id": r["fact_id"], "sid": r["source_sid"] or None,
                     "project": r["project"], "category": r["category"], "status": r["status"],
                     "ts_ms": r["created_ms"], "ts_iso": ts(r["created_ms"]), "matched_via": q,
                     "snippet": snip(r["body"], 190), "source_known": known})

    # ---- 4. commits: a WINDOW, never a file mapping (MC's table has no path or session column)
    commits = []
    commit_total = 0
    if kind == "sha":
        commits = db.execute("select sha, project, short, message, committed_ms from gitcommit "
                             "where lower(sha) like ? or lower(short)=? "
                             "order by committed_ms", ("%" + q.lower() + "%", q.lower())).fetchall()
        commit_total = len(commits)
        why = "sha lookup against the gitcommit table (authoritative, not text that looks like hex)"
    elif time_span:
        lo = time_span[0] - window_days * 86400000
        hi = time_span[1] + window_days * 86400000
        commit_total = db.execute("select count(*) from gitcommit where committed_ms between ? and ?",
                                  (lo, hi)).fetchone()[0]
        commits = db.execute("select sha, project, short, message, committed_ms from gitcommit "
                             "where committed_ms between ? and ? order by committed_ms",
                             (lo, hi)).fetchall()
        why = "window %s .. %s (±%dd around ALL time hits)" % (ts(lo), ts(hi), window_days)
    else:
        like = "%" + _like(q.lower()) + "%"
        commit_total = db.execute("select count(*) from gitcommit where lower(message) like ? escape '\\'",
                                  (like,)).fetchone()[0]
        commits = db.execute("select sha, project, short, message, committed_ms from gitcommit "
                             "where lower(message) like ? escape '\\' limit ?",
                             (like, k)).fetchall()
        why = "message match (no time hits to anchor a window)"
    layer("commit", commit_total, True, why)
    if len(commits) > 1:
        out["flags"].append("multi_commit_window")
    for r in commits[:k]:
        hits.append({"layer": "commit", "sha": r["sha"], "short": r["short"],
                     "project": r["project"], "ts_ms": r["committed_ms"],
                     "ts_iso": ts(r["committed_ms"]), "matched_via": q,
                     "snippet": snip(r["message"], 120)})

    # ---- 5. artifact paths (the real file intersection: 149k file paths + 60k paths)
    like = "%" + _like(q) + "%"
    art_rx = _digit_bound(q)
    if art_rx:
        # a bare integer also lives inside sha fragments ("849...454...89f") — those are not path hits
        raw = db.execute("select kind, name, ref from artifact where kind <> 'sha' "
                         "and name like ? escape '\\' limit 5001", (like,)).fetchall()
        arts = [r for r in raw if art_rx.search(r["name"] or "")]
        art_total = len(arts)
        if art_total == 0:
            art_why = "file/path/mem marks (sha excluded for a bare number)"
        else:
            art_why = "file/path/mem marks (%d standalone, sha excluded)" % art_total
    else:
        art_total = db.execute("select count(*) from artifact where name like ? escape '\\'",
                               (like,)).fetchone()[0]
        arts = db.execute("select kind, name, ref from artifact where name like ? escape '\\' limit ?",
                          (like, k)).fetchall()
        art_why = "file/path/mem/sha marks"
    layer("artifact", art_total, True, art_why)
    for r in arts[:k]:
        hits.append({"layer": "artifact", "kind": r["kind"], "ref": r["ref"],
                     "matched_via": q, "snippet": r["name"], "ts_ms": None, "ts_iso": "-"})

    # ---- 6. docs (values and rules often live in repo documents, not in conversation)
    docs = []
    doc_total = 0
    doc_why = "repo documents (md)"
    if num is not None:
        for form in num_forms:
            p = "%" + _like(form) + "%"
            doc_total = db.execute("select count(*) from doc where text like ? escape '\\'",
                                   (p,)).fetchone()[0]
            if doc_total:
                docs = db.execute("select id, path, title from doc where text like ? escape '\\' limit ?",
                                  (p, k)).fetchall()
                doc_why = "repo documents (md) (form %r, %d rows)" % (form, doc_total)
                break
    else:
        try:
            doc_total = db.execute("select count(*) from doc_fts where doc_fts match ?",
                                   (expr or q,)).fetchone()[0]
            docs = db.execute("select x.id, x.path, x.title from doc_fts f join doc x on x.id=f.rowid "
                              "where doc_fts match ? limit ?", (expr or q, k)).fetchall()
        except sqlite3.OperationalError as e:
            out["flags"].append("layer_error:doc")
            docs = []
            doc_why = "repo documents (FAILED: %s)" % e
        if not docs:
            like = "%" + _like(q) + "%"
            doc_total = db.execute("select count(*) from doc where text like ? escape '\\'",
                                   (like,)).fetchone()[0]
            docs = db.execute("select id, path, title from doc where text like ? escape '\\' limit ?",
                              (like, k)).fetchall()
            doc_why = "repo documents (md, like fallback, %d rows)" % doc_total
    layer("doc", doc_total or len(docs), True, doc_why)
    for r in docs[:k]:
        hits.append({"layer": "doc", "path": r["path"], "title": r["title"], "matched_via": q,
                     "snippet": snip(r["title"], 120), "ts_ms": None, "ts_iso": "-"})

    # ---- 7. blocks: compartments that COVER a found session, plus compartments whose own
    # text contains the value. Without the second half, a number recorded only inside a
    # block summary was unreachable — the tool said "not found" about something it had.
    blocks = []
    seen_bid = set()
    cover_ids = set()
    sids = {h.get("sid") for h in hits if h.get("sid")}
    if sids:
        for r in db.execute("select block_id, sid, seq, title, ts_ms from block where sid in (%s) "
                            "order by seq" % ",".join("?" * len(sids)),
                            tuple(sids)).fetchall():
            blocks.append(r)
            seen_bid.add(r["block_id"])
            cover_ids.add(r["block_id"])
    block_text = 0
    try:
        if num is not None:
            forms = ["%" + _like(f) + "%" for f in num_forms]
        else:
            forms = ["%" + _like(q) + "%"]
        where = " or ".join(
            "(" + " or ".join("p%d like ? escape '\\'" % i for i in (1, 2, 3, 4)) + ")"
            for _f in forms)
        args = []
        for f in forms:
            args += [f] * 4
        block_text = db.execute("select count(*) from block where " + where, tuple(args)).fetchone()[0]
        for r in db.execute("select block_id, sid, seq, title, ts_ms from block where " + where
                            + " limit ?", tuple(args + [k])).fetchall():
            if r["block_id"] not in seen_bid:
                blocks.append(r)
                seen_bid.add(r["block_id"])
    except sqlite3.OperationalError as e:
        out["flags"].append("layer_error:block")
        block_text = -1
    # covering = blocks already in `blocks` that came from a hit session; the old subtraction
    # mixed the two populations and under-reported the covering count
    n_cover = len([b for b in blocks if b["block_id"] in cover_ids])
    layer("block", len(blocks), True,
          "compartment summaries: %d covering a shown session (coverage follows k, text matches are totals), "
          "%d matching the text" % (n_cover, max(0, block_text)))
    for r in blocks[:k]:
        hits.append({"layer": "block", "block_id": r["block_id"], "sid": r["sid"],
                     "ts_ms": r["ts_ms"], "ts_iso": ts(r["ts_ms"]), "matched_via": q,
                     "snippet": snip(r["title"], 120)})
    if hits and not blocks:
        out["flags"].append("block_absent")

    # ---- judgment (every number below is a property of the DATA, never of the display limit)
    if raw_forms:
        out["raw_forms"] = sorted(raw_forms)
        if q not in raw_forms:
            out["flags"].append("normalized")
        if len(raw_forms) > 1:
            out["flags"].append("mixed_spellings")
    if time_total == 1:
        out["flags"].append("appears_once")
    if not any(out["layers"][n]["n"] for n in out["layers"]):
        out["flags"].append("not_found_in_any_searched_layer")
    if time_span:
        lo_c = db.execute("select min(committed_ms) a, max(committed_ms) b from gitcommit").fetchone()
        if lo_c and lo_c["a"] and (time_span[0] < lo_c["a"] or time_span[1] > lo_c["b"]):
            out["flags"].append("outside_commit_coverage")
        if time_span[1] > time_span[0]:
            out["span_days"] = round((time_span[1] - time_span[0]) / 86400000.0, 1)
        out["first_ts"], out["last_ts"] = ts(time_span[0]), ts(time_span[1])
    if len(hits) > k:
        out["flags"].append("truncated")

    # a value that appears hundreds of times needs a shape, not a dump
    if num is not None and out["layers"]["time"]["n"] > 0:
        months = dict(db.execute(
            "select strftime('%Y-%m', ts_ms/1000, 'unixepoch', 'localtime') m, count(*) "
            "from metric where value_num=? and ts_ms is not null group by 1 order by 1",
            (num,)).fetchall())
        und = db.execute("select count(*) from metric where value_num=? and ts_ms is null",
                         (num,)).fetchone()[0]
        out["by_month"] = months
        if und:
            out["by_month_undated"] = und
        if out["layers"]["time"]["n"] > k:
            out["flags"].append("dense:see_by_month")

    # ---- 8. query log (the other half of the oracle: which queries CONSUMED this row)
    # Written by ann_search.c (data/query_log/ann_query-YYYYMMDD.jsonl, one line per search).
    # Read-only, best effort, and explicit about how many files it opened: no file -> not
    # searched, which is different from "no query ever used this".
    qlog_hits = []
    qlog_files = []
    qdir = Path(r"I:\tools\ann\data\query_log")
    coords = {("%s#%s" % (h["sid"], h["ordinal"])) for h in hits
              if h.get("layer") in ("time", "vault") and h.get("sid") and h.get("ordinal") is not None}
    if qdir.is_dir() and coords:
        # No date filter on the log files: a query run TODAY can cite a message from any month,
        # so the file's own day says nothing about which coordinate it may contain. Scanning is
        # bounded by file count instead, and the count is reported.
        stale = sorted(qdir.glob("ann_query-*.jsonl"))[-400:]
        for f in stale:
            qlog_files.append(f.name)
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        for c in coords:
                            if '"coord":"%s"' % c in line:
                                try:
                                    rec = json.loads(line)
                                except ValueError:
                                    continue
                                qlog_hits.append((c, rec))
                                break
            except OSError:
                continue
    layer("querylog", len(qlog_hits), bool(qlog_files),
          "%d file(s) %s" % (len(qlog_files), ", ".join(qlog_files[:4]) if qlog_files
                            else "(no query_log dir or no coordinate hits to look up)"))
    for c, rec in qlog_hits[:k]:
        sid_part = c.split("#")[0]
        tops = rec.get("top") or []
        self_hit = bool(tops) and tops[0].get("score", 0) >= 0.9999
        if self_hit and "querylog_top1_exact" not in out["flags"]:
            out["flags"].append("querylog_top1_exact")
        hits.append({"layer": "querylog", "coord": c, "sid": sid_part,
                     "ordinal": int(c.split("#")[1]) if "#" in c else None,
                     "ts_ms": rec.get("ts_ms"), "ts_iso": ts(rec.get("ts_ms")),
                     "matched_via": c, "self_hit": self_hit,
                     "snippet": ("query %.0f us · top1 %.4f%s · scored %s · K=%s"
                                 % (rec.get("latency_us") or 0,
                                    tops[0].get("score", 0) if tops else 0,
                                    " (identical vector — the caller likely sent a row's own vec)"
                                    if self_hit else "",
                                    rec.get("items_scored"), rec.get("K")))})
        if rec.get("latency_us"):
            out.setdefault("qlog_latency_us", []).append(rec["latency_us"])

    first_sid = next((h["sid"] for h in hits if h.get("sid")), None)
    if first_sid:
        out["next"].append("chatmap session %s" % first_sid)
    out["next"].append("chatmap ask %r" % q)
    out["not_searched"] = {"kv_resume": "no kv_timeline.idx on this machine (2026-10-28)",
                          "ann_vector": "ANN rows carry no chat coordinate yet"}
    if not qlog_files:
        out["not_searched"]["query_log"] = "no ann_query-*.jsonl found (ANN logging starts 2026-10-29)"

    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # ---- human render
    print("query: %r  (%s, build %s)" % (q, kind, build))
    mark = " | ".join("%s=%s" % (n, ("-" if not v["searched"] else v["n"]))
                      for n, v in out["layers"].items())
    print("layers: %s" % mark)
    if out.get("raw_forms"):
        print("matched raw forms: %s   (query form %r)" % (", ".join(out["raw_forms"]), q))
    if out.get("by_month"):
        print("by month: %s" % ", ".join("%s:%d" % kv for kv in sorted(out["by_month"].items())))
    print()
    if not hits:
        print("no hit in any searched layer — this is 'not found', not 'no history'.")
        print("searched: %s" % ", ".join("%s(%s)" % (n, v["why"]) for n, v in out["layers"].items()))
        return 0
    per = max(2, k // max(1, len(out["layers"])))
    shown = 0
    for lname in out["layers"]:
        group = [h for h in hits if h["layer"] == lname]
        for h in group[:per]:
            shown += 1
            tag = h["layer"]
            if tag == "time" or tag == "vault":
                print("  %-5s %s#%-5s %s  %s" % (tag, (h["sid"] or "?")[:24], h["ordinal"],
                                                 h["ts_iso"], h["snippet"]))
            elif tag == "fact":
                print("  fact  #%-4s %-14s %-9s %s  %s" % (h["fact_id"], (h.get("category") or "")[:14],
                                                          (h.get("project") or "")[:9], h["ts_iso"],
                                                          h["snippet"]))
            elif tag == "commit":
                print("  commit %-8s %s %s" % (h["short"], h["ts_iso"], h["snippet"]))
            elif tag == "artifact":
                print("  %-8s %-10s %s" % (tag, h["kind"], h["snippet"][:100]))
            elif tag == "doc":
                print("  doc   %s" % h["path"])
            elif tag == "block":
                print("  block #%-4s %s  %s" % (h["block_id"], h["ts_iso"], h["snippet"]))
            elif tag == "querylog":
                print("  query %s  %s  %s" % (h["ts_iso"], h["coord"], h["snippet"]))
        if len(group) > per:
            print("  … %d more in %s (raise -k)" % (len(group) - per, lname))
    if len(hits) > shown and not any(len([h for h in hits if h["layer"] == n]) > per
                                     for n in out["layers"]):
        print("  … %d more (raise -k)" % (len(hits) - shown))
    print()
    if out["flags"]:
        print("flags: %s" % ", ".join(out["flags"]))
    print("next: %s" % " | ".join(out["next"][:2]))
    return 0


def cmd_audit(db):
    """The trust ledger: every place where a hit cannot be traced to ground truth."""
    print("authority")
    print("  gitcommit rows                    %7d" % db.execute(
        "select count(*) from gitcommit").fetchone()[0])
    print("  sessions                          %7d" % db.execute(
        "select count(*) from session").fetchone()[0])
    print("  sessions with messages            %7d" % db.execute(
        "select count(*) from session where n_msg>0").fetchone()[0])
    print("  sessions without messages         %7d" % db.execute(
        "select count(*) from session where n_msg=0").fetchone()[0])

    print("\nunsafe rows (must shout, never pass silently)")
    orphan_facts = db.execute(
        "select count(*) from fact f where f.source_sid is not null and f.source_sid<>'' "
        "and not exists (select 1 from session s where s.sid=f.source_sid)").fetchone()[0]
    bare = db.execute(
        "select count(*) from fact f join session s on s.sid=f.source_sid "
        "where s.has_content=0").fetchone()[0]
    bare_active = db.execute(
        "select count(*) from fact f join session s on s.sid=f.source_sid "
        "where s.has_content=0 and f.status='active'").fetchone()[0]
    no_src = db.execute("select count(*) from fact where source_sid is null or source_sid=''"
                        ).fetchone()[0]
    print("  facts -> session UNKNOWN anywhere       %7d  (id in no witness at all)" % orphan_facts)
    print("  facts -> session with NO content at all %7d  (bare id: no msgs/blocks/md)" % bare)
    print("      of those, fact status = active      %7d" % bare_active)
    print("  facts with no source session            %7d" % no_src)
    hex_art = db.execute(
        "select count(distinct name) from artifact a where kind='sha' and source<>'commit' "
        "and not exists (select 1 from gitcommit g where g.sha=a.name or g.short=a.name)"
    ).fetchone()[0]
    sha_ok = db.execute("select count(distinct name) from artifact where kind='sha' "
                        "and source='commit'").fetchone()[0]
    print("  hex artifacts matching no commit  %7d (distinct strings)" % hex_art)
    print("  sha artifacts that ARE commits    %7d (distinct strings)" % sha_ok)
    print("  deepseek chunks without conv      %7d" % db.execute(
        "select count(*) from ds_chunk where cid is null").fetchone()[0])
    print("  msg without text (MC dropped)     %7d" % db.execute(
        "select count(*) from msg where text is null").fetchone()[0])
    print("  msg without real clock            %7d" % db.execute(
        "select count(*) from msg where ts_src=0").fetchone()[0])
    print("  blocks without p3                 %7d" % db.execute(
        "select count(*) from block where p3 is null or p3=''").fetchone()[0])

    print("\nworst orphan-fact sources (session id -> facts)")
    for r in db.execute(
            "select f.source_sid, count(*) c from fact f "
            "where f.source_sid is not null and f.source_sid<>'' "
            "and not exists (select 1 from session s where s.sid=f.source_sid) "
            "group by 1 order by 2 desc limit 5"):
        print("  %-40s %4d" % (r[0], r[1]))

    print("\nsession witnesses (a fact is orphan only when none of them has the id)")
    for label, sql in (("opencode.db session", "select count(*) from session where in_source=1"),
                       ("opencode.db session_v2 only",
                        "select count(*) from session where src_v2=1 and in_source=1 and src_msg=0"),
                       ("MC session_meta only", "select count(*) from session where in_mc_meta=1"),
                       ("vault md", "select count(*) from session where has_vault_md=1"),
                       ("with messages (MC)", "select count(*) from session where n_msg>0"),
                       ("HAS CONTENT (any layer)",
                        "select count(*) from session where has_content=1"),
                       ("NO content in any layer",
                        "select count(*) from session where has_content=0"),
                       ("EMPTY at the source (0 msgs in v1+v2)",
                        "select count(*) from session where in_source=1 and src_msg=0 "
                        "and src_v2_msg=0"),
                       ("in opencode.db but 0 messages in map",
                        "select count(*) from session where in_source=1 and n_msg=0"),
                       ("recovered stubs (source title)",
                        "select count(*) from session where src_title like "
                        "'Recovered from magic-context%'")):
        print("  %-52s %6d" % (label, db.execute(sql).fetchone()[0]))

    print("\nregression canaries (signatures of the 2026-10-04 recovery run)")
    for r in db.execute("select first_ms, count(*) c from session where first_ms is not null "
                        "group by 1 having c >= 5 order by c desc limit 3"):
        print("  %3d sessions share one created timestamp %s"
              % (r[1], ts(r[0])))
    print("  sessions titled 'Recovered from magic-context'  %6d  (source title)"
          % db.execute("select count(*) from session where src_title like "
                       "'Recovered from magic-context%'").fetchone()[0])
    print("      title rebuilt from first user message        %6d"
          % db.execute("select count(*) from session where src_title like "
                       "'Recovered from magic-context%' "
                       "and title_src='first_message'").fetchone()[0])
    print("      title still just the marker                  %6d"
          % db.execute("select count(*) from session where title like "
                       "'Recovered from magic-context%'").fetchone()[0])
    print("  sessions with no content in any layer           %6d"
          % db.execute("select count(*) from session where has_content=0").fetchone()[0])

    print("\nprovenance layers (what 'when' can intersect)")
    mcells = db.execute("select count(*) from metric").fetchone()[0]
    mvals = db.execute("select count(distinct value_num) from metric").fetchone()[0]
    docs = db.execute("select count(*) from doc").fetchone()[0]
    print("  metric cells %8d / %5d distinct numeric values" % (mcells, mvals))
    print("  doc files    %8d md searchable" % docs)
    print("  artifact     %8d paths/files/PRs/shas" %
          db.execute("select count(*) from artifact").fetchone()[0])

    print("\nfts layers (a dead layer looks exactly like 'no hit')")
    for name, sql, src in (
            ("msg_fts", "select count(*) from msg_fts f join msg m on m.rowid=f.rowid", "msg"),
            ("vault_fts", "select count(*) from vault_fts f join vault_turn v on v.rowid=f.rowid",
             "vault_turn"),
            ("ds_fts", "select count(*) from ds_fts f join ds_turn t on t.rowid=f.rowid", "ds_turn"),
            ("fact_fts", "select count(*) from fact_fts f join fact x on x.fact_id=f.rowid", "fact"),
            ("block_fts", "select count(*) from block_fts f join block b on b.block_id=f.rowid",
             "block"),
            ("doc_fts", "select count(*) from doc_fts f join doc x on x.id=f.rowid", "doc")):
        live = db.execute(sql).fetchone()[0]
        total = db.execute("select count(*) from %s" % src).fetchone()[0]
        mark = "" if (live or not total) else "   <-- DEAD"
        print("  %-10s joinable %6d / %6d%s" % (name, live, total, mark))


def cmd_art(db, q, k):
    rows = db.execute("select name, kind, count(*) c, max(ts_ms) t from artifact "
                      "where name like ? group by 1,2 order by c desc, t desc limit ?",
                      ("%" + q + "%", k)).fetchall()
    if not rows:
        print("no artifact match")
        return
    for r in rows:
        one = db.execute("select source, ref from artifact where name=? and kind=? "
                         "and (ts_ms is not null) order by ts_ms desc limit 1",
                         (r["name"], r["kind"])).fetchone()
        src = one["source"] if one else "?"
        ref = one["ref"] if one else ""
        where = ""
        if src == "block":
            b = db.execute("select sid, title, ts_ms from block where block_id=?", (ref,)).fetchone()
            where = "%s %s | %s" % (b["sid"], ts(b["ts_ms"]), snip(b["title"], 70)) if b else ""
        elif src == "fact":
            f = db.execute("select source_sid, body from fact where fact_id=?", (ref,)).fetchone()
            where = "%s | %s" % (f["source_sid"], snip(f["body"], 90)) if f else ""
        elif src in ("msg", "vault"):
            sid, _, ordinal = ref.partition("#")
            where = "%s#%s %s" % (sid, ordinal, ts(r["t"]))
        elif src == "commit":
            g = db.execute("select message, committed_ms from gitcommit where sha=?", (ref,)).fetchone()
            where = "%s | %s" % (ts(g["committed_ms"]), snip(g["message"], 70)) if g else ""
        print("  %-44s %-5s x%-4d %-7s %s" % (r["name"][:44], r["kind"], r["c"], src, where))


def cmd_session(db, prefix):
    s = db.execute("select * from session where sid like ? order by last_ms desc limit 1",
                   (prefix + "%",)).fetchone()
    if not s:
        print("no session matching", prefix)
        return
    print("session   %s" % s["sid"])
    print("harness   %s   project %s" % (s["harness"], s["project"]))
    print("span      %s .. %s" % (ts(s["first_ms"]), ts(s["last_ms"])))
    print("counts    msg=%s text=%s block=%s tag=%s vault_md=%s"
          % (s["n_msg"], s["n_text"], s["n_block"], s["n_tag"], s["has_vault_md"]))
    for b in db.execute("select * from block where sid=? order by seq", (s["sid"],)):
        print("\n  block %s seq=%s ord %s..%s imp=%s %s" %
              (b["block_id"], b["seq"], b["start_ord"], b["end_ord"], b["importance"], ts(b["ts_ms"])))
        print("    %s" % snip(b["title"], 150))
        for fld in ("p1", "p2", "p3", "p4"):
            if b[fld]:
                print("    %-2s %s" % (fld, snip(b[fld], 220)))


def cmd_block(db, bid):
    b = db.execute("select * from block where block_id=?", (bid,)).fetchone()
    if not b:
        print("no block", bid)
        return
    print("block %s  session %s  seq %s" % (b["block_id"], b["sid"], b["seq"]))
    print("span  ord %s..%s   %s   importance %s   episode %s" %
          (b["start_ord"], b["end_ord"], ts(b["ts_ms"]), b["importance"], b["episode_type"]))
    print("title %s" % b["title"])
    for fld in ("p1", "p2", "p3", "p4"):
        if b[fld]:
            print("%-5s %s" % (fld, b[fld]))
    print("\nmessages in span (text where MC still holds it):")
    for m in db.execute("select ordinal, role, ts_ms, text from msg where sid=? and ordinal between ? and ? "
                        "order by ordinal", (b["sid"], b["start_ord"], b["end_ord"])):
        print("  %s#%-4s %-9s %s" % (b["sid"][-6:], m["ordinal"], m["role"], snip(m["text"], 130)))


def cmd_timeline(db, project, limit):
    q = "select * from session"
    args = ()
    if project:
        q += " where project like ?"
        args = (project + "%",)
    q += " order by first_ms desc limit ?"
    args = args + (limit,)
    for s in db.execute(q, args):
        print("  %s  %-12s msg=%-4s blk=%-3s %s" %
              (ts(s["first_ms"]), s["harness"], s["n_msg"], s["n_block"], s["sid"]))


def cmd_ds(db, key):
    c = db.execute("select * from ds_chunk where chunk_index=?", (key,)).fetchone()
    if c:
        print("chunk %s  conv %s" % (c["chunk_index"], c["cid"]))
        print("  title  %s" % c["title"])
        print("  turn   %s   offset %s char   chars %s" % (c["turn_no"], c["offset"], c["chars"]))
        print("  text   %s" % snip(c["text"], 300))
        return
    rows = db.execute("select index_name from sqlite_master where 0").fetchall()
    cs = db.execute("select * from ds_conv where cid like ? or title like ? limit 5",
                    (key + "%", "%" + key + "%")).fetchall()
    for c in cs:
        print("conv %s  %s" % (c["cid"], c["title"]))
        print("   %s .. %s  turns=%s chunks=%s chars=%s" %
              (ts(c["inserted_ms"]), ts(c["updated_ms"]), c["n_turns"], c["n_chunks"], c["chars"]))


def main():
    ap = argparse.ArgumentParser(description="chatmap query tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("audit")
    p = sub.add_parser("ask"); p.add_argument("q"); p.add_argument("-k", type=int, default=5)
    p = sub.add_parser("art"); p.add_argument("q"); p.add_argument("-k", type=int, default=20)
    p = sub.add_parser("session"); p.add_argument("sid")
    p = sub.add_parser("block"); p.add_argument("bid", type=int)
    p = sub.add_parser("timeline"); p.add_argument("--project"); p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("ds"); p.add_argument("key")
    p = sub.add_parser("when"); p.add_argument("q")
    p.add_argument("-k", type=int, default=25, help="rows shown per layer (layer counts are always totals)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--window", type=int, default=3, help="commit window radius in days")
    a = ap.parse_args()

    if not DB.exists():
        sys.exit("chatmap.db missing — run scripts/build_chatmap.py")
    db = con()
    t0 = time.time()
    if a.cmd == "stats":
        cmd_stats(db)
    elif a.cmd == "audit":
        cmd_audit(db)
    elif a.cmd == "ask":
        cmd_ask(db, a.q, a.k)
    elif a.cmd == "art":
        cmd_art(db, a.q, a.k)
    elif a.cmd == "session":
        cmd_session(db, a.sid)
    elif a.cmd == "block":
        cmd_block(db, a.bid)
    elif a.cmd == "timeline":
        cmd_timeline(db, a.project, a.limit)
    elif a.cmd == "ds":
        cmd_ds(db, a.key)
    elif a.cmd == "when":
        cmd_when(db, a.q, a.k, a.json, a.window)
    elapsed = time.time() - t0
    if elapsed > 0.5:
        # stderr, never stdout: this line used to be appended to `--json` output and broke
        # every machine reader with "Extra data: line 686" (found by scripts/eval_when.py).
        print("\n(%.2fs)" % elapsed, file=sys.stderr)


if __name__ == "__main__":
    main()



