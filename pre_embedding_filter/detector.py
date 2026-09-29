"""
detector.py — classify a ParsedFile into one of the extraction patterns
identified from real samples:

  fork        — long common prefix shared with another file in the batch
  subagent    — dispatch-task shape: <available-skills> block first turn,
                "Return the full content" style instruction, few turns (<30),
                assistant narration ends in a clean results summary
  brainstorm  — everything else: many turns, topic drift, no dispatch shape
"""
import re
from dataclasses import dataclass
from parser import ParsedFile

SUBAGENT_HINT_RE = re.compile(
    r"Return the full content|standalone (?:lib|library)|Build command template",
    re.IGNORECASE,
)
AVAILABLE_SKILLS_RE = re.compile(r"<available-skills>", re.IGNORECASE)


@dataclass
class Classification:
    pattern: str          # "fork" | "subagent" | "brainstorm"
    reason: str


def classify(pf: ParsedFile, is_fork_child: bool = False) -> Classification:
    if is_fork_child:
        return Classification("fork", "shares long common prefix with another file")

    first_you_texts = [t.content for t in pf.turns if t.speaker == "You"][:2]
    header_blob = "\n".join(first_you_texts)

    has_skills_block = bool(AVAILABLE_SKILLS_RE.search(header_blob))
    has_dispatch_hint = bool(SUBAGENT_HINT_RE.search(header_blob))
    few_turns = len(pf.turns) <= 30

    if has_skills_block and has_dispatch_hint and few_turns:
        return Classification("subagent", "dispatch-task shape: skills block + spec + few turns")

    return Classification("brainstorm", "default: multi-turn freeform conversation")
