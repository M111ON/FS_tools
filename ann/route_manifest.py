"""Integrity helpers for binding a route sidecar to one ANN index epoch."""
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_manifest(sidecar, index, producer_epoch, rows):
    return {
        "v": 1,
        "rows": rows,
        "producer_epoch": producer_epoch,
        "sha256": sha256_file(sidecar),
        "index_rows": rows,
        "index_sha256": sha256_file(index),
    }


def verify_manifest(manifest_path, sidecar, index, rows):
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if m.get("v") != 1 or m.get("rows") != rows or m.get("index_rows") != rows:
        raise ValueError("manifest row binding mismatch")
    if m.get("sha256") != sha256_file(sidecar):
        raise ValueError("sidecar sha256 mismatch")
    if m.get("index_sha256") != sha256_file(index):
        raise ValueError("index sha256 mismatch")
    return m
