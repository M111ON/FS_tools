#!/usr/bin/env python3
"""
convert_tree_export.py — flattens a tree-structured chat export (conversations
with a `mapping` of {id, parent, children, message:{fragments:[{type,content}]}})
into per-conversation JSON arrays of [{"role": "User"/"Assistant", "content": "..."}],
one file per conversation, ready to feed into chat_chunker.py:

    python convert_tree_export.py conversations.json --output-dir converted
    python chat_chunker.py --input "converted/*.json" --strip-tool-json \
        --output-format json --output chunks.json

By default only REQUEST/RESPONSE fragments become turns (the real conversation
text). THINK, FILE, SEARCH/TOOL_SEARCH, TOOL_OPEN/TOOL_FIND fragments are
metadata/tool noise, not prose, and are dropped — use --keep-thinking to fold
THINK fragments into the Assistant turn (prefixed with "_Thinking:_", the same
marker chat_chunker.py's --strip-think already knows how to remove later, so
you can keep-then-strip instead of losing it permanently).

Forking: a node with >1 children is a conversation fork. There's no
"current_node" pointer in this export format to say which branch was kept, so:
  --branches longest (default): follow the single deepest root-to-leaf path
  --branches all: emit every root-to-leaf path as a separate numbered file
"""
import argparse
import json
import re
import sys
from pathlib import Path

TEXT_FRAGMENT_TYPES = {"REQUEST": "User", "RESPONSE": "Assistant"}


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^\w\-]", "", text, flags=re.UNICODE)
    return text[:max_len] or "untitled"


def all_root_to_leaf_paths(mapping: dict, node_id: str) -> list:
    """Iterative DFS (stack-based) so very long linear chains (hundreds+ nodes
    deep) don't blow Python's recursion limit."""
    out = []
    stack = [(node_id, [])]
    while stack:
        nid, path = stack.pop()
        node = mapping.get(nid)
        if not node:
            continue
        path = path + [nid]
        children = node.get("children") or []
        if not children:
            out.append(path)
            continue
        for c in children:
            stack.append((c, path))
    return out


def longest_path(mapping: dict, node_id: str) -> list:
    paths = all_root_to_leaf_paths(mapping, node_id)
    if not paths:
        return []
    return max(paths, key=len)


def find_root(mapping: dict) -> str:
    for nid, node in mapping.items():
        if node.get("parent") is None:
            return nid
    return next(iter(mapping))


def path_to_turns(mapping: dict, path: list, keep_thinking: bool, keep_meta: bool) -> list:
    turns = []
    for nid in path:
        node = mapping.get(nid) or {}
        msg = node.get("message")
        if not msg:
            continue
        fragments = msg.get("fragments") or []
        think_buf = []
        for frag in fragments:
            ftype = frag.get("type")
            if ftype in TEXT_FRAGMENT_TYPES:
                content = frag.get("content") or ""
                if keep_thinking and think_buf:
                    content = "_Thinking:_\n" + "\n".join(think_buf) + "\n\n" + content
                    think_buf = []
                if content.strip():
                    turns.append({"role": TEXT_FRAGMENT_TYPES[ftype], "content": content})
            elif ftype == "THINK":
                if keep_thinking:
                    think_buf.append(frag.get("content") or "")
                # else: dropped
            elif ftype == "FILE" and keep_meta:
                names = ", ".join(f.get("file_name", "?") for f in (frag.get("files") or []))
                turns.append({"role": "User", "content": f"[uploaded file: {names}]"})
            elif ftype in ("SEARCH", "TOOL_SEARCH") and keep_meta:
                titles = "; ".join(r.get("title", "") for r in (frag.get("results") or [])[:5])
                turns.append({"role": "Assistant", "content": f"[search results: {titles}]"})
            # TOOL_OPEN / TOOL_FIND carry no content — always dropped
    return turns


def main():
    ap = argparse.ArgumentParser(description="Flatten tree-structured chat export into per-conversation JSON")
    ap.add_argument("input", help="Path to conversations.json")
    ap.add_argument("--output-dir", default="converted")
    ap.add_argument("--branches", choices=["longest", "all"], default="longest")
    ap.add_argument("--keep-thinking", action="store_true", help="Fold THINK fragments into the Assistant turn")
    ap.add_argument("--keep-meta", action="store_true", help="Keep FILE/SEARCH fragments as short marker turns")
    ap.add_argument("--min-turns", type=int, default=1, help="Skip conversations/branches with fewer turns than this")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for convo in data:
        mapping = convo.get("mapping") or {}
        if not mapping:
            continue
        root = find_root(mapping)
        title = convo.get("title") or "untitled"
        cid = convo.get("id", "")[:8]
        slug = slugify(title)

        if args.branches == "longest":
            paths = [longest_path(mapping, root)]
        else:
            paths = all_root_to_leaf_paths(mapping, root)

        for i, path in enumerate(paths):
            turns = path_to_turns(mapping, path, args.keep_thinking, args.keep_meta)
            if len(turns) < args.min_turns:
                continue
            suffix = f"_{i}" if len(paths) > 1 else ""
            fname = out_dir / f"{slug}-{cid}{suffix}.json"
            fname.write_text(json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8")
            written += 1
            print(f"  {len(turns):>4} turns -> {fname.name}", file=sys.stderr)

    print(f"Wrote {written} file(s) to {out_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
