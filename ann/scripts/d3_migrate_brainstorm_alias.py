"""D3 migration — fold the duplicate `brainstorm` tag into `entry_alias`.

Authorised 2026-10-31 (owner: change the target to brainstorm, ship the alias
table in the SAME transaction as the delete).

Verified preconditions (d3_verify_hash.py, read-only, raw-byte sha256):
  * brainstorm 9,918 rows, every content hash present in v1-migrate (subset)
  * multiplicity 1: each brainstorm hash matches exactly ONE v1-migrate row
  * shared row ids: 0  => the link is content, not id
  * entries_fts has entries_ai/ad/au triggers, so DELETE keeps FTS in sync
  * embeddings has NO foreign key => orphans must be deleted explicitly

Order (one transaction):
  0. backup the 9,918 entries + embeddings to a separate file (full undo)
  1. CREATE entry_alias + index            (entry_id is TEXT, not INTEGER)
  2. INSERT alias 'brainstorm' -> surviving v1-migrate id
  3. DELETE embeddings for the brainstorm ids
  4. DELETE entries where pattern='brainstorm'   (FTS triggers fire)
  5. verify counts, orphan-free, FTS integrity-check
  6. commit  (no VACUUM — pages are reused)

NOTE ON BINDING: content is hashed as raw bytes (text_factory=bytes), but every
id is bound as str. Binding bytes would store a BLOB, and a BLOB never compares
equal to a TEXT id — that silently matched 0 rows on the first attempt.

Run: python d3_migrate.py            # plan only
     python d3_migrate.py --commit   # do it
"""
import hashlib
import os
import sqlite3
import sys

DB = r"I:\tools\cloud_ws\memcore\fact_store.v2.sqlite3"
BACKUP = r"I:\tools\cloud_ws\memcore\brainstorm_backup_20261031.sqlite3"
COMMIT = "--commit" in sys.argv
CHUNK = 500


def sha(b):
    return hashlib.sha256(b).hexdigest()


def chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def qmarks(n):
    return ",".join("?" * n)


# ---------- read the plan (read-only connection) ----------
ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
ro.text_factory = bytes
rows = list(ro.execute("select id, pattern, content from entries"))
ro.close()

bs = [(r[0].decode(), r[2]) for r in rows if r[1] == b"brainstorm"]
v1 = [(r[0].decode(), r[2]) for r in rows if r[1] == b"v1-migrate"]

h_bs = {sha(c): i for i, c in bs}                    # brainstorm hash -> str id
v1_by_hash = {}
for i, c in v1:
    h = sha(c)
    if h in h_bs:
        v1_by_hash.setdefault(h, []).append(i)

missing = [h for h in h_bs if h not in v1_by_hash]
multi = {h: v for h, v in v1_by_hash.items() if len(v) > 1}
print(f"brainstorm rows            : {len(bs)}")
print(f"resolved to a v1-migrate id: {len(v1_by_hash)}")
print(f"unresolved                 : {len(missing)}")
print(f"ambiguous (multi-match)    : {len(multi)}")
if missing or multi:
    print("\nREFUSING: the remap is not a clean 1:1. Nothing was changed.")
    sys.exit(2)

alias_rows = [("brainstorm", v1_by_hash[h][0]) for h in h_bs]
bs_ids = [i for i, _ in bs]
print(f"alias rows to insert       : {len(alias_rows)}")
print(f"entries to delete          : {len(bs_ids)}")
print(f"embeddings to delete       : {len(bs_ids)}")

if not COMMIT:
    print("\nplan only. re-run with --commit")
    sys.exit(0)

# ---------- 0. backup ----------
if os.path.exists(BACKUP):
    try:
        chk = sqlite3.connect(BACKUP)
        n = chk.execute("select count(*) from entries").fetchone()[0]
        chk.close()
    except Exception:
        n = 0
    if n > 0:
        print(f"\nbackup already holds {n} rows, refusing to overwrite: {BACKUP}")
        sys.exit(2)
    os.remove(BACKUP)          # empty schema left by a refused run

bk = sqlite3.connect(BACKUP)
bk.execute("CREATE TABLE entries (id TEXT PRIMARY KEY, source_file TEXT, "
           "pattern TEXT, term_hint TEXT, kind TEXT, content TEXT, ts INTEGER)")
bk.execute("CREATE TABLE embeddings (entry_id TEXT PRIMARY KEY, dim INT, vec BLOB)")
src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
for part in chunks(bs_ids):
    ph = qmarks(len(part))
    bk.executemany(
        "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
        src.execute(f"select id,source_file,pattern,term_hint,kind,content,ts "
                    f"from entries where id in ({ph})", part))
    bk.executemany(
        "INSERT INTO embeddings VALUES (?,?,?)",
        src.execute(f"select entry_id,dim,vec from embeddings "
                    f"where entry_id in ({ph})", part))
bk.commit()
n_e = bk.execute("select count(*) from entries").fetchone()[0]
n_m = bk.execute("select count(*) from embeddings").fetchone()[0]
bk.close()
src.close()
print(f"\nbackup written: {BACKUP}")
print(f"  entries {n_e}   embeddings {n_m}")
if n_e != len(bs_ids) or n_m != len(bs_ids):
    print("REFUSING: backup is incomplete. Nothing was changed.")
    sys.exit(2)

# ---------- 1..6 one transaction ----------
c = sqlite3.connect(DB, timeout=30)
c.execute("PRAGMA foreign_keys=ON")
c.execute("PRAGMA busy_timeout=30000")
pages_before = c.execute("PRAGMA page_count").fetchone()[0]

try:
    c.execute("BEGIN IMMEDIATE")
    c.execute("""CREATE TABLE IF NOT EXISTS entry_alias (
                   alias_tag TEXT NOT NULL,
                   entry_id  TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                   PRIMARY KEY (alias_tag, entry_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alias_entry ON entry_alias(entry_id)")

    c.executemany("INSERT OR IGNORE INTO entry_alias VALUES (?,?)", alias_rows)

    emb_del = 0
    for part in chunks(bs_ids):
        emb_del += c.execute(f"DELETE FROM embeddings WHERE entry_id IN "
                             f"({qmarks(len(part))})", part).rowcount
    ent_del = c.execute("DELETE FROM entries WHERE pattern=?",
                        ("brainstorm",)).rowcount

    # ---- verify before commit ----
    alias_n = c.execute("select count(*) from entry_alias "
                        "where alias_tag='brainstorm'").fetchone()[0]
    left = c.execute("select count(*) from entries where pattern=?",
                     ("brainstorm",)).fetchone()[0]
    orphan = c.execute("select count(*) from embeddings m "
                       "left join entries e on e.id=m.entry_id "
                       "where e.id is null").fetchone()[0]
    dangling = c.execute("select count(*) from entry_alias a "
                         "left join entries e on e.id=a.entry_id "
                         "where e.id is null").fetchone()[0]
    fts_n = c.execute("select count(*) from entries_fts").fetchone()[0]
    ent_n = c.execute("select count(*) from entries").fetchone()[0]

    print(f"\ninside the transaction:")
    print(f"  alias rows inserted   : {alias_n}   (expected {len(alias_rows)})")
    print(f"  brainstorm rows left  : {left}       (expected 0)")
    print(f"  embeddings deleted    : {emb_del}")
    print(f"  entries deleted       : {ent_del}")
    print(f"  orphan embeddings     : {orphan}     (expected 0)")
    print(f"  dangling alias rows   : {dangling}   (expected 0)")
    print(f"  entries / fts rows    : {ent_n} / {fts_n}   (must be equal)")

    ok = (alias_n == len(alias_rows) and left == 0 and orphan == 0
          and dangling == 0 and ent_n == fts_n and ent_del == len(bs_ids))
    if not ok:
        raise RuntimeError("verification failed inside the transaction")

    c.execute("INSERT INTO entries_fts(entries_fts) VALUES('integrity-check')")
    print("  fts integrity-check   : ok")

    c.commit()
except Exception as e:
    c.rollback()
    print(f"\nROLLED BACK: {e}")
    c.close()
    sys.exit(1)

pages_after = c.execute("PRAGMA page_count").fetchone()[0]
free_after = c.execute("PRAGMA freelist_count").fetchone()[0]
print(f"\ncommitted.")
print(f"  pages {pages_before} -> {pages_after} "
      f"(free pages {free_after}, no VACUUM by design)")
c.close()
