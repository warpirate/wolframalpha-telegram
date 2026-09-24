"""Work out what a photo or a text message is, so the bot needs no commands."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

from ai_client import AIError, NebiusClient
from exam_prompts import INTENT_PROMPT, PAGE_READ_PROMPT
from jsonutil import JSONParseError, complete_json, loads_loose
from syllabus import BOOKS, normalise_subject

PAGE_KINDS = ("cover", "index", "pyq", "content", "question", "other")
INTENTS = ("study_plan", "lookup", "search", "quiz", "stats", "daily", "daily_off", "solve")
LOW_CONFIDENCE = 0.6
MAX_PAGE_TEXT = 6000

_QUESTION_NUMBER = re.compile(
    r"^\s*(?:(?:help(?:\s+me)?\s+with|explain|solve|do|answer)\s+)?"
    r"(?:q|qn|question|no)\s*[.#:]?\s*(\d{1,3})\b",
    re.I,
)

# Whole-message phrasings common enough to route without a model call. Anything
# longer or different still goes to classify_text, so these stay strict.
_POLITE = r"(?:\s*(?:please|pls|plz))?[\s?.!]*$"
_FIXED_INTENTS = [
    ("study_plan", re.compile(
        r"^\s*(?:what\s+(?:should|do)\s+i\s+(?:study|read)(?:\s+(?:next|now|today))?"
        r"|(?:my\s+|a\s+)?study\s+plan)" + _POLITE, re.I)),
    ("stats", re.compile(
        r"^\s*(?:how\s+am\s+i\s+doing|(?:my\s+)?(?:stats|progress|score))" + _POLITE, re.I)),
    ("daily_off", re.compile(
        r"^\s*(?:stop|cancel|turn\s+off)\s+(?:the\s+)?daily(?:\s+quiz)?" + _POLITE, re.I)),
]
_QUIZ = re.compile(r"^\s*(?:quiz\s+me|start\s+(?:a\s+)?quiz|quiz)(?:\s+on\s+([a-z .&-]+?))?" + _POLITE, re.I)


@dataclass
class PageRead:
    kind: str = "other"
    confidence: float = 0.0
    book: str = ""
    subject: str = "General"
    topic: str = ""
    page_no: int | None = None
    exam: str = ""
    year: int | None = None
    text: str = ""
    chapters: list[dict] = field(default_factory=list)
    pyqs: list[dict] = field(default_factory=list)


@dataclass
class Intent:
    name: str = "solve"
    number: int | None = None
    subject: str | None = None
    hour: int | None = None
    query: str = ""


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _str(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _exam(value: Any) -> str:
    exam = _str(value, 10).upper()
    return exam if exam in ("SI", "PC") else ""


def parse_page_read(raw: str) -> PageRead:
    """Validate the page reader's JSON. Anything unusable becomes an 'other' page."""
    try:
        data = loads_loose(raw)
    except JSONParseError:
        return PageRead()
    if not isinstance(data, dict):
        return PageRead()

    kind = _str(data.get("kind"), 20).lower()
    if kind not in PAGE_KINDS:
        kind = "other"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    book = _str(data.get("book"), 30).lower()
    if book not in BOOKS:
        book = ""
    page_exam, page_year = _exam(data.get("exam")), _int(data.get("year"))

    chapters: list[dict] = []
    for item in data.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        title = _str(item.get("title"), 120)
        if not title:
            continue
        topics = [t for t in (_str(x, 120) for x in item.get("topics") or []) if t]
        chapters.append({
            "number": _int(item.get("number")),
            "title": title,
            "page_start": _int(item.get("page_start")),
            "page_end": _int(item.get("page_end")),
            "topics": topics,
        })

    pyqs: list[dict] = []
    for item in data.get("pyqs") or []:
        if not isinstance(item, dict):
            continue
        question = _str(item.get("question"), 600)
        if not question:
            continue
        options = [o for o in (_str(x, 200) for x in item.get("options") or []) if o]
        pyqs.append({
            "number": _int(item.get("number")),
            "question": question,
            "options": options[:5],
            "answer": _str(item.get("answer"), 200),
            "exam": _exam(item.get("exam")) or page_exam,
            "year": _int(item.get("year")) or page_year,
        })

    if (kind == "index" and not chapters) or (kind == "pyq" and not pyqs):
        confidence = min(confidence, 0.3)  # claimed a kind but extracted nothing for it

    text = data.get("text")
    return PageRead(
        kind=kind,
        confidence=confidence,
        book=book,
        subject=normalise_subject(data.get("subject")),
        topic=_str(data.get("topic"), 80),
        page_no=_int(data.get("page_no")),
        exam=page_exam,
        year=page_year,
        text=text.strip()[:MAX_PAGE_TEXT] if isinstance(text, str) else "",
        chapters=chapters,
        pyqs=pyqs,
    )


async def read_page(
    client: NebiusClient,
    image_bytes: bytes,
    caption: str = "",
    forced_kind: str | None = None,
) -> PageRead:
    """One vision call: classify the page and extract everything worth storing."""
    note = "Read this page."
    if caption:
        note += f"\nThe user's caption: {caption}"
    if forced_kind:
        note += f"\nThe user says this page is of kind '{forced_kind}'. Use that kind."
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": PAGE_READ_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": note},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        },
    ]
    raw = await complete_json(client, messages, max_tokens=6000, temperature=0.1)
    read = parse_page_read(raw)
    if forced_kind in PAGE_KINDS:
        read.kind, read.confidence = forced_kind, 1.0
    return read


def quick_intent(text: str) -> Intent | None:
    """Catch 'Q14', 'quiz me on polity', 'how am I doing' and the like without a model call."""
    text = text or ""
    match = _QUESTION_NUMBER.match(text)
    if match:
        return Intent(name="lookup", number=int(match.group(1)), query=text.strip())
    for name, pattern in _FIXED_INTENTS:
        if pattern.match(text):
            return Intent(name=name, query=text.strip())
    match = _QUIZ.match(text)
    if match:
        subject = None
        if match.group(1):
            subject = normalise_subject(match.group(1))
            if subject == "General":
                return None  # a topic, not a subject: let the model read it
        return Intent(name="quiz", subject=subject, query=text.strip())
    return None


def parse_intent(raw: str, text: str) -> Intent:
    try:
        data = loads_loose(raw)
    except JSONParseError:
        return Intent(query=text)
    if not isinstance(data, dict):
        return Intent(query=text)
    name = _str(data.get("intent"), 20).lower()
    if name not in INTENTS:
        name = "solve"
    subject = normalise_subject(data.get("subject")) if data.get("subject") else None
    hour = _int(data.get("hour"))
    return Intent(
        name=name,
        number=_int(data.get("number")),
        subject=None if subject == "General" else subject,
        hour=hour if hour is not None and 0 <= hour <= 23 else None,
        query=_str(data.get("query"), 300) or text,
    )


async def classify_text(client: NebiusClient, text: str, focus_summary: str = "") -> Intent:
    """One small text call to pick an intent. Falls back to 'solve' on any failure."""
    messages = [
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": f"Current focus: {focus_summary or 'none'}\n\nMessage: {text}"},
    ]
    try:
        raw = await complete_json(client, messages, max_tokens=1500, temperature=0.0)
    except AIError:
        return Intent(query=text)
    return parse_intent(raw, text)
