"""
extractors.py — turn cleaned, deduped turns into fact CANDIDATES.

Important: this stage does NOT call an LLM. It produces clean, structured,
deduped text blocks tagged with (pattern, kind, term_hint) — the exact
shape you feed into your term-extraction / synthesis LLM pass in colab.
Keeping the LLM call out of this script means you can re-run parsing for
free any time you tweak regex rules, and only pay LLM cost once on the
final clean candidate set.
"""
import re
from typing import List, Optional
from dataclasses import dataclass
from parser import Turn, ParsedFile, merge_consecutive_assistant_turns

# ---- boilerplate stripping ----------------------------------------------

AVAILABLE_SKILLS_RE = re.compile(r"<available-skills>.*?</available-skills>", re.DOTALL)

# The recurring "[IMPORTANT: ... skill ... loaded below]" + yaml frontmatter
# + full skill body block seen verbatim across files (inbox-board etc).
SKILL_INJECTION_RE = re.compile(
    r"\[IMPORTANT: The user has invoked the \"(?P<skill>[\w\-]+)\" skill.*?"
    r"\[Skill directory:.*?\]\r?\n?"
    r"(?:Resolve any relative paths.*?\r?\n)?",
    re.DOTALL,
)

TRAILING_INSTRUCTION_RE = re.compile(
    r"The user has provided the following instruction alongside the skill invocation:\s*(?P<instr>.*)",
    re.DOTALL,
)

# Provider-locked reasoning traces (e.g. OpenAI o-series/gpt-5 "encrypted
# reasoning" returned when a session round-trips through their API).
# These are opaque ciphertext — unusable across model/provider/session,
# and explicitly designed to block distillation. Pure noise for a
# cross-platform fact pipeline; strip before hashing so they don't
# inflate turn size or create bogus "unique" turns.
REASONING_ENCRYPTED_RE = re.compile(
    r'"reasoningEncryptedContent"\s*:\s*"[^"]*"'
)
OPENAI_METADATA_BLOCK_RE = re.compile(
    r'"metadata"\s*:\s*\{\s*"openai"\s*:\s*\{.*?\}\s*\}',
    re.DOTALL,
)

# Inline tool-call echo from session exports: the marker line + JSON input,
# optionally followed by the tagged tool output (<path>/<type>/<content>).
# Pure transcript machinery — carries no knowledge, inflates hashes.
TOOL_CALL_RE = re.compile(
    r"Called the \w+ tool with the following input:[^\r\n]*"
    r"(?:\r?\n<path>.*?</path>)?"
    r"(?:\r?\n<type>[^<\r\n]*</type>)?"
    r"(?:\r?\n<content>.*?</content>)?",
    re.DOTALL,
)

# System-level XML tags injected by opencode runtime — not conversation
# content.  Strip these so they don't pollute fact extraction or hashes.
SYSTEM_TAG_RE = re.compile(
    r"<(available-skills|ctx-search-hint|session-history"
    r"|session-history-since|new-compartments|system-reminder"
    r"|path|type|content|directory|entries"
    r"|available_skills|ctx_search_hint)>.*?</\1>",
    re.DOTALL,
)

# Self-closing or unclosed variant (e.g. <type>file</type> on its own line)
SYSTEM_TAG_SELF_RE = re.compile(
    r"<(path|type|content|directory|entries)>[^<]*</\1>"
)

# Turn metadata header like "## Assistant (explore · deepseek-v4-flash-free · 10.8s)"
# or "## Assistant (build · model · N.Ns)" — pure label, no knowledge.
TURN_HEADER_RE = re.compile(
    r"^##\s+(User|Assistant)\b[^\r\n]*\r?\n", re.MULTILINE
)


def strip_boilerplate(text: str) -> str:
    text = AVAILABLE_SKILLS_RE.sub("", text)
    text = TOOL_CALL_RE.sub("", text)

    # strip provider-locked reasoning ciphertext before anything else —
    # try the full metadata block first (cleaner), fall back to just the
    # single field in case the surrounding JSON shape differs
    text = OPENAI_METADATA_BLOCK_RE.sub("", text)
    text = REASONING_ENCRYPTED_RE.sub('"reasoningEncryptedContent": "[stripped]"', text)

    # strip system-level XML tags (opencode runtime injected)
    text = SYSTEM_TAG_RE.sub("", text)
    text = SYSTEM_TAG_SELF_RE.sub("", text)

    # strip turn metadata headers
    text = TURN_HEADER_RE.sub("", text)

    def _repl(m: re.Match) -> str:
        tail = TRAILING_INSTRUCTION_RE.search(m.group(0))
        instr = tail.group("instr").strip() if tail else ""
        skill = m.group("skill")
        return f"[skill invoked: {skill}] {instr}".strip()

    text = SKILL_INJECTION_RE.sub(_repl, text)
    return text.strip()


# ---- module/term name guessing for subagent dispatch files --------------

MODULE_NAME_RE = re.compile(r"\b(pogls_[a-z0-9_]+)\b")


def guess_module_name(title: str, first_turn_text: str) -> Optional[str]:
    m = MODULE_NAME_RE.search(title) or MODULE_NAME_RE.search(first_turn_text)
    return m.group(1) if m else None


# ---- pattern: subagent dispatch ------------------------------------------

def extract_subagent(pf: ParsedFile, clean_turns: List[Turn]) -> List[dict]:
    """Skip straight to the final assistant summary — that's where the
    dispatch-task pattern places all the fact-worthy content (design
    decisions, test results, API surface)."""
    if not clean_turns:
        return []

    first_you = next((t.content for t in clean_turns if t.speaker == "You"), "")
    module = guess_module_name(pf.title, first_you) or pf.title

    assistant_turns = [t for t in clean_turns if t.speaker == "Assistant"]
    if not assistant_turns:
        return []

    # Final assistant turn is the merged tail (merge_consecutive_assistant_turns
    # already collapsed the run of short narration + final summary into one).
    summary = assistant_turns[-1].content.strip()
    if len(summary) < 40:
        # too short to be a real summary — fall back to last 2 turns joined
        summary = "\n\n".join(t.content.strip() for t in assistant_turns[-2:])

    return [{
        "content": summary,
        "kind": "task_summary",
        "term_hint": module,
    }]


# ---- pattern: brainstorm / freeform dialogue -----------------------------

INSIGHT_MARKER_RE = re.compile(r"\*\*(?:insight|connection กับ POGLS)\*\*", re.IGNORECASE)
DOC_HEADING_RE = re.compile(r"^#{2,4}\s+\d", re.MULTILINE)  # "### 5.2 Timeline" style
EXTERNAL_REF_RE = re.compile(r"file:///\S+")


def looks_like_embedded_document(text: str) -> bool:
    heading_hits = len(DOC_HEADING_RE.findall(text))
    return heading_hits >= 2 and len(text) > 600


def extract_brainstorm(pf: ParsedFile, clean_turns: List[Turn]) -> List[dict]:
    facts: List[dict] = []

    # pull out embedded documents as their own artifact-type fact, separate
    # from the surrounding dialogue
    for t in clean_turns:
        if looks_like_embedded_document(t.content):
            facts.append({
                "content": t.content.strip(),
                "kind": "embedded_document",
                "term_hint": pf.title,
            })

    # pull out external file references as link metadata, not content
    for t in clean_turns:
        for ref in EXTERNAL_REF_RE.findall(t.content):
            facts.append({
                "content": ref,
                "kind": "external_ref",
                "term_hint": pf.title,
            })

    # exchange-level candidates: pair each You turn with the Assistant
    # turn(s) that follow, dropping the pure embedded-document turns already
    # captured above so they aren't duplicated as dialogue too.
    doc_indices = {t.index for t in clean_turns if looks_like_embedded_document(t.content)}
    i = 0
    n = len(clean_turns)
    while i < n:
        t = clean_turns[i]
        if t.speaker == "You" and t.index not in doc_indices:
            you_text = t.content.strip()
            reply_parts = []
            j = i + 1
            while j < n and clean_turns[j].speaker == "Assistant":
                if clean_turns[j].index not in doc_indices:
                    reply_parts.append(clean_turns[j].content.strip())
                j += 1
            reply = "\n\n".join(reply_parts).strip()
            if you_text or reply:
                is_insight = bool(INSIGHT_MARKER_RE.search(reply))
                # trivial chatter ("test", "ทำต่อ", "ok") is not a fact
                # candidate — dropping it here keeps the synthesis corpus clean
                if len(you_text) + len(reply) < 80 and not is_insight:
                    i = j
                    continue
                facts.append({
                    "content": f"Q: {you_text}\nA: {reply}" if reply else you_text,
                    "kind": "insight_exchange" if is_insight else "exchange",
                    "term_hint": pf.title,
                })
            i = j
        else:
            i += 1

    return facts


# ---- dispatcher -----------------------------------------------------------

def extract(pf: ParsedFile, clean_turns: List[Turn], pattern: str) -> List[dict]:
    if pattern == "subagent":
        return extract_subagent(pf, clean_turns)
    # "fork" survivors and "brainstorm" both go through the same dialogue
    # extractor — fork-ness only affected which turns survived dedup, not
    # how the remaining unique content should be read.
    return extract_brainstorm(pf, clean_turns)
