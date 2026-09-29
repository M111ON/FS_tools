"""
Example: ready-made ingestion via LangChain, feeding chunks produced by
chat_chunker.py (--output-format json). This replaces a hand-rolled
checkpoint/dedup mechanism with LangChain's built-in Indexing API.

pip install langchain langchain-community langchain-cloudflare SQLAlchemy
(package names/imports for community vector stores shift between releases —
verify the current import path in LangChain's docs before running)
"""
import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_community.embeddings import HuggingFaceEmbeddings  # local bge-m3, fits your CUDA setup
# from langchain_cloudflare import CloudflareVectorize            # newer packaging
from langchain_community.vectorstores import CloudflareVectorize   # or here, depending on version
from langchain.indexes import SQLRecordManager, index

# 1) Load chunks produced by your chat_chunker.py CLI
chunks = json.loads(Path("chunks.json").read_text(encoding="utf-8"))
docs = [
    Document(
        page_content=c["text"],
        # source_id_key below uses this field to know what to skip/delete later
        metadata={"source": ",".join(c["files"]) + f"#chunk{c['index']}"},
    )
    for c in chunks
]

# 2) Embeddings — swap for your NIM API call if you'd rather not run bge-m3 locally
embeddings = HuggingFaceEmbeddings(model_name="BAAI/bge-m3")

# 3) Vector store — Cloudflare Vectorize
vectorstore = CloudflareVectorize(
    embedding=embeddings,
    account_id="<CF_ACCOUNT_ID>",
    vectorize_api_token="<CF_VECTORIZE_TOKEN>",
)
vectorstore.create_index(index_name="pogls-chatlog", wait=True)  # no-op if it already exists

# 4) Record manager — THIS is the checkpoint/dedup piece.
#    A local SQLite file tracks a content hash per doc. Re-running this script
#    with the same (or a superset of) chunks.json will skip anything unchanged.
record_manager = SQLRecordManager(
    namespace="pogls_chatlog",
    db_url="sqlite:///record_manager_cache.sql",
)
record_manager.create_schema()  # safe to call every run, no-op after first time

result = index(
    docs,
    record_manager,
    vectorstore,
    cleanup="incremental",   # "incremental": skip unchanged, delete stale, embed new/changed only
    source_id_key="source",
)
print(result)
# e.g. {'num_added': 42, 'num_updated': 0, 'num_skipped': 9871, 'num_deleted': 3}
# ^ num_skipped is the number you'd want to watch — that's proof re-runs
#   aren't re-embedding (and re-paying for) everything from scratch anymore.
