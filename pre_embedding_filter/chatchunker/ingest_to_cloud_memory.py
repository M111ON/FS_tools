#!/usr/bin/env python3
"""
ingest_to_cloud_memory.py — feed chat_chunker.py's chunks.json into the
existing cloud-memory-worker (D1 + Vectorize, bge-m3 embeddings) instead of
building a separate LangChain/Vectorize pipeline.

Pre-embedding filter (from the parent pre_embedding_filter pipeline):
every chunk runs through strip_boilerplate() (available-skills blocks,
system XML tags, reasoning-encrypted blobs, skill injections, turn headers)
and a content-hash dedup — duplicates within the batch AND across previous
runs are dropped before they ever reach the embed API or the worker.

Embeds each chunk client-side via Cloudflare Workers AI's REST API using the
SAME model the worker's /search route uses (@cf/baai/bge-m3), so the vector
space matches. Then POSTs batches to the worker's /ingest endpoint.

A local JSON cache (content-hash per chunk id) makes re-runs idempotent AND
cheap: unchanged chunks are skipped entirely (no re-embed, no re-upload) —
this is the checkpoint/dedup piece that was missing before.

Required environment variables (don't pass secrets on the command line):
  CF_ACCOUNT_ID     Cloudflare account id (for the Workers AI REST call)
  CF_API_TOKEN      Cloudflare API token with Workers AI read access
  WORKER_API_KEY    The worker's own API_KEY secret (X-API-Key header)

Usage:
  export CF_ACCOUNT_ID=...
  export CF_API_TOKEN=...
  export WORKER_API_KEY=...
  python ingest_to_cloud_memory.py --chunks chunks.json \
      --worker-url https://cloud-memory-worker.aexid03.workers.dev

  # Force re-embedding everything, ignoring the local cache:
  python ingest_to_cloud_memory.py --chunks chunks.json --no-cache
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import urllib.request
import urllib.error

# pre-embedding filter lives one dir up (parent pipeline)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extractors import strip_boilerplate

EMBED_MODEL = "@cf/baai/bge-m3"
EMBED_BATCH = 20      # texts per Workers AI embedding call
INGEST_BATCH = 100    # chunks per POST /ingest call (matches worker's BATCH_SIZE)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id_for(source_file: str, index: int) -> str:
    # Stable id so re-ingesting the same logical chunk overwrites (D1: INSERT OR
    # REPLACE) instead of duplicating, and so the local cache key is consistent.
    return f"{source_file}#{index}"


def http_json(url: str, payload: dict, headers: dict, method: str = "POST") -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from None


def embed_batch(texts: list, account_id: str, api_token: str) -> list:
    """Returns a list of 1024-dim vectors, one per input text, same order."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{EMBED_MODEL}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    resp = http_json(url, {"text": texts}, headers)
    if not resp.get("success", True) and "result" not in resp:
        raise RuntimeError(f"Workers AI error: {resp}")
    result = resp.get("result", resp)  # some CF responses nest under "result"
    data = result.get("data")
    if data is None:
        raise RuntimeError(f"Unexpected embedding response shape: {list(result.keys())}")
    # data can be [[...], [...]] (batch) or [...] (single) depending on model/input shape
    if len(data) and isinstance(data[0], (int, float)):
        return [data]  # single vector came back flat
    return data


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  [warn] cache file unreadable, starting fresh: {path}", file=sys.stderr)
    return {}


def save_cache(path: Path, cache: dict):
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Embed + ingest chunks.json into cloud-memory-worker")
    ap.add_argument("--chunks", required=True, help="Path to chunks.json (from chat_chunker.py --output-format json)")
    ap.add_argument("--worker-url", default="https://cloud-memory-worker.aexid03.workers.dev")
    ap.add_argument("--cache-file", default=".ingest_cache.json", help="Local checkpoint file")
    ap.add_argument("--no-cache", action="store_true", help="Ignore cache — re-embed and re-upload everything")
    ap.add_argument("--dry-run", action="store_true", help="Embed nothing, just show what would be sent")
    args = ap.parse_args()

    account_id = os.environ.get("CF_ACCOUNT_ID")
    api_token = os.environ.get("CF_API_TOKEN")
    worker_key = os.environ.get("WORKER_API_KEY")
    if not args.dry_run and not (account_id and api_token and worker_key):
        print("Missing env vars: CF_ACCOUNT_ID, CF_API_TOKEN, WORKER_API_KEY are all required "
              "(unless --dry-run).", file=sys.stderr)
        sys.exit(1)

    chunks = json.loads(Path(args.chunks).read_text(encoding="utf-8"))
    cache_path = Path(args.cache_file)
    cache = {} if args.no_cache else load_cache(cache_path)

    # Decide what actually needs (re-)embedding, through the filter:
    # strip boilerplate first, then drop empty / content-hash duplicates
    # (within this batch AND against every run already in the cache).
    todo = []
    skipped = 0
    n_empty = 0
    n_dup = 0
    seen = cache.setdefault("_seen", {})  # text-hash -> first ingested chunk id
    pending = set()  # hashes queued in THIS batch, not yet ingested
    for c in chunks:
        files = c.get("files") or ["unknown"]
        source_file = "+".join(sorted(files))[:200]
        cid = chunk_id_for(source_file, c["index"])
        text = strip_boilerplate(c["text"])
        if not text.strip():
            n_empty += 1
            continue
        h = sha256(text)
        owner = seen.get(h)
        if (owner is not None and owner != cid) or h in pending:
            n_dup += 1  # same content already ingested (or queued) under another chunk id
            continue
        if not args.no_cache and cache.get(cid) == h:
            skipped += 1
            seen.setdefault(h, cid)
            continue
        todo.append({"id": cid, "source_file": source_file, "chunk_index": c["index"],
                     "text": text, "hash": h})
        pending.add(h)

    print(f"Total chunks: {len(chunks)} | unchanged (skipped): {skipped} | "
          f"filtered (boilerplate-only: {n_empty}, dup: {n_dup}) | to embed+ingest: {len(todo)}",
          file=sys.stderr)

    if args.dry_run:
        for t in todo[:5]:
            print(f"  would ingest: {t['id']}  ({len(t['text'])} chars)", file=sys.stderr)
        if len(todo) > 5:
            print(f"  ... and {len(todo) - 5} more", file=sys.stderr)
        return

    if not todo:
        print("Nothing to do — everything already ingested and unchanged.", file=sys.stderr)
        return

    ingest_url = args.worker_url.rstrip("/") + "/ingest"
    ingest_headers = {"Content-Type": "application/json", "X-API-Key": worker_key}

    total_inserted = 0
    for i in range(0, len(todo), INGEST_BATCH):
        batch = todo[i:i + INGEST_BATCH]

        # Embed this batch's texts in sub-batches (Workers AI has its own input limits)
        vectors = []
        for j in range(0, len(batch), EMBED_BATCH):
            sub = batch[j:j + EMBED_BATCH]
            vecs = embed_batch([t["text"] for t in sub], account_id, api_token)
            if len(vecs) != len(sub):
                raise RuntimeError(f"Embedding count mismatch: sent {len(sub)}, got {len(vecs)}")
            vectors.extend(vecs)
            time.sleep(0.05)  # gentle pacing

        payload = [
            {"id": t["id"], "source_file": t["source_file"], "chunk_index": t["chunk_index"],
             "text": t["text"], "vector": v}
            for t, v in zip(batch, vectors)
        ]
        resp = http_json(ingest_url, payload, ingest_headers)
        inserted = resp.get("inserted", 0)
        total_inserted += inserted
        print(f"  ingested batch {i // INGEST_BATCH + 1}: {inserted} chunks", file=sys.stderr)

        # Only mark as cached AFTER a successful ingest response
        for t in batch:
            cache[t["id"]] = t["hash"]
            seen.setdefault(t["hash"], t["id"])  # content-hash dedup for future runs
        save_cache(cache_path, cache)  # save incrementally so a crash mid-run doesn't lose progress

    print(f"Done. Inserted/updated {total_inserted} chunks. Cache: {cache_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
