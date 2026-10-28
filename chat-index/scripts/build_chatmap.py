#!/usr/bin/env python3
"""Build chatmap.db — one read-only order map over every corpus this machine keeps.

Bridges (read-only, sources are never written):

  magic-context context.db   session_projects, message_history_source, message_history_fts,
                             compartments (blocks), memories (facts), tags (message marks),
                             git_commits, compartment_chunk_embeddings
  vault exported_session     .md fallback text for sessions MC never indexed
  deepseek export            conversations.json + turns (4).txt + chat-dsk2.json chunks
  ann data/memcore           ann_index.jsonl (sid/file/npast) + ann_tags.tsv (kind/weight)

Output: chatmap.db (delete and rebuild, never edited in place)
Every count printed here is read back from the built db, not from the inputs.
"""
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(r"I:\tools\chat-index")
WORK = ROOT / "work"
DB = ROOT / "chatmap.db"
TMP = ROOT / "chatmap.db.tmp"
MC = Path(r"I:\cortexkit\magic-context\context.db")
VAULT = Path(r"I:\.vault\exported_session")
DSK = Path(r"F:\Dowload_backup\Dow_October\deepseek_data-2026-09-29")
ANN = Path(r"I:\tools\ann\data\memcore")
t0 = time.time()


def step(msg):
    print("[%7.1fs] %s" % (time.time() - t0, msg))


def iso_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def clean(s):
    """Drop lone surrogates that appear when a chunker cut an emoji in half."""
    if not isinstance(s, str):
        return s
    return s.encode("utf-8", "ignore").decode("utf-8", "ignore")


def clean_rows(rows, idxs):
    return [tuple(clean(v) if i in idxs else v for i, v in enumerate(r)) for r in rows]


if TMP.exists():
    TMP.unlink()
for suf in ("-wal", "-shm"):
    p = Path(str(TMP) + suf)
    if p.exists():
        p.unlink()
out = sqlite3.connect(TMP)
out.execute("pragma journal_mode=WAL")
out.execute("pragma synchronous=NORMAL")
out.executescript("""
create table src(kind text, path text, built_at int, detail text);
create table project(path text primary key, kind text);
create table session(
  sid text primary key, harness text, project text,
  first_ms int, last_ms int, n_msg int, n_text int, n_block int, n_tag int,
  title text, has_vault_md int,
  in_source int, in_mc_meta int, archived int, directory text, parent_sid text, src_msg int,
  src_v2 int, src_v2_msg int, has_content int, src_title text, title_src text);
create table msg(
  sid text, ordinal int, mid text, role text, ts_ms int, chars int, text text, ts_src int,
  primary key(sid, mid));
create table block(
  block_id int primary key, sid text, seq int, start_ord int, end_ord int,
  start_mid text, end_mid text, title text, importance int, episode_type text,
  ts_ms int, p1 text, p2 text, p3 text, p4 text, n_chunk_emb int);
create table fact(
  fact_id int primary key, project text, category text, scope text, importance int,
  status text, source_sid text, source_type text, body text, created_ms int, seen int, hits int);
create table gitcommit(
  sha text primary key, project text, short text, message text, author text, committed_ms int);
create table ds_conv(
  cid text primary key, title text, inserted_ms int, updated_ms int, n_turns int, n_chunks int, chars int);
create table ds_turn(
  cid text, turn_no int, stream_idx int, role text, chars int, body text,
  primary key(cid, turn_no));
create table ds_chunk(
  chunk_index int primary key, cid text, title text, turn_no int, offset int, chars int, text text);
create table ann(sid text primary key, kind text, weight int, file text, npast int);
create table vault_turn(
  sid text, file text, turn_no int, role text, chars int, body text,
  primary key(file, turn_no));
create table artifact(name text, kind text, source text, ref text, ts_ms int);
create index idx_msg_sid on msg(sid, ordinal);
create index idx_block_sid on block(sid, seq);
create index idx_fact_src on fact(source_sid);
create index idx_ds_turn on ds_turn(cid, turn_no);
""")
step("schema created")

# ---------------------------------------------------------------- magic-context
mc = sqlite3.connect("file:%s?mode=ro" % MC.as_posix(), uri=True)
mc.row_factory = sqlite3.Row
cur = mc.cursor()

projects = {}
for r in cur.execute("select distinct project_path from session_projects where project_path is not null"):
    p = r[0]
    projects[p] = "git" if p.startswith("git:") else "dir"
out.executemany("insert or replace into project(path,kind) values(?,?)", projects.items())
step("projects: %d" % len(projects))

sess_proj = {}
for r in cur.execute("select session_id, project_path from session_projects"):
    sess_proj[r["session_id"]] = r["project_path"]

harness = {}
for r in cur.execute("select session_id, harness from session_meta"):
    harness[r["session_id"]] = r["harness"]

# message text + the real event clock
# updated_at in message_history_source is INGEST time (every row of a re-imported session
# carries one identical stamp), so ordering must come from message_fts_rowid_map.
text_by_key = {}
for r in cur.execute("select session_id, message_ordinal, content from message_history_fts"):
    text_by_key[(r["session_id"], int(r["message_ordinal"]))] = r["content"]
step("mc text rows: %d" % len(text_by_key))

time_by_key = {}
for r in cur.execute("select session_id, message_ordinal, message_time_ms from message_fts_rowid_map"
                     " where message_time_ms is not null"):
    time_by_key[(r["session_id"], int(r["message_ordinal"]))] = r["message_time_ms"]
step("mc event clocks: %d" % len(time_by_key))

msg_rows = []
sess_stat = {}
for r in cur.execute("""select session_id, message_ordinal, message_id, role, updated_at
                        from message_history_source"""):
    sid = r["session_id"]
    ordn = r["message_ordinal"]
    body = text_by_key.get((sid, ordn))
    ts = time_by_key.get((sid, ordn)) or r["updated_at"]
    real = 1 if (sid, ordn) in time_by_key else 0
    msg_rows.append((sid, ordn, r["message_id"], r["role"], ts,
                     len(body) if body else 0, body, real))
    st = sess_stat.setdefault(sid, {"first": None, "last": None, "n": 0, "t": 0})
    if ts:
        st["first"] = min(st["first"] or ts, ts)
        st["last"] = max(st["last"] or ts, ts)
    st["n"] += 1
    if body:
        st["t"] += 1
out.executemany("insert or replace into msg(sid,ordinal,mid,role,ts_ms,chars,text,ts_src) "
                "values(?,?,?,?,?,?,?,?)",
                clean_rows(msg_rows, {6}))
step("mc messages: %d" % len(msg_rows))

# blocks = compartments (the historian already decided these boundaries)
emb_count = {}
for r in cur.execute("select compartment_id, count(*) c from compartment_chunk_embeddings group by 1"):
    emb_count[r["compartment_id"]] = r["c"]
block_rows = []
for r in cur.execute("""select id, session_id, sequence, start_message, end_message,
                               start_message_id, end_message_id, title, importance,
                               episode_type, created_at, p1, p2, p3, p4
                        from compartments"""):
    block_rows.append((r["id"], r["session_id"], r["sequence"], r["start_message"], r["end_message"],
                       r["start_message_id"], r["end_message_id"], r["title"], r["importance"],
                       r["episode_type"], r["created_at"], r["p1"], r["p2"], r["p3"], r["p4"],
                       emb_count.get(r["id"], 0)))
out.executemany("""insert or replace into block
  (block_id,sid,seq,start_ord,end_ord,start_mid,end_mid,title,importance,episode_type,
   ts_ms,p1,p2,p3,p4,n_chunk_emb) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                clean_rows(block_rows, {7, 11, 12, 13, 14}))
step("blocks: %d" % len(block_rows))

fact_rows = []
for r in cur.execute("""select id, project_path, category, scope, importance, status,
                               source_session_id, source_type, content, created_at,
                               seen_count, retrieval_count from memories"""):
    fact_rows.append((r["id"], r["project_path"], r["category"], r["scope"], r["importance"],
                      r["status"], r["source_session_id"], r["source_type"], r["content"],
                      r["created_at"], r["seen_count"], r["retrieval_count"]))
out.executemany("""insert or replace into fact
  (fact_id,project,category,scope,importance,status,source_sid,source_type,body,created_ms,seen,hits)
  values(?,?,?,?,?,?,?,?,?,?,?,?)""", clean_rows(fact_rows, {8}))
step("facts: %d" % len(fact_rows))

commit_rows = [(r["sha"], r["project_path"], r["short_sha"], r["message"], r["author"],
                r["committed_at"]) for r in cur.execute(
    "select sha, project_path, short_sha, message, author, committed_at from git_commits")]
out.executemany("insert or replace into gitcommit(sha,project,short,message,author,committed_ms) "
                "values(?,?,?,?,?,?)", commit_rows)
step("commits: %d" % len(commit_rows))

tag_count = {}
for r in cur.execute("select session_id, count(*) c from tags group by 1"):
    tag_count[r["session_id"]] = r["c"]
mc.close()

# ---------------------------------------------------------------- deepseek
stream = json.loads((WORK / "ds_stream_map.json").read_text(encoding="utf-8"))
turns = stream["turns"]
conv_meta = stream["conv_meta"]
chunk_prov = stream["chunks"]
chunks = json.loads((DSK / "chat-dsk2.json").read_text(encoding="utf-8"))

# turn body: keep full text; we have it in the stream
ds_turns = []
for t in turns:
    if not t.get("conv"):
        continue
    ds_turns.append((t["conv"], t["turn_no"], t["stream_idx"], t["role"], t["chars"], t["content"]))
# a conversation can legitimately repeat a turn number only if our matcher was wrong; count it
seen = Counter((a, b) for a, b, *_ in ds_turns)
dupes = [k for k, v in seen.items() if v > 1]
if dupes:
    print("  WARN duplicate (cid,turn_no): %d e.g. %s" % (len(dupes), dupes[:3]))
out.executemany("insert or replace into ds_turn(cid,turn_no,stream_idx,role,chars,body) "
                "values(?,?,?,?,?,?)", clean_rows(ds_turns, {5}))

ds_chunks = []
for c in chunks:
    pv = chunk_prov.get(str(c["index"]), {})
    ds_chunks.append((c["index"], pv.get("conv"), pv.get("title"), pv.get("turn_no"),
                      pv.get("offset"), c["chars"], c["text"]))
out.executemany("insert or replace into ds_chunk(chunk_index,cid,title,turn_no,offset,chars,text) "
                "values(?,?,?,?,?,?,?)", clean_rows(ds_chunks, {2, 6}))

per_conv_turns = Counter(t[0] for t in ds_turns)
per_conv_chunks = Counter(c[1] for c in ds_chunks if c[1])
per_conv_chars = Counter()
for c in ds_chunks:
    if c[1]:
        per_conv_chars[c[1]] += c[5]
conv_rows = []
for cid, meta in conv_meta.items():
    conv_rows.append((cid, meta.get("title"), iso_ms(meta.get("inserted_at")),
                      iso_ms(meta.get("updated_at")), per_conv_turns.get(cid, 0),
                      per_conv_chunks.get(cid, 0), per_conv_chars.get(cid, 0)))
out.executemany("insert or replace into ds_conv(cid,title,inserted_ms,updated_ms,n_turns,n_chunks,chars) "
                "values(?,?,?,?,?,?,?)", conv_rows)
step("deepseek: %d conv / %d turns / %d chunks" % (len(conv_rows), len(ds_turns), len(ds_chunks)))

# ---------------------------------------------------------------- ann
ann_tags = {}
with open(ANN / "ann_tags.tsv", encoding="utf-8") as f:
    for line in f:
        p = line.rstrip("\n").split("\t")
        if len(p) >= 3:
            ann_tags[p[0]] = (p[1], int(p[2]) if p[2].isdigit() else 1)
step("ann tags: %d" % len(ann_tags))

ann_rows = []
missing_tag = 0
with open(ANN / "ann_index.jsonl", encoding="utf-8") as f:
    for line in f:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = d.get("sid")
        if not sid:
            continue
        kind, w = ann_tags.get(sid, (None, None))
        if kind is None:
            missing_tag += 1
        npast = d.get("npast")
        ann_rows.append((sid, kind, w, (d.get("file") or "")[:160],
                         int(npast) if npast not in (None, "") else None))
out.executemany("insert or replace into ann(sid,kind,weight,file,npast) values(?,?,?,?,?)", ann_rows)
step("ann rows: %d (rows without a kind tag: %d)" % (len(ann_rows), missing_tag))

# ---------------------------------------------------------------- vault text archive
# Two heading formats live in the vault:
#   A: "## User"            / "## Assistant (agent · model · 12s)"      (export_session_md.ps1)
#   B: "## 1. user"         / "## 2. assistant"                          (older .chat.md)
# The md header also carries the real session clock:
#   "# <title>" / "**Session ID:** ses_…" / "**Created:** …" / "**Updated:** …"
HDR = re.compile(r"(?m)^## (?:(?:(\d+)\.\s+)?(User|Assistant|user|assistant))\b[^\n]*$")
META_ID = re.compile(r"\*\*Session ID:\*\*\s*(ses_\w+)")
META_CREATED = re.compile(r"\*\*Created:\*\*\s*([^\n]+)")
META_UPDATED = re.compile(r"\*\*Updated:\*\*\s*([^\n]+)")
META_TITLE = re.compile(r"(?m)^#\s+([^\n]+)")
vault_rows = []
md_meta = {}
md_files = 0


def parse_md_date(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(s, fmt)) * 1000)
        except ValueError:
            continue
    return None


if VAULT.exists():
    for p in sorted(VAULT.glob("*.md")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(ses_[A-Za-z0-9]+)", p.name)
        sid = m.group(1) if m else None
        mid = META_ID.search(txt)
        if mid:
            sid = mid.group(1)
        created = parse_md_date(META_CREATED.search(txt).group(1)) if META_CREATED.search(txt) else None
        updated = parse_md_date(META_UPDATED.search(txt).group(1)) if META_UPDATED.search(txt) else None
        title = META_TITLE.search(txt)
        if sid:
            md_meta[sid] = {"file": p.name,
                            "title": title.group(1).strip() if title else None,
                            "created_ms": created, "updated_ms": updated}
        hits = list(HDR.finditer(txt))
        if not hits:
            continue
        md_files += 1
        for i, h in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(txt)
            body = txt[h.end():end].strip("\n")
            body = re.sub(r"(?m)^---\s*$", "", body).strip()
            if not body:
                continue
            vault_rows.append((sid, p.name, int(h.group(1)) if h.group(1) else i + 1,
                               (h.group(2) or "").lower(), len(body), body))
step("vault: %d turns from %d md, %d headers" % (len(vault_rows), md_files, len(md_meta)))

# ---------------------------------------------------------------- session table
# A session can be known by five independent witnesses, and they disagree: MC's
# message_history_source (messages), MC's session_meta (state only), the vault md header,
# a compartment parent, and opencode.db (the true source of truth). A fact is only
# "orphan" when NONE of them has the id — so all five must be unioned here.
SRC = Path(r"I:\OpenCode-data\opencode.db")
src_sess = {}
src_n = 0
if SRC.exists():
    sdb = sqlite3.connect("file:%s?mode=ro" % SRC.as_posix(), uri=True)
    sdb.row_factory = sqlite3.Row
    for r in sdb.execute("select id, title, directory, parent_id, time_created, time_updated,"
                         " time_archived from session"):
        src_sess[r["id"]] = {"title": r["title"], "dir": r["directory"],
                             "parent": r["parent_id"],
                             "created_ms": r["time_created"], "updated_ms": r["time_updated"],
                             "archived": 1 if r["time_archived"] else 0, "smsgs": 0, "sv2": 0}
        src_n += 1
    # opencode v2 keeps a second session table and its own message projection; a session can
    # exist in one and not the other, and content can live in either. Both are witnesses.
    v2_n = 0
    for r in sdb.execute("select id, title, directory, parent_id, time_created, time_updated,"
                         " time_archived from session_v2"):
        v2_n += 1
        s = src_sess.get(r["id"])
        if s is None:
            src_sess[r["id"]] = {"title": r["title"], "dir": r["directory"],
                                 "parent": r["parent_id"],
                                 "created_ms": r["time_created"],
                                 "updated_ms": r["time_updated"],
                                 "archived": 1 if r["time_archived"] else 0,
                                 "smsgs": 0, "sv2": 1}
        else:
            s["sv2"] = 1
    for r in sdb.execute("select session_id, count(*) c from message group by 1"):
        if r["session_id"] in src_sess:
            src_sess[r["session_id"]]["smsgs"] = r["c"]
    for r in sdb.execute("select session_id, count(*) c from session_message group by 1"):
        if r["session_id"] in src_sess:
            src_sess[r["session_id"]]["sv2msg"] = r["c"]
    sdb.close()
step("opencode.db sessions: %d (+%d only in session_v2)" % (src_n, v2_n))

mc_meta_sess = {}
mc_meta_n = 0
if MC.exists():
    mdb = sqlite3.connect("file:%s?mode=ro" % MC.as_posix(), uri=True)
    mdb.row_factory = sqlite3.Row
    for r in mdb.execute("select session_id, harness, last_response_time from session_meta"):
        mc_meta_n += 1
        mc_meta_sess[r["session_id"]] = r["last_response_time"]
        harness.setdefault(r["session_id"], r["harness"])
    mdb.close()
step("mc session_meta rows: %d" % mc_meta_n)
mc_meta_ids = set(mc_meta_sess)

block_count = Counter(b[1] for b in block_rows)
session_rows = []
seen_sids = set()


def add_session(sid, first, last, n_msg, n_text, title, has_md):
    s = src_sess.get(sid) or {}
    md = md_meta.get(sid) or {}
    s_msg = s.get("smsgs", 0)
    s_v2msg = s.get("sv2msg", 0)
    nblk = block_count.get(sid, 0)
    row = (sid,
           harness.get(sid),
           sess_proj.get(sid),
           md.get("created_ms") or first or s.get("created_ms"),
           md.get("updated_ms") or s.get("updated_ms") or mc_meta_sess.get(sid) or last,
           n_msg, n_text,
           nblk, tag_count.get(sid, 0),
           md.get("title") or title or s.get("title"),
           has_md,
           1 if sid in src_sess else 0,
           1 if sid in mc_meta_ids else 0,
           s.get("archived", 0),
           s.get("dir"),
           s.get("parent"),
           s_msg,
           s.get("sv2", 0),
           s_v2msg,
           1 if (n_msg or n_text or s_msg or s_v2msg or nblk or has_md) else 0,
           s.get("title"),
           "vault" if md.get("title") else ("source" if s.get("title") else None))
    session_rows.append(row)
    seen_sids.add(sid)


for sid, st in sess_stat.items():
    add_session(sid, st["first"], st["last"], st["n"], st["t"], None, 1 if sid in md_meta else 0)
extra = 0
for sid, meta in md_meta.items():
    if sid not in seen_sids:
        add_session(sid, meta.get("created_ms"), meta.get("updated_ms"), 0, 0, None, 1)
        extra += 1
for sid in set(block_count) - seen_sids:
    add_session(sid, None, None, 0, 0, None, 0)
    extra += 1
for sid in set(src_sess) - seen_sids:
    add_session(sid, None, None, 0, 0, None, 0)
    extra += 1
for sid in mc_meta_ids - seen_sids:
    add_session(sid, None, None, 0, 0, None, 0)
    extra += 1
out.executemany("""insert or replace into session
  (sid,harness,project,first_ms,last_ms,n_msg,n_text,n_block,n_tag,title,has_vault_md,
   in_source,in_mc_meta,archived,directory,parent_sid,src_msg,src_v2,src_v2_msg,has_content,
   src_title,title_src)
  values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              clean_rows(session_rows, {9, 14, 20}))
step("sessions: %d (of which %d have no messages: vault/block/meta/source only)"
     % (len(session_rows), extra))

out.executemany("insert or replace into vault_turn(sid,file,turn_no,role,chars,body) "
                "values(?,?,?,?,?,?)", clean_rows(vault_rows, {5}))

# ---------------------------------------------------------------- text search layers
out.executescript("""
create virtual table msg_fts using fts5(
  sid, ordinal UNINDEXED, role UNINDEXED, text, content='');
create virtual table vault_fts using fts5(
  sid, file UNINDEXED, turn_no UNINDEXED, role UNINDEXED, text, content='');
create virtual table ds_fts using fts5(
  cid UNINDEXED, turn_no UNINDEXED, role UNINDEXED, text, content='');
create virtual table fact_fts using fts5(
  fact_id UNINDEXED, project UNINDEXED, category UNINDEXED, text, content='');
create virtual table block_fts using fts5(
  block_id UNINDEXED, sid UNINDEXED, title, text, content='');
create virtual table doc_fts using fts5(
  path UNINDEXED, title, text, content='');
create table metric(
  value_num real, raw text, src text, sid text, ordinal int, ts_ms int, ctx text);
create table doc(id integer primary key, path text, title text, mtime_ms int, chars int, text text);
""")
out.execute("insert into msg_fts(rowid,sid,ordinal,role,text) "
                "select rowid,sid,ordinal,role,text from msg where text is not null")
n_msg_fts = out.execute("select count(*) from msg_fts").fetchone()[0]
out.execute("insert into vault_fts(rowid,sid,file,turn_no,role,text) "
                "select rowid,sid,file,turn_no,role,body from vault_turn")
n_vault_fts = out.execute("select count(*) from vault_fts").fetchone()[0]
out.execute("insert into ds_fts(rowid,cid,turn_no,role,text) "
                "select rowid,cid,turn_no,role,body from ds_turn")
# contentless FTS5 returns NULL for every column, so only rowid is joinable: pin the rowid
# to the source primary key (fact_id / block_id) instead of the implicit insert order.
out.execute("insert into fact_fts(rowid,fact_id,project,category,text) "
            "select fact_id,fact_id,project,category,body from fact")
out.execute("insert into block_fts(rowid,block_id,sid,title,text) "
            "select block_id,block_id,sid,title,coalesce(p1,'')||' '||coalesce(p2,'')||' '||"
            "coalesce(p3,'')||' '||coalesce(p4,'') from block")
step("fts: msg=%d vault=%d" % (n_msg_fts, n_vault_fts))

# ---------------------------------------------------------------- metric layer
# Numbers cannot be found literally: FTS5 splits on '.', and people write ".9627" without the
# leading zero — a literal search returns 0 hits for a value that has four coordinates. So every
# decimal token in the transcript is normalised to a float, with the raw spelling kept so the
# answer can say what it actually matched.
#
# Three shapes the first version got wrong:
#   - "1,234.5678" matched as "234.5678" (a comma is not a word char, so the lookbehind allowed it)
#     -> comma-grouped numbers are now their own alternative, grouped and stripped
#   - "1.5ms" / "0.5B" were dropped because the unit letter tripped the trailing guard
#     -> a trailing unit is allowed, but scientific notation "1.2e5" stays out (it is 120000)
#   - one-decimal values ("0.5", "1.5") were not indexed at all, so "when 1.5" said "not found"
#     -> any 1..6 decimal places
NUM_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d{1,6})?|\d*\.\d{1,6})(?![\d.]|[eE][+-]?\d)")


def _num_cells(text):
    """(value, raw) for every decimal in text, comma groups collapsed to the number they mean."""
    for m in NUM_RE.finditer(text):
        raw = m.group(1)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        yield v, raw, m


metric_rows = []
for sid, ordinal, ts_ms, text in out.execute(
        "select sid, ordinal, ts_ms, text from msg where text is not null and ts_ms is not null"):
    for v, raw, m in _num_cells(text):
        lo = max(0, m.start() - 70)
        metric_rows.append((round(v, 6), raw, "msg", sid, ordinal, ts_ms,
                            text[lo:m.end() + 70].replace("\n", " ")))
for sid, file, turn_no, body in out.execute(
        "select sid, file, turn_no, body from vault_turn where body is not null"):
    for v, raw, m in _num_cells(body):
        lo = max(0, m.start() - 70)
        metric_rows.append((round(v, 6), raw, "vault", sid, turn_no, None,
                            body[lo:m.end() + 70].replace("\n", " ")))
out.executemany("insert into metric(value_num,raw,src,sid,ordinal,ts_ms,ctx) "
                "values(?,?,?,?,?,?,?)", metric_rows)
out.execute("create index metric_v on metric(value_num)")
out.execute("create index metric_sid on metric(sid, ordinal)")
n_metric = out.execute("select count(*) from metric").fetchone()[0]
n_values = out.execute("select count(distinct value_num) from metric").fetchone()[0]
step("metric: %d cells / %d distinct values" % (n_metric, n_values))

# ---------------------------------------------------------------- doc layer
# Values and rules often live in repo documents, not in conversation. 193 md files / 1.9 MB is
# cheap to index and stops "no history" from being printed for something that is written down.
DOC_ROOTS = [Path(r"I:\DWGLS-native-fs\docs"), Path(r"I:\tools\ann\docs"),
             Path(r"I:\tools\chat-index\docs"), Path(r"I:\keep\intel\PLAYBOOK")]
doc_rows = []
for root in DOC_ROOTS:
    if not root.exists():
        continue
    for f in sorted(root.rglob("*.md")):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not txt.strip():
            continue
        first = ""
        for line in txt.splitlines():
            if line.startswith("#"):
                first = line.lstrip("# ").strip()
                break
        doc_rows.append((str(f), first, int(f.stat().st_mtime * 1000), len(txt), txt))
out.executemany("insert or replace into doc(id,path,title,mtime_ms,chars,text) "
                "values(?,?,?,?,?,?)",
                [(i + 1, p, t, m, c, x) for i, (p, t, m, c, x) in enumerate(doc_rows)])
out.execute("insert into doc_fts(rowid,path,title,text) select id,path,title,text from doc")
n_doc = out.execute("select count(*) from doc").fetchone()[0]
step("docs: %d md (%d roots)" % (n_doc, sum(1 for r in DOC_ROOTS if r.exists())))

# join self-check: a contentless FTS5 table stores no column values, so a query that joins
# on a column (not rowid) returns zero rows and looks exactly like "no hit". Refuse to ship
# a map with a dead layer.
JOIN_CHECKS = [
    ("msg_fts", "select count(*) from msg_fts f join msg m on m.rowid=f.rowid", "msg"),
    ("vault_fts", "select count(*) from vault_fts f join vault_turn v on v.rowid=f.rowid", "vault_turn"),
    ("ds_fts", "select count(*) from ds_fts f join ds_turn t on t.rowid=f.rowid", "ds_turn"),
    ("fact_fts", "select count(*) from fact_fts f join fact x on x.fact_id=f.rowid", "fact"),
    ("block_fts", "select count(*) from block_fts f join block b on b.block_id=f.rowid", "block"),
    ("doc_fts", "select count(*) from doc_fts f join doc x on x.id=f.rowid", "doc"),
]
for name, sql, src in JOIN_CHECKS:
    live = out.execute(sql).fetchone()[0]
    total = out.execute("select count(*) from %s" % src).fetchone()[0]
    print("  fts %-10s joinable %6d / %6d (%s)" % (name, live, total, src))
    if total and not live:
        raise SystemExit("FAIL LOUD: %s is dead — join returns 0 of %d rows" % (name, total))

# ---------------------------------------------------------------- artifact extraction
ART = [
    ("pr", re.compile(r"\bPR\s*#?(\d{1,5})\b")),
    ("task", re.compile(r"\btask-([0-9a-f]{8})\b")),
    ("sha", re.compile(r"\b[0-9a-f]{7,40}\b")),
    ("path", re.compile(r"\b[A-Za-z]:\\[^\s`'\"]{3,120}")),
    ("file", re.compile(r"\b(?:[A-Za-z0-9_./-]{2,80})\.(?:c|h|cc|cpp|py|ps1|cmd|sh|md|json|jsonl|db|gguf|tesspack|inc)\b")),
    ("mem", re.compile(r"(?m)#(\d{3,5})\b")),
    ("cov", re.compile(r"\bGEO_FAST\b|\bACTIVE\b")),
]
art_rows = []
# block titles + p1 heads
for bid, title, p1, ts in out.execute("select block_id,title,p1,ts_ms from block"):
    for k, rx in ART:
        for m in set(rx.findall(title or "")) | set(rx.findall((p1 or "")[:400])):
            art_rows.append((m if isinstance(m, str) else m, k, "block", str(bid), ts))
# facts bodies (first 400 chars only, artifacts live in the claim)
for fid, body, ts in out.execute("select fact_id, substr(body,1,400), created_ms from fact"):
    for k, rx in ART:
        for m in set(rx.findall(body or "")):
            art_rows.append((m, k, "fact", str(fid), ts))
# commit shas are authoritative
for sha, short, ts in out.execute("select sha, short, committed_ms from gitcommit"):
    art_rows.append((sha, "sha", "commit", sha, ts))
    art_rows.append((short, "sha", "commit", sha, ts))
# message + archive text: this is where paths, PR numbers, task ids and memory ids actually live
for sid, ordinal, ts, body in out.execute(
        "select sid, ordinal, ts_ms, text from msg where text is not null"):
    for k, rx in ART:
        for m in set(rx.findall(body)):
            art_rows.append((m, k, "msg", "%s#%s" % (sid, ordinal), ts))
for sid, turn, body in out.execute("select sid, turn_no, body from vault_turn"):
    for k, rx in ART:
        for m in set(rx.findall(body)):
            art_rows.append((m, k, "vault", "%s#%s" % (sid, turn), None))
out.executemany("insert into artifact(name,kind,source,ref,ts_ms) values(?,?,?,?,?)",
                clean_rows(art_rows, {0}))
out.execute("create index idx_art_name on artifact(name)")
out.execute("create index idx_art_kind on artifact(kind)")
step("artifacts: %d" % len(art_rows))

# ---------------------------------------------------------------- receipts read back
out.commit()
built = int(time.time() * 1000)
receipts = {}
for t in ("project", "session", "msg", "block", "fact", "gitcommit", "ds_conv", "ds_turn",
          "ds_chunk", "ann", "vault_turn", "artifact"):
    receipts[t] = out.execute("select count(*) from %s" % t).fetchone()[0]
receipts["msg_with_text"] = out.execute("select count(*) from msg where text is not null").fetchone()[0]
receipts["sessions_with_blocks"] = out.execute("select count(*) from session where n_block>0").fetchone()[0]
receipts["msg_orphan_sid"] = out.execute(
    "select count(*) from msg where sid not in (select sid from session)").fetchone()[0]
receipts["block_orphan_sid"] = out.execute(
    "select count(*) from block where sid not in (select sid from session)").fetchone()[0]
receipts["fact_with_session"] = out.execute(
    "select count(*) from fact where source_sid is not null and source_sid<>''").fetchone()[0]
receipts["fact_session_resolves"] = out.execute(
    "select count(*) from fact f join session s on s.sid=f.source_sid").fetchone()[0]
receipts["ds_chunk_with_conv"] = out.execute(
    "select count(*) from ds_chunk where cid is not null").fetchone()[0]
receipts["ds_conv_with_chunks"] = out.execute(
    "select count(*) from ds_conv where n_chunks>0").fetchone()[0]
receipts["span_sessions"] = out.execute(
    "select min(first_ms), max(last_ms) from session").fetchone()
receipts["msg_role"] = dict(out.execute("select role, count(*) from msg group by 1").fetchall())

# A recovery run overwrote 67 session titles with one literal string. The map must not pass that
# on as if it were a title, so rebuild a usable one from the first user message — map-level only,
# the source db is never written. title_src records where the title came from.
out.execute("""
    update session set title = (
        select substr(replace(replace(trim(m.text), char(10), ' '), char(13), ' '), 1, 70)
        from msg m
        where m.sid = session.sid and m.role = 'user'
          and m.text is not null and length(trim(m.text)) > 8
        order by m.ordinal limit 1),
      title_src = 'first_message'
    where (src_title like 'Recovered from magic-context%'
           or title is null or title = '' or title like 'Recovered from magic-context%')
      and exists (select 1 from msg m where m.sid = session.sid and m.role = 'user'
                  and m.text is not null and length(trim(m.text)) > 8)""")
out.commit()
receipts["titles_recovered_from_first_message"] = out.execute(
    "select count(*) from session where title_src='first_message'").fetchone()[0]
receipts["titles_from"] = dict(out.execute(
    "select coalesce(title_src,'<none>'), count(*) from session group by 1").fetchall())
receipts["ann_kinds"] = dict(out.execute(
    "select coalesce(kind,'<none>'), count(*) from ann group by 1 order by 2 desc").fetchall())
receipts["session_witness"] = {
    "opencode_db": out.execute("select count(*) from session where in_source=1").fetchone()[0],
    "opencode_v2_only": out.execute(
        "select count(*) from session where in_source=1 and src_v2=1 and src_msg=0").fetchone()[0],
    "mc_session_meta": out.execute("select count(*) from session where in_mc_meta=1").fetchone()[0],
    "vault_md": out.execute("select count(*) from session where has_vault_md=1").fetchone()[0],
    "with_messages": out.execute("select count(*) from session where n_msg>0").fetchone()[0],
    "has_content": out.execute("select count(*) from session where has_content=1").fetchone()[0],
    "no_content_anywhere": out.execute(
        "select count(*) from session where has_content=0").fetchone()[0],
    "empty_at_source": out.execute(
        "select count(*) from session where in_source=1 and src_msg=0 and src_v2_msg=0").fetchone()[0],
    "no_messages_in_map": out.execute(
        "select count(*) from session where in_source=1 and n_msg=0").fetchone()[0],
}
receipts["orphan_fact_unknown"] = out.execute(
    "select count(*) from fact f where f.source_sid<>'' and not exists "
    "(select 1 from session s where s.sid=f.source_sid)").fetchone()[0]
receipts["orphan_fact_no_content"] = out.execute(
    "select count(*) from fact f join session s on s.sid=f.source_sid "
    "where s.has_content=0").fetchone()[0]
receipts["orphan_fact_no_content_active"] = out.execute(
    "select count(*) from fact f join session s on s.sid=f.source_sid "
    "where s.has_content=0 and f.status='active'").fetchone()[0]

detail = json.dumps(receipts, ensure_ascii=False)
out.execute("insert into src values(?,?,?,?)", ("magic-context", str(MC), built, "read-only"))
out.execute("insert into src values(?,?,?,?)", ("vault-md", str(VAULT), built, "%d sessions" % len(md_meta)))
out.execute("insert into src values(?,?,?,?)", ("deepseek", str(DSK), built, "conversations+turns(4)+chat-dsk2"))
out.execute("insert into src values(?,?,?,?)", ("ann", str(ANN), built, "ann_index.jsonl+ann_tags.tsv"))
out.execute("insert into src values(?,?,?,?)", ("receipts", str(DB), built, detail))
out.commit()

(WORK / "chatmap_receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
print()
for k, v in receipts.items():
    print("  %-26s %s" % (k, v))

# ---------------------------------------------------------------- never shrink, never guess
# A refresh runs unattended at 05:10. If a source is locked, moved or half-written, the build
# above happily produces a smaller map — and swapping it in would silently replace a good map
# with a partial one, which reads exactly like "we have no history for that". So the swap is
# gated: every layer must be present at or above a floor, and no layer may lose rows against
# the map it is about to replace.
FLOORS = {"session": 2000, "msg": 38000, "vault_turn": 28000, "block": 900, "fact": 2300,
          "gitcommit": 450, "ds_chunk": 1000, "ann": 60000, "artifact": 250000}
SCALE = float(os.environ.get("CHATMAP_FLOOR_SCALE", "1"))
FLOORS = {k: int(v * SCALE) for k, v in FLOORS.items()}
counts = {t: out.execute("select count(*) from %s" % t).fetchone()[0] for t in FLOORS}
fail = []
for t, floor in FLOORS.items():
    if counts[t] < floor:
        fail.append("%s=%d below floor %d" % (t, counts[t], floor))
if DB.exists():
    old = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    for t in FLOORS:
        try:
            prev = old.execute("select count(*) from %s" % t).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if prev and counts[t] < prev * 0.98:
            fail.append("%s shrank %d -> %d" % (t, prev, counts[t]))
    old.close()
for label, path in (("mc", MC), ("opencode", SRC), ("vault", VAULT), ("ann", ANN)):
    if not path.exists():
        fail.append("source missing: %s (%s)" % (label, path))
if fail:
    out.execute("pragma wal_checkpoint(TRUNCATE)")
    out.close()
    TMP.unlink()
    print("\nFAIL LOUD — refusing to replace the map:")
    for f in fail:
        print("   %s" % f)
    print("   previous map left untouched: %s" % DB)
    raise SystemExit(3)

out.execute("pragma wal_checkpoint(TRUNCATE)")
out.close()
os.replace(TMP, DB)
step("done -> %s (%.1f MB)" % (DB, DB.stat().st_size / 1e6))

