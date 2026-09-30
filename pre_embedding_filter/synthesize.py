#!/usr/bin/env python3
"""
synthesize.py — LLM synthesis pass over facts.jsonl (memcore roadmap step 1).

Stage M (map):     batch fact candidates -> NVIDIA NIM re-tags each with
                   0-3 canonical terms + writes a 1-2 sentence Thai gist.
Group:             local, by term.
Stage S (reduce):  per term (>=2 gists) -> one dictionary entry
                   {term, definition}; terms with a single gist pass through.

Backend: OpenAI-compatible chat API (default NVIDIA NIM, key from
         NVIDIA_API_KEY env; override with --base-url/--model/--api-key-env).

Checkpointing: every response lands in synth_state.sqlite3 keyed by
         sha256(model+prompt); re-runs skip completed work, so crashes and
         rate limits never cost a full restart.

Usage:
    python synthesize.py [--facts facts.jsonl] [--limit N]
        [--batch-size 10] [--workers 3] [--out dictionary.jsonl]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-nano-30b-a3b"
DEFAULT_KEY_ENV = "NVIDIA_API_KEY"

MAX_CHARS_PER_FACT = 800
MAX_GISTS_PER_TERM = 24
GIST_CAP_IN_PROMPT = 220

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    model TEXT,
    response TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS gists (
    fact_id INTEGER PRIMARY KEY,
    terms TEXT,
    gist TEXT,
    source_file TEXT,
    kind TEXT
);
CREATE TABLE IF NOT EXISTS definitions (
    term TEXT PRIMARY KEY,
    definition TEXT,
    n_sources INTEGER,
    synthesized INTEGER DEFAULT 0
);
"""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class State:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        # single shared connection guarded by self.lock — workers are few,
        # writes are tiny; simplicity beats a per-thread pool here
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(STATE_SCHEMA)
        self.conn.commit()

    def cached(self, key: str):
        with self.lock:
            row = self.conn.execute(
                "SELECT response FROM llm_cache WHERE cache_key=?", (key,)
            ).fetchone()
            return row[0] if row else None

    def put_cache(self, key: str, model: str, response: str):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO llm_cache (cache_key, model, response, created_at) "
                "VALUES (?, ?, ?, ?)", (key, model, response, time.time()))
            self.conn.commit()

    def load_gists(self):
        with self.lock:
            return self.conn.execute(
                "SELECT fact_id, terms, gist, source_file, kind FROM gists ORDER BY fact_id"
            ).fetchall()

    def gist_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM gists").fetchone()[0]

    def replace_gists(self, rows):
        with self.lock:
            self.conn.execute("DELETE FROM gists")
            self.conn.executemany(
                "INSERT INTO gists (fact_id, terms, gist, source_file, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], r[4]) for r in rows])
            self.conn.commit()

    # ---- definitions ----
    def seed_definitions(self, term_groups):
        """term_groups: {term: [(fact_id, gist)]} — insert new terms only."""
        with self.lock:
            for term, gists in term_groups.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO definitions (term, definition, n_sources, synthesized) "
                    "VALUES (?, '', ?, 0)", (term, len(gists)))
            self.conn.commit()

    def pending_terms(self):
        with self.lock:
            return self.conn.execute(
                "SELECT term FROM definitions WHERE synthesized=0 AND n_sources>0 "
                "ORDER BY n_sources DESC").fetchall()

    def definition_row(self, term: str):
        with self.lock:
            return self.conn.execute(
                "SELECT definition, synthesized FROM definitions WHERE term=?",
                (term,)).fetchone()

    def save_definition(self, term: str, definition: str, synthesized: bool):
        with self.lock:
            self.conn.execute(
                "UPDATE definitions SET definition=?, synthesized=? WHERE term=?",
                (definition, 1 if synthesized else 0, term))
            self.conn.commit()

    def insert_gists(self, rows):
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO gists "
                "(fact_id, terms, gist, source_file, kind) VALUES (?,?,?,?,?)",
                rows)
            self.conn.commit()

    def reset_definitions(self):
        """Wipe synthesized definitions so --redo regenerates everything."""
        with self.lock:
            self.conn.execute("DELETE FROM definitions")
            self.conn.commit()

    def export(self, out_path: str) -> int:
        with self.lock:
            rows = self.conn.execute(
                "SELECT term, definition, n_sources, synthesized FROM definitions "
                "WHERE definition != '' ORDER BY n_sources DESC").fetchall()
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "term": r[0], "definition": r[1],
                    "n_sources": r[2],
                    "origin": "llm" if r[3] else "passthrough",
                }, ensure_ascii=False) + "\n")
        return len(rows)


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, system: str, user: str, max_tokens: int = 1400,
             temperature: float = 0.2, retries: int = 5) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                # zen (opencode.ai) sits behind Cloudflare which blocks
                # the default python-urllib UA with error 1010
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "opencode/1.0",
            })
        delay = 2.0
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read().decode("utf-8", errors="replace").strip()
                    # 9router free proxy appends SSE sentinel "data: [DONE]" after JSON
                    if "data:" in raw:
                        raw = raw.split("data:")[0].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # extract first balanced {...} (handles trailing garbage)
                        start = raw.find("{")
                        if start == -1:
                            raise
                        depth = 0
                        instr = esc = False
                        end = -1
                        for i in range(start, len(raw)):
                            ch = raw[i]
                            if esc:
                                esc = False
                            elif instr:
                                if ch == "\\":
                                    esc = True
                                elif ch == '"':
                                    instr = False
                            elif ch == '"':
                                instr = True
                            elif ch == "{":
                                depth += 1
                            elif ch == "}":
                                depth -= 1
                                if depth == 0:
                                    end = i
                                    break
                        if end == -1:
                            raise
                        data = json.loads(raw[start:end + 1])
                content = (data["choices"][0]["message"].get("content") or "").strip()
                # some models burn the whole budget on reasoning and return
                # content: null — treat as a retryable failure, NEVER cache
                if not content:
                    raise ValueError("empty/null completion")
                return content
            except urllib.error.HTTPError as e:
                # retryable: rate-limit / transient server errors
                # 404/410/401 = model not entitled or EOL — NOT retryable on same model
                # (pool-level fallback will try next model)
                if e.code not in (429, 500, 502, 503, 504):
                    raise
                if attempt >= retries - 1:
                    raise
            except (urllib.error.URLError, ConnectionResetError,
                    TimeoutError, OSError, KeyError, json.JSONDecodeError):
                # NOTE: HTTPError subclasses URLError; ConnectionResetError
                # (WinError 10054) arrives as raw OSError — NIM drops
                # connections often enough that this path is load-bearing
                if attempt >= retries - 1:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 60)


class LLMPool:
    """Round-robin over several OpenAI-compatible clients.
    Free tiers rate-limit PER MODEL, so rotating models multiplies
    effective throughput without tripping any single bucket.
    Clients that fail 3x in a row are circuit-broken for the rest of
    the run instead of burning worker time on doomed retries.
    Fallback: chat_with_fallback() tries models sequentially on
    hard failures (404/410/401) so --models acts as a priority list."""

    def __init__(self, clients):
        self.clients = clients
        self._i = 0
        self._lock = threading.Lock()
        self._fails = [0] * len(clients)
        self._dead = set()

    def pick(self) -> LLM:
        with self._lock:
            alive = [c for i, c in enumerate(self.clients)
                     if i not in self._dead]
            if not alive:
                self._dead.clear()
                self._fails = [0] * len(self.clients)
                alive = self.clients
            c = alive[self._i % len(alive)]
            self._i += 1
            return c

    def report(self, client: LLM, ok: bool):
        with self._lock:
            idx = self.clients.index(client)
            if ok:
                self._fails[idx] = 0
                return
            self._fails[idx] += 1
            if self._fails[idx] >= 3 and idx not in self._dead:
                self._dead.add(idx)
                print(f"[pool] circuit-break: {client.model}")

    def chat_with_fallback(self, state, cache_key_fn, system: str, user: str,
                           max_tokens: int = 1400, temperature: float = 0.2,
                           use_cache: bool = True) -> tuple[str, LLM]:
        """Try models in pool order until one succeeds.
        - checks state cache per-model before calling
        - on 404/410/401/400 immediately tries next model (no delay)
        - transient 429/5xx are retried inside LLM.chat() first
        Returns (text, llm_used). Raises last error if all fail."""
        tried = set()
        last_err = None
        # try at most len(clients) distinct models
        for _ in range(len(self.clients)):
            llm = self.pick()
            # avoid infinite loop when dead-set was cleared
            if llm.model in tried and len(tried) >= len(self.clients):
                break
            tried.add(llm.model)
            ckey = cache_key_fn(llm.model)
            if use_cache:
                cached = state.cached(ckey)
                if cached:
                    return cached, llm
            try:
                text = llm.chat(system, user, max_tokens=max_tokens,
                                temperature=temperature)
                # cache under the successful model's key
                state.put_cache(ckey, llm.model, text)
                self.report(llm, True)
                return text, llm
            except urllib.error.HTTPError as e:
                # hard fail → try next model immediately
                if e.code in (400, 401, 403, 404, 410):
                    print(f"  [fallback] {llm.model} -> {e.code} {e.reason}, trying next")
                    self.report(llm, False)
                    last_err = e
                    continue
                # retryable already exhausted inside llm.chat
                self.report(llm, False)
                last_err = e
                raise
            except Exception as e:
                self.report(llm, False)
                last_err = e
                # transient → try next model as well
                if len(tried) < len(self.clients):
                    print(f"  [fallback] {llm.model} -> {e}, trying next")
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("all models exhausted")


def clip(text: str, cap: int = MAX_CHARS_PER_FACT) -> str:
    text = text.strip()
    if len(text) <= cap:
        return text
    head = text[: int(cap * 0.7)]
    tail = text[-int(cap * 0.3):]
    return head + "\n[…]\n" + tail


MAP_SYSTEM = (
    "คุณเป็นเครื่องมือสกัดความรู้จากบันทึกการทำงาน (chat log) ของโปรแกรมเมอร์\n"
    "ตอบเป็น JSON array เท่านั้น ห้ามมีข้อความอื่นนอกจาก JSON"
)

MAP_USER_TMPL = """อ่าน candidate ต่อไปนี้ (บันทึก chat ภาษาไทย/อังกฤษปนกัน)

สำหรับ "แต่ละ" candidate ให้คืน object หนึ่งอัน:
  "i"     = เลข id ที่กำกับไว้ (copy ตรงๆ)
  "terms" = ชื่อเฉพาะที่พบจริงในข้อความ 0-3 อัน (ชื่อ module/function/ระบบ/แนวคิดเทคนิค,
            เขียนตามต้นฉบับ เช่น pogls_dram, KV remap, DRamTile;
            ถ้าเป็นแชทไร้สาระหรือสั่งงานธรรมดา ให้ terms = [] )
  "gist"  = สรุปสาระความรู้/การตัดสินใจ/ผลลัพธ์ 1-2 ประโยค ภาษาไทย
            (ถ้าไม่มีสาระ ให้ gist = "")

candidates:
{blocks}

ตอบ: JSON array เท่านั้น"""


def build_map_prompt(batch):
    blocks = []
    for fid, _, _, content, _kind in batch:
        blocks.append(f"[{fid}]\n{clip(content)}")
    return MAP_USER_TMPL.format(blocks="\n\n".join(blocks))


def extract_json_array(text: str):
    """Pull the FIRST valid JSON array out of a possibly-chatty response.
    Reasoning models may emit deliberation before/around the payload;
    scanning for balanced brackets (string-aware) beats one greedy regex."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    for m in re.finditer(r"\[", text):
        start = m.start()
        depth = 0
        instr = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
            elif instr:
                if ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(text[start:i + 1])
                        if isinstance(arr, list) and arr:
                            return arr
                    except json.JSONDecodeError:
                        pass
                    break
    return None


def run_map(args, state: State, pool: LLMPool, facts):
    todo = []
    seen = set()
    for row in state.conn.execute("SELECT fact_id FROM gists"):
        seen.add(row[0])
    for i, f in enumerate(facts):
        if args.limit and i >= args.limit:
            break
        if f["fact_id"] in seen:
            continue
        todo.append((f["fact_id"], f["source_file"], f["pattern"],
                     f["content"], f["kind"]))

    batches = [todo[i:i + args.batch_size]
               for i in range(0, len(todo), args.batch_size)]
    print(f"[map] {len(todo)} candidates -> {len(batches)} batches "
          f"(model={args.models or args.model}, workers={args.workers})")

    done = 0

    def one_batch(batch):
        user = build_map_prompt(batch)
        # /no_think: Nemotron-family toggle — skips the long deliberation
        # phase that otherwise eats the token budget before emitting JSON
        def _ckey(m): return content_hash(m + "|map|" + user)
        if len(pool.clients) > 1 and not args.fresh:
            # pool-level fallback handles cache per-model internally
            text, llm = pool.chat_with_fallback(state, _ckey,
                MAP_SYSTEM + "\n/no_think", user, max_tokens=2400,
                use_cache=True)
        else:
            llm = pool.pick()
            key = _ckey(llm.model)
            cached = None if args.fresh else state.cached(key)
            text = cached if cached else llm.chat(
                MAP_SYSTEM + "\n/no_think", user, max_tokens=2400)
            if not cached:
                state.put_cache(key, llm.model, text)
                pool.report(llm, True)
        arr = extract_json_array(text)
        if not arr:
            raise ValueError(f"unparseable LLM response ({len(text)} chars)")
        out = {}
        by_id = {str(b[0]): b for b in batch}
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            fid = str(obj.get("i", ""))
            if fid not in by_id:
                continue
            terms = obj.get("terms") or []
            gist = (obj.get("gist") or "").strip()
            if not isinstance(terms, list):
                terms = []
            terms = [str(t).strip() for t in terms if str(t).strip()][:3]
            src = by_id[fid][1]
            kind = by_id[fid][4]
            out[int(fid)] = ("|".join(terms), gist or "", src, kind)
        return batch, out

    def persist(batch, out):
        rows = []
        for fid, _src, _pat, _content, _kind in batch:
            if fid in out:
                terms_s, gist, src_file, kind = out[fid]
            else:
                terms_s, gist, src_file, kind = "", "", _src, _kind
            rows.append((fid, terms_s, gist, src_file, kind))
        state.insert_gists(rows)

    # bounded concurrency with progress prints
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for bi, batch in enumerate(batches):
            fut = ex.submit(one_batch, batch)
            futures[fut] = batch
        for fut in as_completed(futures):
            try:
                batch, out = fut.result()
                persist(batch, out)
            except Exception as e:
                print(f"  ! batch failed: {e}")
            done += len(futures[fut])
            if done % (args.batch_size * 10) < args.batch_size or done == len(todo):
                print(f"  [map] {done}/{len(todo)}")


REDUCE_SYSTEM = (
    "คุณเป็นผู้เรียบเรียงพจนานุกรมศัพท์เทคนิคจากบันทึกการทำงานจริง\n"
    "ตอบเป็นข้อความเปล่า (ไม่ใช่ JSON)"
)

REDUCE_USER_TMPL = """ศัพท์: {term}

บันทึกที่เกี่ยวข้อง (gist จาก chat log จริง):
{gists}

เขียน "definition" สำหรับศัพท์นี้ 2-5 ประโยค ภาษาไทย (คงชื่อเฉพาะ/ภาษาอังกฤษตามต้นฉบับ):
- มันคืออะไร ทำงานอย่างไรในบริบทของงานนี้
- ประเด็น/ข้อจำกัด/ผลลัพธ์สำคัญที่ซ้ำๆ พบในบันทึก
- ใช้เฉพาะข้อมูลจากบันทึก ห้ามเดาสิ่งที่ไม่ปรากฏ

ตอบเป็น definition เท่านั้น"""

BATCH_SYSTEM = (
    "คุณเป็นผู้เรียบเรียงพจนานุกรมศัพท์เทคนิคจากบันทึกการทำงานจริง\n"
    "ตอบเป็น JSON array เท่านั้น ห้ามมีข้อความอื่นนอกจาก JSON"
)

BATCH_USER_TMPL = """เขียน definition ภาษาไทย 1-2 ประโยคให้ "แต่ละ" ศัพท์ต่อไปนี้
(จาก gist บันทึกจริงของมัน ห้ามเดาสิ่งที่ไม่ปรากฏ คงชื่อเฉพาะตามต้นฉบับ)

{blocks}

ตอบ: JSON array [{{"term": "...", "definition": "..."}}] เท่านั้น"""


def build_batch_prompt(terms_gists):
    blocks = []
    for term, gist in terms_gists:
        blocks.append(f"[{term}]\n{gist[:300]}")
    return BATCH_USER_TMPL.format(blocks="\n\n".join(blocks))


def run_reduce(args, state: State, pool: LLMPool):
    gists = {}
    for fid, terms_s, gist, _src, _kind in state.load_gists():
        for t in filter(None, terms_s.split("|")):
            gists.setdefault(t, []).append((fid, gist))

    state.seed_definitions(gists)
    pending = {r[0] for r in state.pending_terms()}
    multi = sorted(t for t in pending if len(gists.get(t, [])) >= 2)
    single = sorted(t for t in pending if 0 < len(gists.get(t, [])) <= 1)
    print(f"[reduce] {len(gists)} unique terms | multi-source: {len(multi)} "
          f"(1 call each) | single-source: {len(single)} (batched)")

    def one_term(term):
        rows = gists[term]
        picked = rows[:MAX_GISTS_PER_TERM]
        gtext = "\n".join(f"- {g[:GIST_CAP_IN_PROMPT]}" for _f, g in picked)
        user = REDUCE_USER_TMPL.format(term=term, gists=gtext)
        def _ckey(m): return content_hash(m + "|reduce|" + user)
        if len(pool.clients) > 1 and not args.fresh:
            # fallback will pick cache or try models sequentially
            try:
                text, _llm = pool.chat_with_fallback(state, _ckey,
                    REDUCE_SYSTEM + "\n/no_think", user,
                    max_tokens=700, temperature=0.3, use_cache=True)
            except Exception:
                raise
        else:
            llm = pool.pick()
            key = _ckey(llm.model)
            cached = None if args.fresh else state.cached(key)
            try:
                if cached:
                    text = cached
                else:
                    text = llm.chat(REDUCE_SYSTEM + "\n/no_think", user,
                                    max_tokens=700, temperature=0.3)
                    state.put_cache(key, llm.model, text)
            except Exception:
                pool.report(llm, False)
                raise
            pool.report(llm, True)
        state.save_definition(term, text.strip(), True)

    def one_single_batch(chunk):
        """Define several single-gist terms in ONE call."""
        pairs = [(t, gists[t][0][1]) for t in chunk]
        user = build_batch_prompt(pairs)
        def _ckey(m): return content_hash(m + "|reducebatch|" + user)
        if len(pool.clients) > 1 and not args.fresh:
            try:
                text, _llm = pool.chat_with_fallback(state, _ckey,
                    BATCH_SYSTEM + "\n/no_think", user,
                    max_tokens=3200, temperature=0.3, use_cache=True)
            except Exception:
                raise
        else:
            llm = pool.pick()
            key = _ckey(llm.model)
            cached = None if args.fresh else state.cached(key)
            try:
                text = cached or llm.chat(BATCH_SYSTEM + "\n/no_think", user,
                                           max_tokens=3200, temperature=0.3)
                if not cached:
                    state.put_cache(key, llm.model, text)
            except Exception:
                pool.report(llm, False)
                raise
            pool.report(llm, True)
        arr = extract_json_array(text)
        got = {}
        if arr:
            by_name = {t: d for t, d in pairs}
            # models often normalize case/spacing ("Standby Replicas" vs
            # "standby replicas") — match case-insensitively, keep our key
            norm = {t.casefold().strip(): t for t in by_name}
            for obj in arr:
                if not isinstance(obj, dict):
                    continue
                term = str(obj.get("term", "")).strip()
                definition = str(obj.get("definition", "")).strip()
                key = norm.get(term.casefold().strip())
                if key and definition:
                    got[key] = definition
        if not got:
            raise ValueError(f"unparseable batch ({len(text)} chars)")
        for t, d in got.items():
            state.save_definition(t, d, True)
        return [t for t in chunk if t not in got]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one_term, t): t for t in multi}
        done = 0
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                print(f"  ! term '{futs[fut]}' failed: {e}")
            done += 1
            if done % 50 == 0 or done == len(multi):
                print(f"  [reduce] multi {done}/{len(multi)}")

        BATCH = 8
        chunks = [single[i:i + BATCH] for i in range(0, len(single), BATCH)]
        futs = {ex.submit(one_single_batch, c): c for c in chunks}
        done = 0
        leftovers = []
        for fut in as_completed(futs):
            try:
                leftovers.extend(fut.result())
            except Exception as e:
                leftovers.extend(futs[fut])
                print(f"  ! batch failed: {e}")
            done += 1
            if done % 10 == 0 or done == len(chunks):
                print(f"  [reduce] single-batches {done}/{len(chunks)}")

        # anything a batch missed gets its own call (or gist passthrough)
        for t in leftovers:
            try:
                one_term(t)
            except Exception as e:
                print(f"  ! leftover '{t}': {e}")
                row = state.definition_row(t)
                if row and row[0] == "" and row[1] == 0 and gists.get(t):
                    state.save_definition(t, gists[t][0][1], False)

    n_left = sum(1 for t in pending
                 if not (state.definition_row(t) or [""])[0])
    print(f"  [reduce] empty definitions remaining: {n_left}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", default="facts.jsonl")
    ap.add_argument("--state", default="synth_state.sqlite3")
    ap.add_argument("--out", default="dictionary.jsonl")
    ap.add_argument("--limit", type=int, default=0,
                    help="only feed first N facts into map stage (smoke test)")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="single model (ignored if --models given)")
    ap.add_argument("--models", default="",
                    help="comma-separated model list — round-robins across "
                         "them to spread per-model free-tier rate limits")
    ap.add_argument("--key-env", default=DEFAULT_KEY_ENV)
    ap.add_argument("--stage", choices=["map", "reduce", "both"], default="both")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore cached LLM responses (re-call, re-cache)")
    ap.add_argument("--redo", action="store_true",
                    help="reduce stage only: reset all definitions and "
                         "regenerate them with the current model")
    args = ap.parse_args()

    api_key = os.environ.get(args.key_env)
    if not api_key:
        sys.exit(f"missing env {args.key_env}")

    if args.models:
        names = [m.strip() for m in args.models.split(",") if m.strip()]
        pool = LLMPool([LLM(args.base_url, m, api_key) for m in names])
        print(f"[pool] {len(names)} models: {', '.join(names)}")
    else:
        pool = LLMPool([LLM(args.base_url, args.model, api_key)])
    state = State(args.state)

    if args.stage in ("map", "both"):
        facts = []
        with open(args.facts, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                r["fact_id"] = len(facts)
                r["content_hash"] = content_hash(r["content"])
                facts.append(r)
        run_map(args, state, pool, facts)
        print(f"[map] gists stored: {state.gist_count()}")

    if args.stage in ("reduce", "both"):
        if args.redo:
            state.reset_definitions()
            print("[reduce] --redo: cleared all definitions")
        run_reduce(args, state, pool)

    n = state.export(args.out)
    print(f"[done] wrote {n} dictionary entries -> {args.out}")


if __name__ == "__main__":
    main()
