#!/usr/bin/env python3
"""
chat_chunker.py — CLI version of the chat-log chunker web tool.
Mirrors: parse (variant A/B markdown, JSON, plain fallback) -> noise-strip
(built-in + custom rules) -> chunk simulate (turn / sliding) -> export.

Usage:
  python chat_chunker.py --input logs/*.md --strip-tool --strip-think \
      --custom-rule "assistant" --custom-rule "regex:foo\\d+bar" \
      --chunk-size 1200 --overlap 150 --mode turn \
      --output-format json --output chunks.json

  # Reusable rule set:
  python chat_chunker.py --input logs/*.md --rules-config rules.json \
      --output-format facts --output facts.jsonl

rules.json format:
  {
    "strip_tool": true, "strip_think": true, "strip_skill": true, "strip_ansi": true,
    "custom_rules": [{"pattern": "assistant", "is_regex": false},
                      {"pattern": "foo\\d+bar", "is_regex": true}]
  }
"""
import argparse
import json
import re
import sys
import glob
from pathlib import Path

ROLE_RE_B = re.compile(r'^##\s+(User|Assistant)\b.*$', re.MULTILINE)
ROLE_RE_A = re.compile(r'^\*\*(You|Assistant):\*\*.*$', re.MULTILINE)
LINE_RE = re.compile(r'^(User|Assistant|You)\s*:\s*')


def parse_content(name: str, content: str):
    turns = []
    if name.lower().endswith('.json'):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return turns
        arr = data if isinstance(data, list) else data.get('messages') or data.get('turns') or []
        for m in arr:
            role = m.get('role') or m.get('speaker') or 'Unknown'
            role_norm = 'Assistant' if re.search(r'assistant|model|bot', role, re.I) else \
                        'User' if re.search(r'user|you|human', role, re.I) else role
            c = m.get('content') or m.get('text') or m.get('message') or ''
            if isinstance(c, list):
                c = '\n'.join(x.get('text', json.dumps(x)) if isinstance(x, dict) else str(x) for x in c)
            if c:
                turns.append({'file': name, 'role': role_norm, 'raw': str(c)})
        return turns

    matches_b = list(ROLE_RE_B.finditer(content))
    if matches_b:
        for i, m in enumerate(matches_b):
            start = m.end()
            end = matches_b[i + 1].start() if i + 1 < len(matches_b) else len(content)
            body = content[start:end].strip()
            if body:
                turns.append({'file': name, 'role': m.group(1), 'raw': body})
        return turns

    matches_a = list(ROLE_RE_A.finditer(content))
    if matches_a:
        for i, m in enumerate(matches_a):
            role = 'User' if m.group(1) == 'You' else m.group(1)
            start = m.end()
            end = matches_a[i + 1].start() if i + 1 < len(matches_a) else len(content)
            body = content[start:end].strip()
            if body:
                turns.append({'file': name, 'role': role, 'raw': body})
        return turns

    cur = None
    any_line = False
    for line in content.splitlines():
        m = LINE_RE.match(line)
        if m:
            any_line = True
            if cur:
                turns.append(cur)
            role = 'User' if m.group(1) == 'You' else m.group(1)
            cur = {'file': name, 'role': role, 'raw': line[m.end():]}
        elif cur:
            cur['raw'] += '\n' + line
    if cur:
        turns.append(cur)
    if not any_line:
        turns.append({'file': name, 'role': 'Unknown', 'raw': content})
    return turns


def strip_noise(text: str, opt: dict) -> str:
    t = text
    if opt.get('strip_ansi'):
        t = re.sub(r'\x1b\[[0-9;]*m', '', t)
    t = re.sub(r'^\d+:\s?', '', t, flags=re.MULTILINE)
    if opt.get('strip_tool'):
        t = re.sub(
            r'Called the \w+ tool with the following input:[^\r\n]*'
            r'(?:<path>[\s\S]*?</path>)?(?:\r?\n<type>[^<\r\n]*</type>)?(?:\r?\n<content>[\s\S]*?</content>)?',
            '', t)
        prev = None
        while prev != t:
            prev = t
            t = re.sub(r'<content>[\s\S]*?</content>', '', t)
        t = re.sub(r'<path>[^<]*</path>', '', t)
        t = re.sub(r'<type>[^<]*</type>', '', t)
        t = re.sub(r'\*\*Tool:[\s\S]*?(?=\n## |\n\*\*Tool:|$)', '', t)
        t = re.sub(r'Full output saved to:.*$', '', t, flags=re.MULTILINE)
    if opt.get('strip_think'):
        t = re.sub(r'_Thinking:_(?:\n(?!## ).*)*', '', t)
    if opt.get('strip_skill'):
        t = re.sub(
            r'\[IMPORTANT: The user has invoked the "[\w-]+" skill[\s\S]*?\[Skill directory:[\s\S]*?\]\r?\n?'
            r'(?:Resolve any relative paths.*?\r?\n)?', '', t)
        t = re.sub(r'<available-skills>[\s\S]*?</available-skills>', '', t)
    for rule in opt.get('custom_rules', []):
        pattern = rule.get('pattern', '')
        if not pattern:
            continue
        try:
            src = pattern if rule.get('is_regex') else re.escape(pattern)
            t = re.sub(src, '', t, flags=re.IGNORECASE)
        except re.error:
            print(f'  [warn] skipping invalid custom rule: {pattern!r}', file=sys.stderr)
    return t.strip()


def build_chunks(turns, size, overlap, mode, opt, min_chars=0):
    chunks = []
    if mode == 'turn':
        buf, meta = '', []
        for t in turns:
            cleaned = strip_noise(t['raw'], opt)
            if not cleaned:
                continue
            piece = f"[{t['role']}] {cleaned}"
            if buf and len(buf) + len(piece) + 2 > size:
                if len(buf) >= min_chars:
                    chunks.append({'text': buf, 'files': sorted(set(meta))})
                buf = buf[max(0, len(buf) - overlap):]
                meta = []
            buf += ('\n\n' if buf else '') + piece
            meta.append(t['file'])
        if buf and len(buf) >= min_chars:
            chunks.append({'text': buf, 'files': sorted(set(meta))})
    else:  # sliding
        full = '\n\n'.join(f"[{t['role']}] {strip_noise(t['raw'], opt)}" for t in turns)
        i = 0
        step = max(1, size - overlap)
        while i < len(full):
            end = min(len(full), i + size)
            piece = full[i:end]
            if len(piece) >= min_chars:
                chunks.append({'text': piece, 'files': ['(merged)']})
            if end >= len(full):
                break
            i += step
    return chunks


def main():
    ap = argparse.ArgumentParser(description='CLI chat-log chunker (parse -> strip -> chunk -> export)')
    ap.add_argument('--input', nargs='+', required=True, help='Input files/globs (.md .json .txt)')
    ap.add_argument('--strip-tool', action='store_true')
    ap.add_argument('--strip-think', action='store_true')
    ap.add_argument('--strip-skill', action='store_true')
    ap.add_argument('--strip-ansi', action='store_true')
    ap.add_argument('--custom-rule', action='append', default=[],
                     help='Text to remove; prefix with "regex:" for a regex pattern. Repeatable.')
    ap.add_argument('--rules-config', help='JSON file with strip flags + custom_rules (overrides individual flags)')
    ap.add_argument('--chunk-size', type=int, default=1200)
    ap.add_argument('--overlap', type=int, default=150)
    ap.add_argument('--mode', choices=['turn', 'sliding'], default='turn')
    ap.add_argument('--min-chunk-chars', type=int, default=0, help='Drop chunks shorter than this')
    ap.add_argument('--output-format', choices=['json', 'md', 'txt', 'facts'], default='json')
    ap.add_argument('--output', required=True, help='Output file path')
    ap.add_argument('--exclude-roles', nargs='*', default=[], help='e.g. --exclude-roles Unknown')
    args = ap.parse_args()

    if args.rules_config:
        cfg = json.loads(Path(args.rules_config).read_text(encoding='utf-8'))
        opt = {
            'strip_tool': cfg.get('strip_tool', False),
            'strip_think': cfg.get('strip_think', False),
            'strip_skill': cfg.get('strip_skill', False),
            'strip_ansi': cfg.get('strip_ansi', False),
            'custom_rules': cfg.get('custom_rules', []),
        }
    else:
        custom_rules = []
        for r in args.custom_rule:
            if r.startswith('regex:'):
                custom_rules.append({'pattern': r[len('regex:'):], 'is_regex': True})
            else:
                custom_rules.append({'pattern': r, 'is_regex': False})
        opt = {
            'strip_tool': args.strip_tool, 'strip_think': args.strip_think,
            'strip_skill': args.strip_skill, 'strip_ansi': args.strip_ansi,
            'custom_rules': custom_rules,
        }

    files = []
    for pattern in args.input:
        matched = glob.glob(pattern)
        files.extend(matched if matched else [pattern])

    turns = []
    for fpath in files:
        p = Path(fpath)
        if not p.exists():
            print(f'  [warn] file not found, skipping: {fpath}', file=sys.stderr)
            continue
        content = p.read_text(encoding='utf-8', errors='replace')
        parsed = parse_content(p.name, content)
        turns.extend(parsed)
        print(f'  parsed {len(parsed):>5} turns  <-  {p.name}', file=sys.stderr)

    if args.exclude_roles:
        turns = [t for t in turns if t['role'] not in args.exclude_roles]

    print(f'Total turns: {len(turns)}', file=sys.stderr)

    out_path = Path(args.output)
    if args.output_format == 'json':
        chunks = build_chunks(turns, args.chunk_size, args.overlap, args.mode, opt, args.min_chunk_chars)
        payload = [
            {'index': i, 'chars': len(c['text']), 'approx_tokens': round(len(c['text']) / 4),
             'files': c['files'], 'text': c['text']}
            for i, c in enumerate(chunks)
        ]
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Wrote {len(chunks)} chunks -> {out_path}', file=sys.stderr)
    elif args.output_format == 'md':
        out_path.write_text(
            '\n\n---\n\n'.join(f"## {t['role']}\n\n{strip_noise(t['raw'], opt)}" for t in turns),
            encoding='utf-8')
        print(f'Wrote cleaned markdown -> {out_path}', file=sys.stderr)
    elif args.output_format == 'txt':
        out_path.write_text(
            '\n\n'.join(f"[{t['role']}] {strip_noise(t['raw'], opt)}" for t in turns),
            encoding='utf-8')
        print(f'Wrote plain text -> {out_path}', file=sys.stderr)
    elif args.output_format == 'facts':
        lines = [
            json.dumps({'source_file': t['file'], 'role': t['role'], 'content': strip_noise(t['raw'], opt)},
                       ensure_ascii=False)
            for t in turns
        ]
        out_path.write_text('\n'.join(lines), encoding='utf-8')
        print(f'Wrote {len(lines)} facts -> {out_path}', file=sys.stderr)


if __name__ == '__main__':
    main()
