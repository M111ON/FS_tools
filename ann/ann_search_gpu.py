"""ann_search_gpu.py — GPU/CPU-resident semantic search over ann_index.jsonl.

Loads the ANN index once, keeps V resident (ResidentReranker), embeds the
query via the sidecar, and returns top-k hits by cosine similarity. Works on
CPU (numpy/torch CPU) and GPU (torch CUDA) — falls back automatically.

Usage:
    from ann_search_gpu import AnnSearchGPU
    s = AnnSearchGPU()            # loads index
    hits = s.search("query text", topk=5)   # -> list of dicts
"""
import os, sys, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "experiments", "LC_tilecube"))
import numpy as np

ANN_INDEX = os.environ.get("ANN_INDEX",
                           "I:/tools/ann/data/memcore/ann_index.jsonl")
EMBED_HOST = os.environ.get("LZ_EMBED_HOST", "127.0.0.1")
EMBED_PORT = os.environ.get("LZ_EMBED_PORT", "18095")

try:
    import torch
    _HAS_TORCH = True
except Exception:
    torch = None
    _HAS_TORCH = False

from rerank_gpu import ResidentReranker


def read_shared_log(limit=200):
    """Read the shared append-only query log (all processes). Returns list of
    entries (newest last), capped at `limit`."""
    import json as _j
    path = os.environ.get("ANN_SHARED_LOG",
                          "I:/tools/ann/data/query_log/all_queries.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(_j.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def shared_stats(limit=10000):
    """Aggregate stats over the shared query log (all processes)."""
    rows = read_shared_log(limit=limit)
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
        "distinct_queries": len(set(e.get("query", "") for e in rows)),
        "recent": rows[-20:],
    }


def _embed(text):
    """Embed text via the sidecar (e5-small on :18095). Uses /embedding
    with {"content": text} (same as ann_search.c lz_embed)."""
    try:
        import unicodedata
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode("ascii")
    except Exception:
        pass
    url = f"http://{EMBED_HOST}:{EMBED_PORT}/embedding"
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
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
    return np.asarray(emb, dtype=np.float32)


class AnnSearchGPU:
    def __init__(self, index_path=ANN_INDEX):
        self._index_path = index_path
        self._loaded = False
        # query log (ring buffer) + stats for the per-turn hook / closing log
        self._log = []
        self._log_cap = 1000
        self._last = None  # last call summary for MCP footer
        self._stats = {"queries": 0, "hits_total": 0, "latencies_ms": [],
                       "embed_ms_list": [], "kinds_usage": {}, "devices": {},
                       "errors": 0}
        # hook: print the closing log at process exit (ANN_CLOSING_LOG=1)
        if os.environ.get("ANN_CLOSING_LOG", "") not in ("", "0", "false", "off", "no"):
            import atexit
            atexit.register(lambda: print(self.closing_log(), flush=True))

    def _ensure_loaded(self):
        if self._loaded:
            return
        import pickle
        v_cache = self._index_path + ".V.npy"
        m_cache = self._index_path + ".meta.pkl"
        try:
            import os as _os
            src_mtime = _os.path.getmtime(self._index_path)
            cache_ok = (_os.path.exists(v_cache) and _os.path.exists(m_cache)
                        and _os.path.getmtime(v_cache) >= src_mtime
                        and _os.path.getmtime(m_cache) >= src_mtime)
        except Exception:
            cache_ok = False
        if cache_ok:
            try:
                self.V = np.load(v_cache, allow_pickle=False)
                with open(m_cache, "rb") as f:
                    blob = pickle.load(f)
                self.ids = blob["ids"]
                self.meta = blob["meta"]
                self._text = blob["text"]
                self._kind = blob["kind"]
                self.n = len(self.ids)
                self.rr = ResidentReranker(self.V)
                # precompute normalized V as torch tensor on device
                if _HAS_TORCH:
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                    self._dev = dev
                    self._Vt = self.rr.Vt
                else:
                    self._dev = "cpu"
                    self._Vt = None
                    self._Vu = self.V / (np.linalg.norm(self.V, axis=1, keepdims=True) + 1e-9)
                self._loaded = True
                return
            except Exception:
                cache_ok = False
        # Cold build from JSONL
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
        self.rr = ResidentReranker(self.V)
        self.n = len(self.ids)
        # Optional: join with memcore fact_store to surface usable text.
        self._text = {}
        self._kind = {}
        db_path = os.environ.get("MEMCORE_DB",
                                 "I:/tools/cloud_ws/memcore/fact_store.v2.sqlite3")
        if os.path.exists(db_path):
            try:
                import sqlite3
                c = sqlite3.connect(db_path)
                for sid, pattern, content, kind in c.execute(
                        "SELECT id, pattern, content, kind FROM entries"):
                    self._text[sid] = (pattern or "", content or "")
                    self._kind[sid] = kind or ""
                c.close()
            except Exception:
                self._text = {}
                self._kind = {}
        # precompute normalized V as torch tensor on device
        if _HAS_TORCH:
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            self._dev = dev
            self._Vt = self.rr.Vt  # already normalized, on device
        else:
            self._dev = "cpu"
            self._Vt = None
            self._Vu = self.V / (np.linalg.norm(self.V, axis=1, keepdims=True) + 1e-9)
        self._loaded = True
        # Write cache for next (cold) process
        try:
            np.save(v_cache, self.V)
            with open(m_cache, "wb") as f:
                pickle.dump({"ids": self.ids, "meta": self.meta,
                             "text": self._text, "kind": self._kind}, f)
        except Exception:
            pass

    def search(self, query, topk=5, kinds=None):
        """Search. kinds: list of kinds to include (e.g. ['embedded_document',
        'dictionary_entry']); None = all. Every call is recorded in the query
        log (stats()/closing_log()/flush_log())."""
        self._ensure_loaded()
        t0 = time.perf_counter()
        qstr = query if isinstance(query, str) else "<vector>"
        embed_ms = 0.0
        try:
            if isinstance(query, str):
                te = time.perf_counter()
                qv = _embed(query)
                embed_ms = (time.perf_counter() - te) * 1000.0
            else:
                qv = np.asarray(query, dtype=np.float32)
            qn = qv / (np.linalg.norm(qv) + 1e-9)
            if _HAS_TORCH:
                with torch.no_grad():
                    Pt = torch.from_numpy(qn.astype(np.float32)).to(self._dev)
                    S = (Pt @ self._Vt.T).cpu().numpy()
            else:
                S = qn @ self._Vu.T
            if kinds:
                kindset = set(kinds)
                mask = np.array([self._kind.get(self.ids[i], "") in kindset
                                 for i in range(self.n)], dtype=bool)
                if mask.any():
                    S = np.where(mask, S, -np.inf)
            kk = min(int(topk), self.n)
            idx = np.argpartition(-S, kk - 1)[:kk]
            idx = idx[np.argsort(-S[idx])]
            out = []
            for i in idx:
                sid = self.ids[i]
                pattern, content = self._text.get(sid, ("", ""))
                out.append({"sid": sid,
                            "file": self.meta[i]["file"],
                            "npast": self.meta[i]["npast"],
                            "score": float(S[i]),
                            "kind": self._kind.get(sid, ""),
                            "pattern": pattern,
                            "content": (content[:240] + ("..." if len(content) > 240 else ""))})
            _lat = (time.perf_counter() - t0) * 1000.0
            self._record(qstr, topk, kinds, _lat, len(out), embed_ms=embed_ms)
            self._last = {"latency_ms": round(_lat, 2),
                          "embed_ms": round(embed_ms, 2),
                          "scan_ms": round(_lat - embed_ms, 2),
                          "hits": len(out), "device": getattr(self, "_dev", "cpu")}
            return out
        except Exception as e:
            self._record(qstr, topk, kinds, (time.perf_counter() - t0) * 1000.0, 0,
                         error=str(e), embed_ms=embed_ms)
            self._stats["errors"] += 1
            raise

    def _record(self, query, topk, kinds, latency_ms, hits, error=None, embed_ms=0.0):
        entry = {"ts": time.time(),
                 "query": (query[:80] if isinstance(query, str) else "<vector>"),
                 "topk": int(topk), "kinds": kinds,
                 "latency_ms": round(latency_ms, 2),
                 "embed_ms": round(embed_ms, 2),
                 "scan_ms": round(latency_ms - embed_ms, 2),
                 "hits": int(hits),
                 "device": getattr(self, "_dev", "cpu"),
                 "error": error}
        self._log.append(entry)
        if len(self._log) > self._log_cap:
            self._log = self._log[-self._log_cap:]
        # Persist to a shared append-only log so every process (MCP, dashboard,
        # CLI) contributes to one history. Best-effort; never blocks search.
        try:
            import json as _j
            log_path = os.environ.get("ANN_SHARED_LOG",
                                      "I:/tools/ann/data/query_log/all_queries.jsonl")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as _f:
                _f.write(_j.dumps(entry, ensure_ascii=False) + "\n")
                _f.flush()
        except Exception:
            pass
        self._stats["queries"] += 1
        self._stats["hits_total"] += int(hits)
        self._stats["latencies_ms"].append(latency_ms)
        self._stats.setdefault("embed_ms_list", []).append(embed_ms)
        if len(self._stats["latencies_ms"]) > self._log_cap:
            self._stats["latencies_ms"] = self._stats["latencies_ms"][-self._log_cap:]
        for k in (kinds if kinds else ["*"]):
            self._stats["kinds_usage"][k] = self._stats["kinds_usage"].get(k, 0) + 1
        dev = getattr(self, "_dev", "cpu")
        self._stats["devices"][dev] = self._stats["devices"].get(dev, 0) + 1

    def stats(self, since=None):
        """Aggregate stats, optionally only for log entries with ts >= since.
        Derived from the query log so a per-turn window is exact."""
        if not getattr(self, "_loaded", False):
            return {"queries": 0, "hits_total": 0, "avg_latency_ms": 0.0,
                    "avg_embed_ms": 0.0, "avg_scan_ms": 0.0, "p50_ms": 0.0,
                    "p95_ms": 0.0, "p99_ms": 0.0, "max_latency_ms": 0.0,
                    "errors": 0, "kinds_usage": {}, "devices": {},
                    "index_size": 0, "device": "cpu", "recent": []}
        rows = [e for e in self._log if since is None or e["ts"] >= since]
        lats = sorted(e["latency_ms"] for e in rows)
        embs = [e["embed_ms"] for e in rows]
        if lats:
            n = len(lats)
            avg = sum(lats) / n
            p50 = lats[n // 2]
            p95 = lats[min(int(n * 0.95), n - 1)]
            p99 = lats[min(int(n * 0.99), n - 1)]
        else:
            avg = p50 = p95 = p99 = 0.0
        kinds_usage = {}
        devices = {}
        for e in rows:
            for k in (e["kinds"] if e["kinds"] else ["*"]):
                kinds_usage[k] = kinds_usage.get(k, 0) + 1
            devices[e["device"]] = devices.get(e["device"], 0) + 1
        avg_embed = (sum(embs) / len(embs)) if embs else 0.0
        return {
            "queries": len(rows),
            "hits_total": sum(e["hits"] for e in rows),
            "avg_latency_ms": round(avg, 2),
            "avg_embed_ms": round(avg_embed, 2),
            "avg_scan_ms": round(avg - avg_embed, 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
            "max_latency_ms": round(max(lats) if lats else 0.0, 2),
            "errors": sum(1 for e in rows if e["error"]),
            "kinds_usage": kinds_usage,
            "devices": devices,
            "index_size": self.n,
            "device": getattr(self, "_dev", "cpu"),
            "recent": rows[-5:],
        }

    def closing_log(self, since=None):
        """One-line closing summary (graft-style). since=<ts> for a turn window."""
        st = self.stats(since=since)
        if st["queries"] == 0:
            return ""
        kinds = ", ".join(f"{k}={v}" for k, v in st["kinds_usage"].items()) or "none"
        return (f"[ann] {st['queries']} queries, {st['hits_total']} hits | "
                f"p50={st['p50_ms']}ms p95={st['p95_ms']}ms p99={st['p99_ms']}ms "
                f"(embed {st['avg_embed_ms']}ms / scan {st['avg_scan_ms']}ms) | "
                f"device={st['device']} index={st['index_size']} | kinds={kinds}")

    def flush_log(self, path=None):
        """Append the query log to JSONL (qlog-style) and clear it. Returns count."""
        import json as _json
        if path is None:
            path = os.environ.get("ANN_QUERY_LOG",
                                  "I:/tools/ann/data/query_log/fast_search.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in self._log:
                f.write(_json.dumps(e, ensure_ascii=False) + "\n")
        n = len(self._log)
        self._log = []
        return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--kinds", default=None,
                    help="comma-separated kinds to include (e.g. embedded_document,dictionary_entry)")
    args = ap.parse_args()
    s = AnnSearchGPU()
    print(f"loaded {s.n} entries  device={s._dev}")
    t0 = time.perf_counter()
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    hits = s.search(args.query, topk=args.topk, kinds=kinds)
    dt = (time.perf_counter() - t0) * 1000
    print(f"search: {dt:.1f} ms")
    for h in hits:
        print(f"  {h['score']:.4f}  [{h.get('kind','')}]  {h['sid'][:12]}  {h['file'][:40]}")
        print(f"      pattern: {h.get('pattern','')[:80]}")
        print(f"      content: {h.get('content','')[:120]}")
