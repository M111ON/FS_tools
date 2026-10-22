#!/usr/bin/env python3
"""
obsidian_mem.py — Obsidian vault as vector persistent memory for opencode.

Stores markdown notes from an Obsidian vault in a local SQLite DB with
embeddings, so the agent can semantically search and inject only the relevant
notes into a prompt ("shorter prompt index link").

Storage:
  <vault>/.vector-memory/obsidian_mem.db   (SQLite, embeddings as float32 blobs)

Embedding backends (auto-selected):
  1. llama.cpp server  (/v1/embeddings on :11434, auto-started)   [preferred]
  2. Ollama nomic-embed-text (same OpenAI-compatible endpoint)    [fallback]
  3. Local lexical embedding (hashed bag-of-words, no deps)       [offline]

Usage (run from anywhere):
  python obsidian_mem.py index                  [--vault VAULT] [--force] [--embed {auto,ollama,llama,server,local}]
  python obsidian_mem.py search "QUERY"         [--vault VAULT] [--k N]
  python obsidian_mem.py query  "QUERY"         [--vault VAULT] [--k N]   # prints full content for prompts
  python obsidian_mem.py remember "FACT"        [--vault VAULT] [--tag TAG] [--file NAME]
  python obsidian_mem.py link "TITLE"           [--vault VAULT]           # print [[wikilink]]
  python obsidian_mem.py status                 [--vault VAULT]
  python obsidian_mem.py stop                   [--vault VAULT]           # stop auto-started server

Dependencies: python stdlib + numpy (for cosine similarity).
"""
import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

try:
    import numpy as np
except ImportError:
    print("error: numpy is required.  pip install numpy", file=sys.stderr)
    sys.exit(2)

DEFAULT_VAULT = r"I:\Vaults"
BASE_URL = "http://127.0.0.1:11434"          # llama.cpp server or Ollama
EMBED_URL = BASE_URL + "/v1/embeddings"      # OpenAI-compatible endpoint (both)
OLLAMA_API = BASE_URL + "/api/tags"          # Ollama-specific probe
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024         # bge-m3 dim; local fallback uses 512
LOCAL_DIM = 512
CHUNK_WORDS = 220        # target words per chunk
CHUNK_OVERLAP = 40

# llama.cpp server auto-start (preferred backend)
LLAMA_SERVER = r"I:\llama\llama-b10830-win-vulkan-x64\llama-server.exe"
LLAMA_MODEL  = r"I:\llama\models\bge-m3-q8_0.gguf"


# ---------------------------------------------------------------------------
# paths / db
# ---------------------------------------------------------------------------
def vault_dir(v):
    return os.path.expanduser(v) if v else DEFAULT_VAULT


def norm_root(root):
    return os.path.normcase(os.path.abspath(os.path.expanduser(root)))


def norm_path(p):
    """Canonical path used as the db key — lowercase + normalized separators,
    so the same file written via different root spellings (case or slash
    direction) maps to one row instead of duplicating."""
    return os.path.normcase(os.path.normpath(p))


def resolve_root(args):
    """Pick the memory root: --root wins; else walk up from cwd to the nearest
    project (git worktree/checkout or has .vector-memory); else the vault."""
    r = getattr(args, "root", None)
    if r:
        return norm_root(r)
    d = os.getcwd()
    while True:
        git = os.path.join(d, ".git")
        if os.path.isdir(git) or os.path.isfile(git) or \
           os.path.exists(os.path.join(d, ".vector-memory")):
            return norm_root(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return norm_root(DEFAULT_VAULT)


def db_path(root):
    """Each root gets its own db under the vault's .vector-memory/roots/.
    The vault itself keeps the legacy obsidian_mem.db name."""
    root = norm_root(root)
    if root == norm_root(DEFAULT_VAULT):
        return os.path.join(DEFAULT_VAULT, ".vector-memory", "obsidian_mem.db")
    roots_dir = os.path.join(DEFAULT_VAULT, ".vector-memory", "roots")
    os.makedirs(roots_dir, exist_ok=True)
    safe = re.sub(r"[^\w\-\.]", "_", root).strip("_") or "root"
    return os.path.join(roots_dir, f"{safe}.db")


def connect(root):
    p = db_path(root)
    con = sqlite3.connect(p)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS docs(
        path TEXT PRIMARY KEY,
        title TEXT,
        mtime REAL,
        fhash TEXT,
        nchunks INT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc TEXT,
        idx INT,
        text TEXT,
        emb BLOB)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc)")
    con.commit()
    return con


# ---------------------------------------------------------------------------
# markdown reading / chunking
# ---------------------------------------------------------------------------
MD = re.compile(r"\.md$", re.I)
SKIP_DIRS = {".obsidian", ".git", ".trash", ".vector-memory", ".smart-env",
             "node_modules", "attachments", ".smart-connections"}


def iter_markdown(vault):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if MD.search(f):
                yield norm_path(os.path.join(root, f))


def title_of(path, vault):
    rel = os.path.relpath(path, vault)
    return os.path.splitext(rel)[0].replace("\\", "/")


def read_text(path):
    try:
        with open(path, "rb") as fh:
            b = fh.read()
        for enc in ("utf-8", "utf-16", "cp874", "latin-1"):
            try:
                return b.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return b.decode("utf-8", "replace")
    except OSError:
        return None


def split_chunks(text):
    """Chunk markdown by headings then by word budget."""
    lines = text.splitlines()
    sections = []            # list of (heading, body_lines)
    cur_head = ""
    cur = []
    for ln in lines:
        if re.match(r"^#{1,3}\s", ln):
            if cur:
                sections.append((cur_head, cur))
            cur_head = ln
            cur = []
        else:
            cur.append(ln)
    if cur:
        sections.append((cur_head, cur))

    chunks = []
    for head, body in sections:
        block = "\n".join(body).strip()
        if not block:
            continue
        head_line = (head + "\n") if head else ""
        words = block.split()
        if len(words) <= CHUNK_WORDS:
            chunks.append(head_line + block)
            continue
        # split long section into word-budget pieces with overlap
        i = 0
        while i < len(words):
            end = min(i + CHUNK_WORDS, len(words))
            piece = " ".join(words[i:end])
            chunks.append(head_line + piece)
            if end == len(words):
                break
            i = max(end - CHUNK_OVERLAP, i + 1)
    return chunks


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------
def server_embedding_available():
    """True if any server at :11434 answers the OpenAI /v1/embeddings shape."""
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": ["ping"]}).encode("utf-8")
        req = urllib.request.Request(EMBED_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read().decode("utf-8"))
            return "data" in data and data["data"]
    except Exception:
        return False


def server_pidfile():
    return os.path.join(os.path.dirname(LLAMA_MODEL), "embed-server.pid")


def server_logfile():
    return os.path.join(os.path.dirname(LLAMA_MODEL), "embed-server.log.err")


def _read_pid():
    try:
        with open(server_pidfile()) as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _server_running(pid):
    if not pid:
        return False
    try:
        import ctypes
        PROCESS_QUERY = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _kill_pid(pid):
    try:
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True)
        return True
    except Exception:
        return False


def _start_llama_server():
    """Start llama.cpp embedding server in the background (no blocking).
    Returns the server PID, or None on failure."""
    if not (os.path.exists(LLAMA_SERVER) and os.path.exists(LLAMA_MODEL)):
        print("error: llama.cpp server/model not found — set LLAMA_SERVER / "
              "LLAMA_MODEL or use --embed local", file=sys.stderr)
        return None
    import subprocess
    try:
        log = os.path.join(os.path.dirname(LLAMA_MODEL), "embed-server.log")
        with open(log, "wb"), open(log + ".err", "wb"):
            pass
        p = subprocess.Popen(
            [LLAMA_SERVER, "-m", LLAMA_MODEL, "--embedding",
             "--embd-normalize", "2", "--port", "11434",
             "--host", "127.0.0.1", "-c", "8192", "-ub", "4096",
             "--no-warmup"],
            stdout=open(log, "a"), stderr=open(log + ".err", "a"),
            creationflags=subprocess.CREATE_NO_WINDOW)
        with open(server_pidfile(), "w") as fh:
            fh.write(str(p.pid))
        return p.pid
    except Exception as e:
        print(f"error: could not start llama-server: {e}", file=sys.stderr)
        return None


def _spawn_watcher(pid):
    """Background watcher: kill `pid` after IDLE_SEC of no embedding activity."""
    import subprocess, sys, tempfile
    IDLE_SEC = 60
    logf = server_logfile()
    script = f'''import time,os,ctypes,sys
pid = {pid}
idle = {IDLE_SEC}
logf = {logf!r}
def alive(p):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, p)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False
def mtime():
    try:
        return os.path.getmtime(logf)
    except OSError:
        return 0
last = mtime()
while alive(pid):
    time.sleep(10)
    cur = mtime()
    if cur > last:
        last = cur
    if time.time() - last >= idle:
        os.system("taskkill /PID {pid} /F >nul 2>&1")
        sys.exit(0)
sys.exit(0)
'''
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            tmp = fh.name
        subprocess.Popen([sys.executable, tmp],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _arm_auto_stop(backend, started_pid):
    """If we auto-started the server, arm an idle-based watcher to stop it."""
    if backend == "server" and started_pid:
        _spawn_watcher(started_pid)


def embed_server(texts):
    """Embed via /v1/embeddings (llama.cpp server or Ollama)."""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    return np.array([d["embedding"] for d in data["data"]], dtype=np.float32)


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _bucket(s, salt):
    d = hashlib.blake2b((salt + s).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(d[:2], "big") % LOCAL_DIM


def embed_local(texts):
    """Hashed bag-of-words (unigrams + bigrams) -> LOCAL_DIM unit vectors."""
    out = np.zeros((len(texts), LOCAL_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        vec = out[i]
        toks = _tokens(t)
        for w in toks:
            vec[_bucket(w, "u")] += 1.0
        for j in range(len(toks) - 1):
            vec[_bucket(toks[j] + "~" + toks[j + 1], "b")] += 1.0
        n = np.linalg.norm(vec)
        if n > 0:
            vec /= n
    return out


def make_embedder(backend=None, vault=None):
    """Return (backend, fn, dim, started_pid). started_pid is set only when
    this call auto-started the server (so the caller can arm auto-stop).
    If a server was already running, started_pid is None (don't touch it)."""
    if backend == "local":
        return "local", embed_local, LOCAL_DIM, None
    if backend in ("ollama", "llama", "server"):
        if not server_embedding_available():
            print("error: embedding server not reachable at "
                  f"{BASE_URL} (use --embed local for offline)",
                  file=sys.stderr)
            sys.exit(1)
        return "server", embed_server, EMBED_DIM, None
    # auto: prefer an existing server, else auto-start llama.cpp, else local
    if server_embedding_available():
        return "server", embed_server, EMBED_DIM, None
    pid = _start_llama_server()
    if pid:
        # wait for the server to come up
        import time
        for _ in range(30):
            if server_embedding_available():
                break
            time.sleep(1)
        if server_embedding_available():
            print("(auto-started llama.cpp embedding server pid %d)" % pid,
                  file=sys.stderr)
            return "server", embed_server, EMBED_DIM, pid
    print("(no embedding server at %s — using local lexical embedding)"
          % BASE_URL, file=sys.stderr)
    return "local", embed_local, LOCAL_DIM, None


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------
def cmd_index(args):
    root = resolve_root(args)
    con = connect(root)
    backend, fn, dim, _auto_pid = make_embedder(args.embed, root)
    total = 0
    changed = 0
    # collect changed docs first
    pending = []          # (path, title, mtime, fhash, [chunks])
    for path in iter_markdown(root):
        total += 1
        try:
            st = os.stat(path)
            with open(path, "rb") as fh:
                fhash = hashlib.blake2b(fh.read(), digest_size=16).hexdigest()
        except OSError:
            continue
        row = con.execute("SELECT fhash FROM docs WHERE path=?",
                          (path,)).fetchone()
        if not args.force and row and row[0] == fhash:
            continue  # unchanged
        text = read_text(path)
        if text is None:
            continue
        chunks = split_chunks(text)
        if chunks:
            pending.append((path, title_of(path, root), st.st_mtime,
                            fhash, chunks))
        changed += 1
    # embed all chunks of all changed docs in batches (server call per batch)
    if pending:
        all_chunks = [c for (_p, _t, _m, _h, cs) in pending for c in cs]
        all_embs = np.zeros((len(all_chunks), dim), dtype=np.float32)
        BATCH = 32   # chunks per server call (avoid long HTTP timeouts)
        for b0 in range(0, len(all_chunks), BATCH):
            b1 = min(b0 + BATCH, len(all_chunks))
            all_embs[b0:b1] = fn(all_chunks[b0:b1])
        off = 0
        for path, title, mtime, fhash, chunks in pending:
            n = len(chunks)
            embs = all_embs[off:off + n]
            off += n
            con.execute("DELETE FROM chunks WHERE doc=?", (path,))
            con.execute(
                "INSERT OR REPLACE INTO docs(path,title,mtime,fhash,nchunks) "
                "VALUES(?,?,?,?,?)",
                (path, title, mtime, fhash, n))
            for i, (c, e) in enumerate(zip(chunks, embs)):
                con.execute(
                    "INSERT INTO chunks(doc,idx,text,emb) VALUES(?,?,?,?)",
                    (path, i, c, e.tobytes()))
    # prune docs that no longer exist on disk
    seen = set(p for (p, _t, _m, _h, _c) in pending)
    for (path,) in con.execute("SELECT path FROM docs").fetchall():
        if path not in seen and not os.path.exists(path):
            con.execute("DELETE FROM chunks WHERE doc=?", (path,))
            con.execute("DELETE FROM docs WHERE path=?", (path,))
    # collapse case/separator duplicates left over from older index runs:
    # the same file indexed under different path spellings (I:\\ vs i:\\, / vs \)
    # becomes multiple rows. Keep the canonical row, merge its chunks in, drop
    # the rest.
    canon_map = {}
    for (p,) in con.execute("SELECT path FROM docs").fetchall():
        key = norm_path(p)
        if key not in canon_map:
            canon_map[key] = p
            continue
        target = canon_map[key]
        # prefer the canonical-spelled row as survivor
        if norm_path(p) == p and norm_path(target) != target:
            con.execute("UPDATE chunks SET doc=? WHERE doc=?",
                        (p, target))
            con.execute("DELETE FROM docs WHERE path=?", (target,))
            canon_map[key] = p
        else:
            con.execute("UPDATE chunks SET doc=? WHERE doc=?",
                        (target, p))
            con.execute("DELETE FROM docs WHERE path=?", (p,))
        con.commit()
    con.commit()
    print(f"indexed {changed} changed / {total} files  "
          f"(backend={backend}, dim={dim})  root={root}")
    _arm_auto_stop(backend, _auto_pid)


# ---------------------------------------------------------------------------
# search / query
# ---------------------------------------------------------------------------
def _cosine(db, query_vec, k, backend_dim):
    sims = []
    q = query_vec.reshape(1, -1)
    rows = db.execute("SELECT id, doc, idx, text, emb FROM chunks").fetchall()
    for cid, doc, idx, text, emb in rows:
        v = np.frombuffer(emb, dtype=np.float32)
        if v.shape[0] != q.shape[1]:
            continue
        s = float(np.dot(v, q[0]) /
                  (np.linalg.norm(v) * np.linalg.norm(q[0]) + 1e-12))
        sims.append((s, cid, doc, idx, text))
    sims.sort(reverse=True)
    return sims[:k]


def cmd_search(args, verbose_result=False):
    root = resolve_root(args)
    if not os.path.exists(db_path(root)):
        print(f"error: no index for root {root}. run "
              "'obsidian_mem.py index' first", file=sys.stderr)
        sys.exit(1)
    con = connect(root)
    backend, fn, dim, _auto_pid = make_embedder(args.embed, root)
    qv = fn([args.query])[0]
    results = _cosine(con, qv, args.k, dim)
    if not results:
        _arm_auto_stop(backend, _auto_pid)
        print("(no results)")
        return
    by_doc = {}
    for s, cid, doc, idx, text in results:
        by_doc.setdefault(doc, []).append((s, idx, text))
    # print grouped by doc, sorted by top score
    for doc in sorted(by_doc, key=lambda d: -by_doc[d][0][0]):
        rel = os.path.relpath(doc, root).replace("\\", "/")
        print(f"\n## {rel}  (sim={by_doc[doc][0][0]:.3f})")
        if verbose_result:
            for s, idx, text in by_doc[doc]:
                snippet = " ".join(text.split())
                print(f"  [{idx}] {snippet[:300]}")
        else:
            for s, idx, text in by_doc[doc][:1]:
                snippet = " ".join(text.split())
                print(f"  [{idx}] {snippet[:300]}")
    _arm_auto_stop(backend, _auto_pid)


def cmd_query(args):
    """Search and print full matched chunk text (for injecting into prompts)."""
    root = resolve_root(args)
    if not os.path.exists(db_path(root)):
        print(f"error: no index for root {root}. run "
              "'obsidian_mem.py index' first", file=sys.stderr)
        sys.exit(1)
    con = connect(root)
    backend, fn, dim, _auto_pid = make_embedder(args.embed, root)
    qv = fn([args.query])[0]
    results = _cosine(con, qv, args.k, dim)
    if not results:
        _arm_auto_stop(backend, _auto_pid)
        print("(no results)")
        return
    for s, cid, doc, idx, text in results:
        rel = os.path.relpath(doc, root).replace("\\", "/")
        print(f"--- [{rel} :: {idx}] sim={s:.3f} ---")
        print(text)
        print()
    _arm_auto_stop(backend, _auto_pid)


# ---------------------------------------------------------------------------
# remember (write a fact into the vault)
# ---------------------------------------------------------------------------
def cmd_remember(args):
    vault = norm_root(vault_dir(args.vault))
    con = connect(vault)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    day = time.strftime("%Y-%m-%d")
    fname = args.file or "opencode-remember"
    # sanitize file name
    safe = re.sub(r"[^\w\-]+", "-", fname).strip("-") or "opencode-remember"
    path = norm_path(os.path.join(vault, "Memory", f"{safe}.md"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tag = args.tag or "opencode"
    existing = read_text(path)
    entry = f"- [{stamp}] {args.fact}  #{tag}\n"
    if existing is not None:
        content = existing.rstrip("\n") + "\n" + entry
    else:
        content = (f"# {safe}\n\n"
                   f"**Created:** {stamp}\n**Tags:** #{tag}\n\n---\n\n"
                   f"## Entries\n\n{entry}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    # re-index just this doc so it's immediately searchable
    st = os.stat(path)
    fhash = hashlib.blake2b(open(path, "rb").read(), digest_size=16).hexdigest()
    backend, fn, dim, _auto_pid = make_embedder(args.embed, vault)
    text = read_text(path)
    chunks = split_chunks(text)
    embs = fn(chunks) if chunks else []
    con.execute("DELETE FROM chunks WHERE doc=?", (path,))
    con.execute(
        "INSERT OR REPLACE INTO docs(path,title,mtime,fhash,nchunks) "
        "VALUES(?,?,?,?,?)",
        (path, title_of(path, vault), st.st_mtime, fhash, len(chunks)))
    for i, (c, e) in enumerate(zip(chunks, embs)):
        con.execute("INSERT INTO chunks(doc,idx,text,emb) VALUES(?,?,?,?)",
                    (path, i, c, e.tobytes()))
    con.commit()
    rel = os.path.relpath(path, vault).replace("\\", "/")
    print(f"remembered -> {rel}")
    print(f"  link: [[Memory/{os.path.splitext(os.path.basename(path))[0]}]]")
    _arm_auto_stop(backend, _auto_pid)


def cmd_link(args):
    root = resolve_root(args)
    # find a doc whose title matches
    con = connect(root)
    like = f"%{args.title}%"
    row = con.execute(
        "SELECT path FROM docs WHERE title LIKE ? LIMIT 1", (like,)).fetchone()
    if row:
        rel = os.path.relpath(row[0], root).replace("\\", "/")
        base = os.path.splitext(rel)[0]
        print(f"[[{base}]]")
    else:
        print(f"(no note matches '{args.title}' — index first or create it)")


def cmd_status(args):
    root = resolve_root(args)
    if not os.path.exists(db_path(root)):
        print("root:", root)
        print("db:    (not indexed yet)")
        return
    con = connect(root)
    ndocs = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    nch = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print("root:", root)
    print(f"db:    {db_path(root)}")
    print(f"docs:  {ndocs}")
    print(f"chunks:{nch}")
    if con.execute("SELECT 1 FROM docs LIMIT 1").fetchone():
        row = con.execute("SELECT emb FROM chunks LIMIT 1").fetchone()
        if row:
            dim = len(np.frombuffer(row[0], dtype=np.float32))
            print(f"embed_dim: {dim}")


# ---------------------------------------------------------------------------
# session lifecycle (endsession / newsession)
# ---------------------------------------------------------------------------
SESSION_DIR = "Memory/Sessions"


def _trail_collect(proj, day):
    """Auto-collect this session's work into the trail pool. Session end =
    endsession is the natural trigger, so the next session can query the full
    work trail immediately. Guarded: only fires when a tools/session_trail.py
    exists under the current working directory (or SESSION_TRAIL_TOOL set).
    """
    cwd = os.getcwd()
    tool = os.environ.get("SESSION_TRAIL_TOOL")
    if not tool:
        cand = os.path.join(cwd, "tools", "session_trail.py")
        if os.path.isfile(cand):
            tool = cand
        else:
            return
    import subprocess
    if not os.path.exists(os.path.join(cwd, ".git")):
        git_cmds = []
        print("  [trail] no .git under cwd — skipping commit collect")
    else:
        git_cmds = [["collect-git", "--proj", proj, "--repo", cwd]]
    cmds = ["collect-vault", "--proj", proj, "--date", day]
    for argv in [cmds] + git_cmds:
        try:
            r = subprocess.run([sys.executable, tool] + argv,
                               capture_output=True, text=True,
                               timeout=180, cwd=cwd)
            for ln in (r.stdout or "").splitlines():
                print("  [trail] " + ln)
            if r.returncode and r.stderr:
                last = [l for l in r.stderr.splitlines() if l.strip()]
                print("  [trail] " + (last[-1] if last else "collect failed"))
        except Exception as exc:
            print(f"  [trail] collect error: {exc}")


def cmd_endsession(args):
    """Write a session summary into the vault + index it. Optional --proj name
    tags which project this session was about, so newsession can filter."""
    vault = norm_root(vault_dir(args.vault))
    con = connect(vault)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    day = time.strftime("%Y-%m-%d")
    proj = args.proj or os.path.basename(resolve_root(args))
    fname = f"{day}_{proj}"
    safe = re.sub(r"[^\w\-]+", "-", fname).strip("-") or "session"
    path = norm_path(os.path.join(vault, SESSION_DIR, f"{safe}.md"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = args.summary if args.summary else "(no summary given)"
    entry = (f"## Session ended {stamp}\n"
             f"**Project:** {proj}\n\n{summary}\n")
    existing = read_text(path)
    if existing is not None:
        content = existing.rstrip("\n") + "\n\n" + entry
    else:
        content = (f"# {safe}\n\n"
                   f"**Created:** {stamp}\n"
                   f"**Tags:** #session #{proj}\n\n---\n\n{entry}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    # re-index this doc
    st = os.stat(path)
    fhash = hashlib.blake2b(open(path, "rb").read(), digest_size=16).hexdigest()
    backend, fn, dim, _auto_pid = make_embedder(args.embed, vault)
    text = read_text(path)
    chunks = split_chunks(text)
    embs = fn(chunks) if chunks else []
    con.execute("DELETE FROM chunks WHERE doc=?", (path,))
    con.execute(
        "INSERT OR REPLACE INTO docs(path,title,mtime,fhash,nchunks) "
        "VALUES(?,?,?,?,?)",
        (path, title_of(path, vault), st.st_mtime, fhash, len(chunks)))
    for i, (c, e) in enumerate(zip(chunks, embs)):
        con.execute("INSERT INTO chunks(doc,idx,text,emb) VALUES(?,?,?,?)",
                    (path, i, c, e.tobytes()))
    con.commit()
    rel = os.path.relpath(path, vault).replace("\\", "/")
    print(f"session summary saved -> {rel}")
    print(f"  link: [[Memory/Sessions/{os.path.splitext(os.path.basename(path))[0]}]]")
    print(f"  (new session: run 'newsession' to load recent summaries)")
    _trail_collect(proj, day)
    _arm_auto_stop(backend, _auto_pid)


def cmd_newsession(args):
    """Print the most recent session summaries from the vault, so a new
    session can pick up where the last one left off. Optional --proj filters
    to a specific project."""
    vault = norm_root(vault_dir(args.vault))
    con = connect(vault)
    rows = con.execute(
        "SELECT path, mtime FROM docs WHERE title LIKE 'Memory/Sessions/%' "
        "ORDER BY mtime DESC LIMIT ?", (args.k,)).fetchall()
    if not rows:
        print("(no session summaries yet — run 'endsession \"...\"' when "
              "finishing work)")
        return
    for path, mtime in rows:
        rel = os.path.relpath(path, vault).replace("\\", "/")
        if args.proj and args.proj not in path:
            continue
        text = read_text(path)
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(mtime or 0))
        print(f"\n===== {rel}  ({when}) =====")
        if text:
            print(text.strip())
    print("\n(load this into context to continue where the last session left "
          "off)")


def cmd_stop(args):
    """Stop the auto-started llama.cpp embedding server (free memory)."""
    pid = _read_pid()
    if pid and _server_running(pid):
        _kill_pid(pid)
        print(f"stopped llama.cpp embedding server (pid {pid})")
    else:
        print("(no auto-started embedding server running)")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="obsidian_mem",
                                 description="Obsidian vault vector memory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--vault", default=DEFAULT_VAULT,
                       help=f"vault path (default {DEFAULT_VAULT})")
        p.add_argument("--root", default=None,
                       help="memory root to operate on (default: nearest "
                            "project from cwd, else the vault)")
        p.add_argument("--embed", choices=["auto", "ollama", "llama", "server", "local"],
                       default="auto")

    p = sub.add_parser("index", help="scan + embed + store a root (vault or project)")
    add_common(p)
    p.add_argument("--force", action="store_true", help="re-embed everything")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("search", help="vector search (titles + snippets)")
    add_common(p)
    p.add_argument("query")
    p.add_argument("--k", type=int, default=5)

    p = sub.add_parser("query", help="search + print full content (for prompts)")
    add_common(p)
    p.add_argument("query")
    p.add_argument("--k", type=int, default=3)

    p = sub.add_parser("remember", help="append a fact to a Memory note in the vault")
    add_common(p)
    p.add_argument("fact")
    p.add_argument("--tag", default="opencode")
    p.add_argument("--file", help="note file name (default opencode-remember)")

    p = sub.add_parser("link", help="print [[wikilink]] for a matching note")
    add_common(p)
    p.add_argument("title")

    p = sub.add_parser("status", help="index stats")
    add_common(p)

    p = sub.add_parser("stop", help="stop auto-started embedding server (free mem)")
    add_common(p)

    p = sub.add_parser("endsession", help="save a session summary into the vault")
    add_common(p)
    p.add_argument("summary", nargs="?", default="",
                   help="summary of what this session did")
    p.add_argument("--proj", default=None,
                   help="project name tag (default: current root basename)")

    p = sub.add_parser("newsession", help="print recent session summaries")
    add_common(p)
    p.add_argument("--proj", default=None, help="filter to one project")
    p.add_argument("--k", type=int, default=3)

    args = ap.parse_args()
    if args.cmd == "index":
        cmd_index(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "query":
        cmd_query(args)
    elif args.cmd == "remember":
        cmd_remember(args)
    elif args.cmd == "link":
        cmd_link(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "stop":
        cmd_stop(args)
    elif args.cmd == "endsession":
        cmd_endsession(args)
    elif args.cmd == "newsession":
        cmd_newsession(args)


if __name__ == "__main__":
    main()

