import os, sys, json, time, urllib.request, pickle, numpy as np, threading as _t

# Make the bundled CUDA 12.6 toolkit discoverable for cupy on this box.
if "CUDA_PATH" not in os.environ and os.path.isdir(r"I:\\cuda_temp\\bin"):
    os.environ["CUDA_PATH"] = r"I:\\cuda_temp"
    os.environ["PATH"] = r"I:\\cuda_temp\\bin;" + os.environ.get("PATH", "")

# cupy is only imported when ANN_USE_GPU is explicitly opted in.
# Importing it costs ~3s even when unused, so keep it lazy.
_USE_GPU_REQUESTED = os.environ.get("ANN_USE_GPU", "0").lower() in ("1", "true", "yes", "on")
_HAS_CUPY = False
_cp = None
if _USE_GPU_REQUESTED:
    try:
        import cupy as _cp
        _HAS_CUPY = _cp.cuda.runtime.getDeviceCount() > 0
    except Exception:
        _cp = None
        _HAS_CUPY = False

# GPU is WIRED but OFF by default on this box: WDDM single-query matmuls are
# ~3.6x SLOWER than CPU numpy here (measured 2026-11-07: cupy 29.6ms avg vs
# numpy 8.2ms avg on 54261x384). GPU only wins resident multi-pass/batch.
# Set ANN_USE_GPU=1 to force the cupy path (experiments/batch).
_USE_GPU = _HAS_CUPY and _USE_GPU_REQUESTED

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "experiments", "LC_tilecube"))

ANN_INDEX = os.environ.get("ANN_INDEX",
                           "I:/tools/ann/data/memcore/ann_index.jsonl")
EMBED_HOST = os.environ.get("LZ_EMBED_HOST", "127.0.0.1")
EMBED_PORT = os.environ.get("LZ_EMBED_PORT", "18095")

# Persistent keep-alive connection to the embed sidecar (Phase 1 hot path).
# urllib.request.urlopen opens a fresh TCP connection per call (~80-190ms
# overhead); a kept-alive http.client connection drops that to ~15ms.
import http.client as _hc
_EMBED_CONN = {"c": None}
_EMBED_CONN_LOCK = _t.Lock()
MEMCORE_DB = os.environ.get("MEMCORE_DB",
                           "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3")

# Budget default: 5% scan (max_cell/N < budget criterion)
DEFAULT_BUDGET_PCT = float(os.environ.get("ANN_BUDGET_PCT", "5.0"))
IVF_NPROBE = int(os.environ.get("ANN_IVF_NPROBE", "4"))

# --- Icosphere geometry (frozen v1.0) ---
def icosphere(level):
    PHI = (1 + 5 ** 0.5) / 2
    v = np.array([(0,1,PHI),(0,1,-PHI),(0,-1,PHI),(0,-1,-PHI),(1,PHI,0),
                  (1,-PHI,0),(-1,PHI,0),(-1,-PHI,0),(PHI,0,1),(PHI,0,-1),
                  (-PHI,0,1),(-PHI,0,-1)], dtype=float)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    f = np.array([(0,2,8),(0,2,10),(0,4,6),(0,4,8),(0,6,10),(1,3,9),(1,3,11),
                  (1,4,6),(1,4,9),(1,6,11),(2,5,7),(2,5,8),(2,7,10),(3,5,7),
                  (3,5,9),(3,7,11),(4,8,9),(5,8,9),(6,10,11),(7,10,11)], dtype=int)
    for _ in range(level):
        vl = list(v); mc = {}
        def gm(i, j):
            key = (min(i, j), max(i, j))
            if key not in mc:
                m = (vl[i] + vl[j]) / 2; m /= np.linalg.norm(m)
                mc[key] = len(vl); vl.append(m)
            return mc[key]
        nf = []
        for a, b, c in f:
            ab, bc, ca = gm(a, b), gm(b, c), gm(c, a)
            nf += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        f = np.array(nf); v = np.array(vl)
    return v, f

def vert_adj(nv, faces):
    adj = {i: set() for i in range(nv)}
    for f in faces:
        for k in range(3):
            a, b = f[k], f[(k+1)%3]
            adj[a].add(b); adj[b].add(a)
    return adj

def project_3d(X):
    rng = np.random.default_rng(0)
    si = rng.choice(len(X), min(10000, len(X)), replace=False)
    S = X[si].astype(np.float64); mu = S.mean(0)
    w, V = np.linalg.eigh(np.cov((S - mu).T))
    U3 = V[:, np.argsort(w)[::-1][:3]].astype(np.float32)
    mu32 = mu.astype(np.float32)
    P = (X - mu32) @ U3
    return P / np.clip(np.linalg.norm(P, axis=1, keepdims=True), 1e-9, None), U3, mu32

def ico_partition(X, level=3):
    v, f = icosphere(level)
    nv = len(v)
    adj_raw = vert_adj(nv, f)
    P, U3, mu32 = project_3d(X)
    node = np.argmax(P @ v.T.astype(np.float32), axis=1)
    # compact cells (only non-empty)
    valid_verts = [vi for vi in range(nv) if np.any(node == vi)]
    v2c = {vi: ci for ci, vi in enumerate(valid_verts)}
    cells = [np.where(node == vi)[0] for vi in valid_verts]
    cells = [c for c in cells if len(c) > 0]
    # compact adjacency
    adj_compact = {i: set() for i in range(len(cells))}
    for a in valid_verts:
        for b in adj_raw[a]:
            if b in v2c:
                ca, cb = v2c[a], v2c[b]
                if ca != cb:
                    adj_compact[ca].add(cb)
    adj_list = [list(adj_compact[i]) for i in range(len(cells))]
    node_compact = np.array([v2c[n] for n in node], dtype=np.int32)
    # cell centroids
    centroids = np.array([X[c].mean(0) if len(c)>0 else np.zeros(X.shape[1])
                         for c in cells], dtype=np.float32)
    centroids /= np.clip(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-9, None)
    return cells, adj_list, centroids, len(cells)

def ivf_partition(X, K=642, seed=42):
    from sklearn.cluster import KMeans
    km = KMeans(n_clusters=K, random_state=seed, n_init=1, max_iter=100)
    labels = km.fit_predict(X.astype(np.float64))
    centroids = km.cluster_centers_.astype(np.float32)
    centroids /= np.clip(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-9, None)
    valid = [i for i in range(K) if np.any(labels == i)]
    lmap = {old: new for new, old in enumerate(valid)}
    labels_c = np.array([lmap[l] for l in labels], dtype=np.int32)
    cells = [np.where(labels_c == i)[0] for i in range(len(valid))]
    cells = [c for c in cells if len(c) > 0]
    cents = centroids[valid]
    cents /= np.clip(np.linalg.norm(cents, axis=1, keepdims=True), 1e-9, None)
    return cells, cents, len(valid)

def collect_ico(cells, graph, start, max_c, exclude=None):
    a0 = cells[start]
    if len(a0) >= max_c:
        pool = a0[:max_c]
        if exclude is not None:
            pool = pool[pool != exclude]
        return pool, {start}, True
    parts = [a0]; seen = len(a0); visited = {start}
    from collections import deque
    q = deque([start])
    while q and seen < max_c:
        node = q.popleft()
        for nb in graph[node]:
            if nb in visited:
                continue
            visited.add(nb); q.append(nb)
            a = cells[nb]
            take = min(len(a), max_c - seen)
            if take:
                parts.append(a[:take]); seen += take
            if seen >= max_c:
                break
        if seen >= max_c:
            break
    pool = np.concatenate(parts) if len(parts) > 1 else parts[0]
    if exclude is not None:
        pool = pool[pool != exclude]
    return pool, visited, False

def ivf_query(cells, centroids, qvec, nprobe, N, exclude=None):
    if _USE_GPU:
        sims = (_cp.asarray(centroids) @ _cp.asarray(qvec)).get()
    else:
        sims = centroids @ qvec
    probe_ids = np.argsort(-sims)[:nprobe]
    pool = np.concatenate([cells[c] for c in probe_ids if len(cells[c]) > 0])
    if exclude is not None:
        pool = pool[pool != exclude]
    scan_pct = 100.0 * len(pool) / N
    return pool, scan_pct

# --- Embedding cache ---
_EMBED_CACHE = {}
_EMBED_CACHE_MAX = int(os.environ.get("ANN_EMBED_CACHE_MAX", "512"))
try:
    import threading as _t
    _EMBED_CACHE_LOCK = _t.Lock()
except Exception:
    _EMBED_CACHE_LOCK = None

def _get_embed_conn():
    """Return a live keep-alive connection to the embed sidecar, reconnecting
    on failure. Thread-safe."""
    global _EMBED_CONN
    with _EMBED_CONN_LOCK:
        c = _EMBED_CONN.get("c")
        if c is not None:
            try:
                # cheap liveness probe
                c.sock and c.sock.fileno() >= 0
                return c
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
                _EMBED_CONN["c"] = None
        c = _hc.HTTPConnection(EMBED_HOST, int(EMBED_PORT), timeout=30)
        _EMBED_CONN["c"] = c
        return c


def _embed(text):
    try:
        import unicodedata
        norm = unicodedata.normalize("NFKD", text)
        norm = norm.encode("ascii", "ignore").decode("ascii")
    except Exception:
        norm = text
    if _EMBED_CACHE_LOCK is not None:
        _EMBED_CACHE_LOCK.acquire()
    try:
        cached = _EMBED_CACHE.get(norm)
    finally:
        if _EMBED_CACHE_LOCK is not None:
            _EMBED_CACHE_LOCK.release()
    if cached is not None:
        return cached.copy()
    body = json.dumps({"content": norm}).encode()
    for _attempt in (0, 1):
        try:
            conn = _get_embed_conn()
            conn.request("POST", "/embedding", body=body,
                        headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError("embed HTTP %d: %s" % (resp.status, raw[:200]))
            d = json.loads(raw)
            break
        except Exception:
            with _EMBED_CONN_LOCK:
                c = _EMBED_CONN.get("c")
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass
                _EMBED_CONN["c"] = None
            if _attempt == 1:
                raise
    if isinstance(d, list):
        emb = d[0].get("embedding", d[0])
    elif isinstance(d, dict):
        emb = d.get("embedding", d.get("data", [{}])[0].get("embedding", d))
    else:
        emb = d
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        emb = emb[0]
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        emb = emb[0]
    arr = np.asarray(emb, dtype=np.float32)
    try:
        if _EMBED_CACHE_LOCK is not None:
            _EMBED_CACHE_LOCK.acquire()
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
            try:
                del _EMBED_CACHE[next(iter(_EMBED_CACHE))]
            except StopIteration:
                pass
        _EMBED_CACHE[norm] = arr.copy()
    finally:
        if _EMBED_CACHE_LOCK is not None:
            _EMBED_CACHE_LOCK.release()
    return arr

# --- AnnSearchGPU with union (IVF + LC) ---
class AnnSearchGPU:
    def __init__(self, index_path=ANN_INDEX):
        self._index_path = index_path
        self._loaded = False
        self._log = []
        self._log_cap = 1000
        self._last = None
        self._stats = {"queries": 0, "hits_total": 0, "latencies_ms": [],
                       "embed_ms_list": [], "kinds_usage": {}, "devices": {},
                       "errors": 0}
        self._load_lock = None
        try:
            import threading as _t
            self._load_lock = _t.Lock()
        except Exception:
            pass
        # shared text/kind lookup (cached to disk to avoid re-reading the
        # 489MB fact_store on every cold start)
        self._text = {}
        self._kind = {}
        _tc = self._index_path + ".textcache.pkl"
        try:
            _db_mtime = os.path.getmtime(MEMCORE_DB) if os.path.exists(MEMCORE_DB) else 0
            if os.path.exists(_tc) and os.path.getmtime(_tc) >= _db_mtime:
                with open(_tc, "rb") as _f:
                    _blob = pickle.load(_f)
                self._text = _blob.get("text", {})
                self._kind = _blob.get("kind", {})
            elif os.path.exists(MEMCORE_DB):
                import sqlite3
                c = sqlite3.connect(MEMCORE_DB)
                for sid, pattern, content, kind in c.execute(
                        "SELECT id, pattern, content, kind FROM entries"):
                    self._text[sid] = (pattern or "", content or "")
                    self._kind[sid] = kind or ""
                c.close()
                try:
                    with open(_tc, "wb") as _f:
                        pickle.dump({"text": self._text, "kind": self._kind}, _f)
                except Exception:
                    pass
        except Exception:
            pass
        # shared log
        self._shared_log = os.environ.get("ANN_SHARED_LOG",
                                          "I:/tools/ann/data/query_log/all_queries.jsonl")

    def _ensure_loaded(self):
        if self._loaded:
            return
        lock = self._load_lock
        if lock is not None:
            lock.acquire()
        try:
            if self._loaded:
                return
            import pickle
            v_cache = self._index_path + ".V.npy"
            m_cache = self._index_path + ".meta.pkl"
            p_cache = self._index_path + ".partitions.pkl"
            try:
                src_mtime = os.path.getmtime(self._index_path)
                cache_ok = (os.path.exists(v_cache) and os.path.exists(m_cache)
                            and os.path.getmtime(v_cache) >= src_mtime
                            and os.path.getmtime(m_cache) >= src_mtime)
            except Exception:
                cache_ok = False

            if cache_ok:
                try:
                    self.V = np.load(v_cache, allow_pickle=False)
                    with open(m_cache, "rb") as f:
                        blob = pickle.load(f)
                    self.ids = blob["ids"]
                    self.meta = blob["meta"]
                    self._text = blob.get("text", self._text)
                    self._kind = blob.get("kind", self._kind)
                    self.n = len(self.ids)
                    # load partitions if available
                    if os.path.exists(p_cache) and os.path.getmtime(p_cache) >= src_mtime:
                        with open(p_cache, "rb") as f:
                            pblob = pickle.load(f)
                        self._ico_cells = pblob["ico_cells"]
                        self._ico_adj = pblob["ico_adj"]
                        self._ico_centroids = pblob["ico_centroids"]
                        self._ivf_cells = pblob["ivf_cells"]
                        self._ivf_centroids = pblob["ivf_centroids"]
                    else:
                        self._build_partitions()
                    # Set device (needed by MCP wrapper): cupy/CUDA if opted in, else cpu.
                    # Avoid importing torch here — it costs ~5-6s on cold load.
                    if _USE_GPU:
                        self._dev = "cuda"
                    else:
                        self._dev = "cpu"
                    self._loaded = True
                    return
                except Exception:
                    cache_ok = False

            # Cold build
            self.ids = []
            self.meta = []
            vecs = []
            with open(self._index_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    o = json.loads(line)
                    self.ids.append(o.get("sid", ""))
                    self.meta.append({"file": o.get("file", ""),
                                      "npast": o.get("npast", 0),
                                      "dim": o.get("dim", 384)})
                    vecs.append(o["vec"])
            self.V = np.asarray(vecs, dtype=np.float32)
            self.n = len(self.ids)
            self._build_partitions()
            # Set device (needed by MCP wrapper): cupy/CUDA if opted in, else cpu.
            if _USE_GPU:
                self._dev = "cuda"
            else:
                self._dev = "cpu"
            self._loaded = True
            # Write caches
            try:
                np.save(v_cache, self.V)
                with open(m_cache, "wb") as f:
                    pickle.dump({"ids": self.ids, "meta": self.meta,
                                 "text": self._text, "kind": self._kind}, f)
                with open(p_cache, "wb") as f:
                    pickle.dump({"ico_cells": self._ico_cells,
                                 "ico_adj": self._ico_adj,
                                 "ico_centroids": self._ico_centroids,
                                 "ivf_cells": self._ivf_cells,
                                 "ivf_centroids": self._ivf_centroids}, f)
            except Exception:
                pass
        finally:
            if lock is not None:
                lock.release()

    def _build_partitions(self):
        X = self.V.copy()
        Xn = X / np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-9, None)
        self._ico_cells, self._ico_adj, self._ico_centroids, _ = ico_partition(Xn, level=3)
        self._ivf_cells, self._ivf_centroids, _ = ivf_partition(Xn, K=642, seed=42)

    def search(self, query, topk=5, kinds=None, budget_pct=None):
        self._ensure_loaded()
        t0 = time.perf_counter()
        try:
            if isinstance(query, str):
                _embed_t0 = time.perf_counter()
                qvec = _embed(query)
                embed_ms = (time.perf_counter() - _embed_t0) * 1000.0
                _record_query = query
            else:
                qvec = np.asarray(query, dtype=np.float32).reshape(-1)
                embed_ms = 0.0
                _record_query = "<vec>"
            qvec_n = qvec / (np.linalg.norm(qvec) + 1e-9)
            N = self.n
            auto_budget = True
            auto_adjusted = False
            if budget_pct is not None:
                budget = budget_pct
                auto_budget = False
            else:
                budget = DEFAULT_BUDGET_PCT
            budget_used = budget

            _scan_t0 = time.perf_counter()
            # IVF pool (fast, low scan)
            ivf_pool, ivf_scan = ivf_query(self._ivf_cells, self._ivf_centroids,
                                           qvec_n, IVF_NPROBE, N)
            # LC pool (catch missed, remaining budget)
            remaining = budget - ivf_scan
            if remaining > 0:
                max_c = int(N * remaining / 100.0)
                if _USE_GPU:
                    start = int(_cp.argmax(_cp.asarray(self._ico_centroids) @ _cp.asarray(qvec_n)).get())
                else:
                    start = int(np.argmax(self._ico_centroids @ qvec_n))
                lc_pool, _, _ = collect_ico(self._ico_cells, self._ico_adj,
                                            start, max_c, exclude=None)
            else:
                lc_pool = np.array([], dtype=np.int32)
            # Union
            if len(lc_pool) > 0:
                pool = np.unique(np.concatenate([ivf_pool, lc_pool]))
            else:
                pool = np.unique(ivf_pool)
            # Exclude self (query index) — find by cosine 1.0
            # (query may not be in index, but exclude if present)
            # Rerank by exact cosine
            if len(pool) > 0:
                if _USE_GPU:
                    cos = (_cp.asarray(self.V[pool]) @ _cp.asarray(qvec)).get()
                else:
                    cos = self.V[pool] @ qvec
                order = np.argsort(-cos)[:topk]
                top_indices = pool[order]
                # Auto-budget: if cosine gap between #1 and #2 is small (near-duplicate)
                # and we have budget headroom, re-search with higher budget
                if auto_budget and len(cos) >= 2:
                    sorted_cos = np.sort(cos)[::-1]
                    gap = float(sorted_cos[0] - sorted_cos[1])
                    if gap < 0.01 and budget < 20.0:
                        new_budget = min(budget * 2, 20.0)
                        if new_budget > budget:
                            budget_used = new_budget
                            auto_adjusted = True
                            remaining2 = new_budget - ivf_scan
                            if remaining2 > 0:
                                max_c2 = int(N * remaining2 / 100.0)
                                if _USE_GPU:
                                    start2 = int(_cp.argmax(_cp.asarray(self._ico_centroids) @ _cp.asarray(qvec_n)).get())
                                else:
                                    start2 = int(np.argmax(self._ico_centroids @ qvec_n))
                                lc_pool2, _, _ = collect_ico(
                                    self._ico_cells, self._ico_adj, start2, max_c2)
                                if len(lc_pool2) > 0:
                                    pool = np.unique(np.concatenate([ivf_pool, lc_pool2]))
                            if _USE_GPU:
                                cos = (_cp.asarray(self.V[pool]) @ _cp.asarray(qvec)).get()
                            else:
                                cos = self.V[pool] @ qvec
                            order = np.argsort(-cos)[:topk]
                            top_indices = pool[order]
            else:
                top_indices = np.array([], dtype=np.int32)

            hits = []
            for idx in top_indices:
                sid = self.ids[idx]
                file_ = self.meta[idx].get("file", "")
                kind = self._kind.get(sid, "")
                if kinds and kind not in kinds:
                    continue
                pattern, content = self._text.get(sid, ("", ""))
                hits.append({
                    "sid": sid,
                    "file": file_,
                    "npast": self.meta[idx].get("npast", 0),
                    "score": float(self.V[idx] @ qvec / (np.linalg.norm(self.V[idx]) * np.linalg.norm(qvec) + 1e-9)),
                    "kind": kind,
                    "pattern": pattern,
                    "content": (content or "")[:5000],
                })

            embed_ms = (time.perf_counter() - t0) * 1000.0  # approximate
            # split timing: embed is done, scan is the rerank
            # (embed time not separately measured here; use _embed timing)
            scan_ms = 0.0  # placeholder
            scan_ms = (time.perf_counter() - _scan_t0) * 1000.0
            lat = embed_ms + scan_ms
            self._record(_record_query, topk, kinds, lat, len(hits), embed_ms=embed_ms)
            self._last = {
                "latency_ms": round(lat, 2),
                "embed_ms": round(embed_ms, 2),
                "scan_ms": round(scan_ms, 2),
                "hits": len(hits),
                "device": self._dev,
                "budget_pct": budget_used,
                "budget_requested": budget,
                "auto_adjusted": auto_adjusted,
                "scan_actual_pct": round(100.0 * len(pool) / N, 2),
                "ivf_scan_pct": round(ivf_scan, 2),
                "lc_added": len(lc_pool) if len(lc_pool) > 0 else 0,
            }
            footer = (f"[ann] 1 query, {len(hits)} hits | {lat:.2f}ms "
                      f"(budget {budget_used}% scan {100.0*len(pool)/N:.2f}%) | "
                      f"device={self._dev} index={self.n}")
            return {"hits": hits, "n": self.n, "device": self._dev,
                    "footer": footer, "budget_pct": budget_used,
                    "budget_requested": budget, "auto_adjusted": auto_adjusted,
                    "scan_actual_pct": round(100.0 * len(pool) / N, 2)}
        except Exception as e:
            self._stats["errors"] += 1
            lat = (time.perf_counter() - t0) * 1000.0
            self._record(query, topk, kinds, lat, 0, error=str(e))
            self._last = {"latency_ms": round(lat, 2), "embed_ms": 0.0,
                          "scan_ms": 0.0, "hits": 0, "device": self._dev, "error": str(e)}
            return {"hits": [], "n": self.n, "device": self._dev,
                    "footer": f"[ann] 1 query, 0 hits | error: {e}", "error": str(e)}

    def _record(self, qstr, topk, kinds, latency_ms, hits, embed_ms=0.0, error=None):
        import time as _t
        entry = {
            "ts": _t.time(),
            "query": qstr,
            "topk": topk,
            "kinds": kinds if kinds else ["*"],
            "latency_ms": round(latency_ms, 2),
            "embed_ms": round(embed_ms, 2),
            "scan_ms": round(latency_ms - embed_ms, 2),
            "hits": hits,
            "device": self._dev,
            "error": error,
        }
        self._log.append(entry)
        if len(self._log) > self._log_cap:
            self._log = self._log[-self._log_cap:]
        self._stats["queries"] += 1
        self._stats["hits_total"] += hits
        self._stats["latencies_ms"].append(latency_ms)
        self._stats["embed_ms_list"].append(embed_ms)
        for k in (kinds if kinds else ["*"]):
            self._stats["kinds_usage"][k] = self._stats["kinds_usage"].get(k, 0) + 1
        self._stats["devices"]["cpu"] = self._stats["devices"].get("cpu", 0) + 1
        # shared log
        try:
            os.makedirs(os.path.dirname(self._shared_log), exist_ok=True)
            with open(self._shared_log, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                _f.flush()
        except Exception:
            pass

    def stats(self, since=None):
        if not getattr(self, "_loaded", False):
            return {"queries": 0, "hits_total": 0, "avg_latency_ms": 0.0,
                    "avg_embed_ms": 0.0, "avg_scan_ms": 0.0, "p50_ms": 0.0,
                    "p95_ms": 0.0, "p99_ms": 0.0, "max_latency_ms": 0.0,
                    "errors": 0, "kinds_usage": {}, "devices": {},
                    "index_size": 0, "device": "cpu", "recent": []}
        rows = [e for e in self._log if since is None or e["ts"] >= since]
        lats = sorted(e.get("latency_ms", 0) for e in rows)
        if lats:
            n = len(lats)
            avg = sum(lats) / n
            p50 = lats[n // 2]
            p95 = lats[min(int(n * 0.95), n - 1)]
            p99 = lats[min(int(n * 0.99), n - 1)]
        else:
            avg = p50 = p95 = p99 = 0.0
        succ = sum(1 for e in rows if e.get("hits", 0) > 0 and not e.get("error"))
        kinds = {}
        devices = {}
        for e in rows:
            for k in (e.get("kinds") or ["*"]):
                kinds[k] = kinds.get(k, 0) + 1
            d = e.get("device", "cpu")
            devices[d] = devices.get(d, 0) + 1
        return {
            "queries": len(rows),
            "hits_total": sum(e.get("hits", 0) for e in rows),
            "avg_latency_ms": round(avg, 2),
            "avg_embed_ms": round(sum(e.get("embed_ms", 0) for e in rows) / len(rows), 2) if rows else 0.0,
            "avg_scan_ms": round(sum(e.get("scan_ms", 0) for e in rows) / len(rows), 2) if rows else 0.0,
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
            "max_latency_ms": round(max(lats) if lats else 0.0, 2),
            "errors": sum(1 for e in rows if e.get("error")),
            "kinds_usage": kinds,
            "devices": devices,
            "index_size": self.n if hasattr(self, "n") else 0,
            "device": self._dev,
            "recent": rows[-100:],
        }

    def closing_log(self, since=None):
        rows = [e for e in self._log if since is None or e.get("ts", 0) >= since]
        if not rows:
            return "[ann] no queries this turn"
        lats = sorted(e.get("latency_ms", 0) for e in rows)
        n = len(lats)
        p50 = lats[n // 2] if n else 0.0
        p95 = lats[min(int(n * 0.95), n - 1)] if n else 0.0
        p99 = lats[min(int(n * 0.99), n - 1)] if n else 0.0
        avg_embed = sum(e.get("embed_ms", 0) for e in rows) / n if n else 0.0
        avg_scan = sum(e.get("scan_ms", 0) for e in rows) / n if n else 0.0
        kinds = {}
        for e in rows:
            for k in (e.get("kinds") or ["*"]):
                kinds[k] = kinds.get(k, 0) + 1
        kinds_str = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        dev = self._dev
        return (f"[ann] {n} queries, {sum(e.get('hits',0) for e in rows)} hits | "
                f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms "
                f"(embed {avg_embed:.2f}ms / scan {avg_scan:.2f}ms) | "
                f"device={dev} index={self.n} | kinds={kinds_str}")

    def flush_log(self):
        try:
            os.makedirs(os.path.dirname(self._shared_log), exist_ok=True)
            with open(self._shared_log, "a", encoding="utf-8") as f:
                for e in self._log:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                f.flush()
            self._log.clear()
            return True
        except Exception:
            return False

def read_shared_log(limit=200, since=None):
    path = os.environ.get("ANN_SHARED_LOG",
                          "I:/tools/ann/data/query_log/all_queries.jsonl")
    out = []
    # Primary: the shared log written by AnnSearchGPU._log_entry.
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if since is not None and (row.get("ts") or 0) < since:
                    continue
                out.append(row)
    except FileNotFoundError:
        pass
    # Fallback: derive from per-day query logs so history is never empty
    # just because the shared file was missing/rotated.
    if not out:
        import glob as _glob
        for qpath in sorted(_glob.glob("I:/tools/ann/data/query_log/ann_query-*.jsonl")):
            try:
                with open(qpath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        row = {
                            "ts": r.get("ts_ms"),
                            "latency_ms": (r.get("latency_us") or 0) / 1000.0,
                            "hits": len(r.get("top") or []),
                            "kinds": ["*"],
                            "device": "gpu",
                            "error": None,
                        }
                        if since is not None and (row.get("ts") or 0) < since:
                            continue
                        out.append(row)
            except OSError:
                continue
    if limit and len(out) > limit:
        out = out[-limit:]
    return out

def shared_stats(limit=10000, since=None):
    rows = read_shared_log(limit=limit, since=since)
    lats = sorted(e.get("latency_ms", 0) for e in rows)
    if lats:
        n = len(lats)
        avg = sum(lats) / n
        p50 = lats[n // 2]
        p95 = lats[min(int(n * 0.95), n - 1)]
        p99 = lats[min(int(n * 0.99), n - 1)]
    else:
        avg = p50 = p95 = p99 = 0.0
    succ = sum(1 for e in rows if e.get("hits", 0) > 0 and not e.get("error"))
    kinds = {}
    devices = {}
    for e in rows:
        for k in (e.get("kinds") or ["*"]):
            kinds[k] = kinds.get(k, 0) + 1
        d = e.get("device", "cpu")
        devices[d] = devices.get(d, 0) + 1
    return {
        "total": len(rows),
        "successful": succ,
        "success_rate": round(100.0 * succ / len(rows), 1) if rows else 0.0,
        "hits_total": sum(e.get("hits", 0) for e in rows),
        "avg_latency_ms": round(avg, 2),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "max_latency_ms": round(max(lats) if lats else 0.0, 2),
        "errors": sum(1 for e in rows if e.get("error")),
        "kinds_usage": kinds,
        "devices": devices,
        "distinct_queries": len(set(str(e.get("query", "")) for e in rows)),
        "recent": rows[-100:],
    }