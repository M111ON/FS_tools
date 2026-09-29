"""
parser.py — Parse exported chat-log markdown (state.db export format) into
structured turns: [{speaker, timestamp, content, line_start}]

Format assumed (two variants, both from real samples):

Variant A (state.db export):

    # <title>
    Session: ses_xxxx
    Created: YYYY-MM-DD HH:MM
    Messages: N
    ---
    **You:** [HH:MM]

    <content...>

    ---

    **Assistant:** [HH:MM]

    <content...>
    ---

Variant B (session .md export):

    # <title>

    **Session ID:** ses_xxxx
    **Created:** M/D/YYYY, H:MM:SS AM
    **Updated:** ...

    ---

    ## User

    <content...>

    ## Assistant (build · <model> · N.Ns)

    <content...>
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\r?\n")
SESSION_RE = re.compile(
    r"^\*{0,2}Session(?:\s+ID)?:\*{0,2}\s*(?P<session>\S+)\s*$", re.MULTILINE
)
CREATED_RE = re.compile(
    r"^\*{0,2}Created:\*{0,2}\s*(?P<created>[^\r\n]+?)\s*$", re.MULTILINE
)
SOURCE_RE = re.compile(r"^Source:\s*(?P<source>\S+)\s*\|\s*Messages:\s*(?P<messages>\d+)\s*$", re.MULTILINE)
MESSAGES_RE = re.compile(r"^Messages:\s*(?P<messages>\d+)\s*$", re.MULTILINE)
AVAILABLE_SKILLS_RE = re.compile(
    r"<available-skills>.*?</available-skills>\s*", re.DOTALL
)

# Variant A: **You:** [21:34] / **Assistant:** [21:34]
# Variant B: ## User / ## Assistant (build · model · 6.1s)
MARKER_RE = re.compile(
    r"(?:^\*\*(?P<speaker_a>You|Assistant):\*\*[ \t]*(?:\[(?P<time_a>[\d:]+)\])?[ \t]*\r?\n)"
    r"|(?:^##[ \t]+(?P<speaker_b>User|Assistant)\b(?P<rest_b>[^\r\n]*))",
    re.MULTILINE,
)


@dataclass
class Turn:
    speaker: str          # "You" | "Assistant"
    time: Optional[str]   # "21:34" etc, may be None
    content: str
    index: int            # order in file, 0-based


@dataclass
class ParsedFile:
    path: str
    title: str
    session: Optional[str]
    created: Optional[str]
    declared_messages: Optional[int]
    turns: List[Turn] = field(default_factory=list)


def parse_file(path: str) -> ParsedFile:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    header_end = raw.find("\n---")
    header = raw[:header_end] if header_end != -1 else raw[:400]

    tm_title = TITLE_RE.search(header)
    title = tm_title.group("title").strip() if tm_title else path

    sess = SESSION_RE.search(header)
    session = sess.group("session") if sess else None

    created_m = CREATED_RE.search(header)
    created = created_m.group("created").strip() if created_m else None

    src_m = SOURCE_RE.search(header)
    if src_m:
        declared_messages = int(src_m.group("messages"))
    else:
        msg_m = MESSAGES_RE.search(header)
        declared_messages = int(msg_m.group("messages")) if msg_m else None

    markers = list(MARKER_RE.finditer(raw))
    turns: List[Turn] = []
    for i, mm in enumerate(markers):
        if mm.group("speaker_a"):
            speaker, time = mm.group("speaker_a"), mm.group("time_a")
        else:
            # normalize variant B's "## User" to "You" — downstream code
            # (extractors, detector) only knows You|Assistant
            speaker, time = "You", None
        start = mm.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(raw)
        content = raw[start:end]
        # trim a trailing lone "---" separator line left over between turns
        content = re.sub(r"\r?\n-{3,}\r?\n\s*$", "", content).strip()
        if not content:
            continue
        turns.append(Turn(speaker=speaker, time=time, content=content, index=i))

    return ParsedFile(
        path=path,
        title=title,
        session=session,
        created=created,
        declared_messages=declared_messages,
        turns=turns,
    )


def merge_consecutive_assistant_turns(turns: List[Turn]) -> List[Turn]:
    """Collapse runs of back-to-back Assistant turns (self-correction mid-thought)
    into a single turn, joined by blank line — per pattern seen in real logs
    where the model emits several short **Assistant:** blocks before the real answer."""
    merged: List[Turn] = []
    for t in turns:
        if merged and merged[-1].speaker == "Assistant" and t.speaker == "Assistant":
            merged[-1] = Turn(
                speaker="Assistant",
                time=merged[-1].time,
                content=merged[-1].content + "\n\n" + t.content,
                index=merged[-1].index,
            )
        else:
            merged.append(t)
    return merged
