#!/usr/bin/env python3
"""
main.py — batch-process a folder of exported chat-log .md files into
deduped, pattern-tagged fact candidates ready for an LLM synthesis pass.

Usage:
    python3 main.py <input_dir> [--db state.sqlite3] [--out facts.jsonl] [--force]

Safe to re-run: unchanged files are skipped (by content hash), and turn
content already seen in a previously-processed file (e.g. the shared
prefix of a forked chat) is skipped everywhere it recurs.
"""
import argparse
import sys
from dataclasses import replace
from pathlib import Path

from parser import parse_file, merge_consecutive_assistant_turns
from detector import classify
from extractors import strip_boilerplate, extract
from store import Store


def process_file(path: str, store: Store) -> dict:
    pf = parse_file(path)

    # classify BEFORE any stripping/merging — the detector needs the raw
    # <available-skills> / dispatch-spec markers still intact
    cls = classify(pf)

    merged = merge_consecutive_assistant_turns(pf.turns)

    # strip boilerplate on fresh copies — merge_consecutive_assistant_turns
    # reuses the same Turn objects for non-merged entries, so mutating
    # .content in place would silently corrupt pf.turns too
    merged = [replace(t, content=strip_boilerplate(t.content)) for t in merged]

    # global turn-level dedup — this is what makes fork children cheap:
    # the shared prefix was already recorded by whichever file was
    # processed first, so it's skipped here regardless of pattern.
    survivors = []
    dup_count = 0
    for t in merged:
        if not t.content:
            continue
        if store.turn_seen(t.content):
            dup_count += 1
            continue
        store.record_turn(t.content, path, t.index)
        survivors.append(t)

    fact_dicts = extract(pf, survivors, cls.pattern)

    for fd in fact_dicts:
        store.add_fact(
            source_file=path,
            pattern=cls.pattern,
            content=fd["content"],
            kind=fd["kind"],
            term_hint=fd.get("term_hint"),
        )

    store.mark_file(path, cls.pattern, len(pf.turns))
    store.commit()

    return {
        "path": path,
        "pattern": cls.pattern,
        "reason": cls.reason,
        "raw_turns": len(pf.turns),
        "merged_turns": len(merged),
        "unique_turns": len(survivors),
        "duplicate_turns": dup_count,
        "facts_extracted": len(fact_dicts),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir")
    ap.add_argument("--db", default="fact_pipeline_state.sqlite3")
    ap.add_argument("--out", default="facts.jsonl")
    ap.add_argument("--force", action="store_true", help="reprocess even if unchanged")
    ap.add_argument("--ext", default=".md")
    args = ap.parse_args()

    store = Store(args.db)
    files = sorted(str(p) for p in Path(args.input_dir).glob(f"*{args.ext}"))

    if not files:
        print(f"No {args.ext} files found in {args.input_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"{'file':<60} {'pattern':<11} {'raw':>5} {'uniq':>5} {'dup':>5} {'facts':>6}")
    print("-" * 100)

    for path in files:
        if not args.force and store.file_already_processed(path):
            print(f"{Path(path).name:<60} {'(skip: unchanged)':<11}")
            continue
        result = process_file(path, store)
        print(
            f"{Path(path).name:<60} {result['pattern']:<11} "
            f"{result['raw_turns']:>5} {result['unique_turns']:>5} "
            f"{result['duplicate_turns']:>5} {result['facts_extracted']:>6}"
        )

    n = store.export_facts_jsonl(args.out)
    stats = store.stats()
    print("-" * 100)
    print(f"Total: {stats['files']} files, {stats['unique_turns']} unique turns, "
          f"{stats['facts']} fact candidates -> wrote {n} lines to {args.out}")


if __name__ == "__main__":
    main()
