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
        self.ids = []
        self.meta = []
        vecs = []
        with open(index_path, encoding="utf-8") as f:
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
        # precompute normalized V as torch tensor on device
        if _HAS_TORCH:
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            self._dev = dev
            self._Vt = self.rr.Vt  # already normalized, on device
        else:
            self._dev = "cpu"
            self._Vt = None
            self._Vu = self.V / (np.linalg.norm(self.V, axis=1, keepdims=True) + 1e-9)

    def search(self, query, topk=5):
        if isinstance(query, str):
            qv = _embed(query)
        else:
            qv = np.asarray(query, dtype=np.float32)
        qn = qv / (np.linalg.norm(qv) + 1e-9)
        if _HAS_TORCH:
            with torch.no_grad():
                Pt = torch.from_numpy(qn.astype(np.float32)).to(self._dev)
                S = (Pt @ self._Vt.T).cpu().numpy()
        else:
            S = qn @ self._Vu.T
        kk = min(int(topk), self.n)
        idx = np.argpartition(-S, kk - 1)[:kk]
        idx = idx[np.argsort(-S[idx])]
        out = []
        for i in idx:
            out.append({"sid": self.ids[i],
                        "file": self.meta[i]["file"],
                        "npast": self.meta[i]["npast"],
                        "score": float(S[i])})
        return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args()
    s = AnnSearchGPU()
    print(f"loaded {s.n} entries  device={s._dev}")
    t0 = time.perf_counter()
    hits = s.search(args.query, topk=args.topk)
    dt = (time.perf_counter() - t0) * 1000
    print(f"search: {dt:.1f} ms")
    for h in hits:
        print(f"  {h['score']:.4f}  {h['sid'][:12]}  {h['file'][:60]}")
