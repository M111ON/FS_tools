"""LC_tilecube — geometric retrieval layer (standalone).

Public API:
    from lc import ReadPath, ResidentReranker, rerank_cosine_gpu, rerank_topk_gpu
    from lc import icosphere, Ico, IngestionPipeline

Submodules:
    read_path          read-only layer on top of frozen write path
    rerank_fast        CPU fast rerank (numpy BLAS)
    rerank_gpu         GPU rerank (torch) + ResidentReranker
    ingest_fingerprint ingestion pipeline (write path)
    ico_lifecycle      Ico / level transition
    ico_cost_quality   icosphere geometry
    ico_precompute_order  precompute order asset build
    memcore_dedup      memcore near-dup dedup
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

__version__ = "0.1.0"

from .read_path import ReadPath
from .rerank_fast import rerank_euclid, rerank_dot, rerank_topk
from .rerank_gpu import (rerank_cosine_gpu, rerank_cosine_fp16, rerank_topk_gpu,
                        ResidentReranker)
from .ingest_fingerprint import IngestionPipeline
from .ico_cost_quality import icosphere
from .ico_lifecycle import Ico

__all__ = [
    "ReadPath",
    "ResidentReranker",
    "rerank_cosine_gpu",
    "rerank_cosine_fp16",
    "rerank_topk_gpu",
    "rerank_euclid",
    "rerank_dot",
    "rerank_topk",
    "IngestionPipeline",
    "icosphere",
    "Ico",
]
