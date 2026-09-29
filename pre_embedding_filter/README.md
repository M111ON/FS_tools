# Chat Log Fact-Candidate Pipeline

Parses exported chat-log `.md` files (state.db export format) into clean,
deduped, pattern-tagged **fact candidates** ready for an LLM synthesis pass
(term extraction + dictionary definitions).

Does **not** call an LLM. It's stage 1-3 only:
`parse → dedupe → classify → extract candidates → facts.jsonl`

Re-running is free — unchanged files are skipped, and duplicate turn content
(e.g. the shared prefix of a forked chat) is only ever counted once, no
matter how many files it appears in.

## Usage

```bash
pip install --break-system-packages -r requirements.txt  # (stdlib only, no deps actually needed)
python3 main.py /path/to/chat_logs --db state.sqlite3 --out facts.jsonl
```

Re-run any time — add new files to the folder and run the same command
again. Already-processed files are skipped by content hash; new files are
processed and their facts appended to the same sqlite store.

```bash
# force full reprocess (e.g. after changing extraction rules)
python3 main.py /path/to/chat_logs --force
```

## What it detects

| Pattern | Detected by | Extraction strategy |
|---|---|---|
| **fork** | turn content already seen in an earlier-processed file | skipped entirely — handled by universal turn-hash dedup, not a separate code path |
| **subagent** | `<available-skills>` block + dispatch-spec language (`"Return the full content"`, `"Build command template"`) + ≤30 turns | take the final assistant turn (already merged across consecutive `**Assistant:**` blocks) as one `task_summary` fact, tagged with guessed module name |
| **brainstorm** | everything else | split into `exchange` / `insight_exchange` (marked by `**insight**` / `**connection กับ POGLS**`) / `embedded_document` (heading-numbered docs like `### 5.2 Timeline`) / `external_ref` (`file:///...` links) |

## Files

- `parser.py` — turn splitter (handles both `Session:/Created:/Messages:` and `Source: X | Messages: N` header formats)
- `detector.py` — pattern classifier
- `extractors.py` — boilerplate stripping (available-skills block, repeated skill-injection text) + per-pattern candidate extraction
- `store.py` — sqlite state: file hashes (skip unchanged), turn hashes (dedupe across files), facts table
- `main.py` — CLI batch runner

## Output format (`facts.jsonl`)

```json
{"source_file": "...", "pattern": "subagent", "term_hint": "pogls_dram", "content": "...", "kind": "task_summary"}
```

Feed this into your colab LLM pass: group by `term_hint`, synthesize one
definition per term from all its fact rows, write into your dictionary
table (upsert on term).

## Known rough edges

- `task_summary` for subagent files includes the narration prefix before
  the actual final results (all merged assistant turns joined) — the real
  summary is usually near the end of `content`. Harmless for an LLM
  synthesis pass, just slightly more tokens than the minimum.
- `embedded_document` detection is heuristic (≥2 numbered headings like
  `### 5.2`, length > 600 chars) — tune `DOC_HEADING_RE` / the length
  threshold in `extractors.py` if you see false positives/negatives on
  your real corpus.
- Module-name guessing (`guess_module_name`) only recognizes `pogls_*`
  patterns — extend `MODULE_NAME_RE` if other naming conventions show up.
