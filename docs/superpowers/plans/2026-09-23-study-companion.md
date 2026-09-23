
# Study Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every photo the user sends is automatically classified, stored, indexed (SQL + vectors) and used to ground answers; text messages are routed by intent, so the bot needs no slash commands.

**Architecture:** A vision "page reader" turns each photo into a typed `PageRead` (kind, book, subject, topic, page number, text, chapters, PYQs). `library.py` saves it into new tables (`books`, `chapters`, `pyqs`, `pages`, `chunks`) and embeds its text with Qwen3-Embedding-8B into `chunks` (pgvector on Postgres, float32 blobs + brute-force cosine on SQLite). A text router picks an intent (study plan, lookup, search, quiz, stats, daily, solve); answers are grounded by retrieving the user's own chunks first. `ranking.py` turns PYQ counts, pages read and quiz accuracy into a study plan.

**Tech Stack:** Python 3.12, python-telegram-bot 22, httpx, asyncpg + pgvector 0.8, sqlite3, Nebius Token Factory (chat, vision, embeddings), pytest.

**Spec:** `docs/specs/2026-09-23-study-companion.md`

---

## File map

| File                                                   | Status | Responsibility                                                                                   |
| ------------------------------------------------------ | ------ | ------------------------------------------------------------------------------------------------ |
| `jsonutil.py`                                        | new    | Tolerant JSON parsing + JSON-mode completion (moved out of`mcq.py`)                            |
| `syllabus.py`                                        | new    | The four books, subject normalisation, estimated weightage, gap list                             |
| `router.py`                                          | new    | `read_page` (photo → `PageRead`), `quick_intent` / `classify_text` (text → `Intent`) |
| `retrieval.py`                                       | new    | Chunking, batched embeddings, search, context rendering                                          |
| `ranking.py`                                         | new    | Units (chapters/topics), scores, study-plan text                                                 |
| `library.py`                                         | new    | `save_photo` orchestration per kind, focus, PYQ lookup, grounded prompts                       |
| `exam_prompts.py`                                    | modify | Add`PAGE_READ_PROMPT`, `INTENT_PROMPT`                                                       |
| `config.py`                                          | modify | `NEBIUS_EMBED_MODEL`                                                                           |
| `ai_client.py`                                       | modify | `embed()`; `_post_with_retries` takes an endpoint                                            |
| `mcq.py`                                             | modify | Use`jsonutil`                                                                                  |
| `db.py` / `db_pg.py`                               | modify | New tables, vector storage, library functions,`questions.batch_id/chapter_id`                  |
| `main.py`                                            | modify | Photo + text routing, button callbacks, remove add mode and commands                             |
| `quiz.py`                                            | modify | Drop`/command` mentions from user-facing text                                                  |
| `setup_botfather.py`                                 | modify | Command menu: only`/start`                                                                     |
| `tests/…`, `pytest.ini`, `requirements-dev.txt` | new    | Test suite                                                                                       |

Callback data formats (Telegram limit 64 bytes): `q:` quiz (existing), `u:<batch>` undo, `w:<batch>:<page_id>` show kind picker, `k:<batch>:<page_id>:<kind>` re-read as kind.

---

### Task 1: Test scaffolding and `jsonutil`

**Files:**

- Create: `pytest.ini`, `requirements-dev.txt`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_jsonutil.py`, `jsonutil.py`
- Modify: `mcq.py`

- [ ] **Step 1: Scaffolding**

`pytest.ini`:

```ini
[pytest]
testpaths = tests
```

`requirements-dev.txt`:

```
-r requirements.txt
pytest>=8.0
```

`tests/__init__.py`: empty file.

`tests/conftest.py`:

```python
"""Test setup: fake credentials, SQLite in a temp dir, never the real .env database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# config.py refuses to import without these, and load_dotenv() never overrides
# variables that are already set, so the real DATABASE_URL is kept out.
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["NEBIUS_API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """A fresh SQLite database per test."""
    db.close()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    yield db
    db.close()
```

- [ ] **Step 2: Write the failing test**

`tests/test_jsonutil.py`:

```python
import pytest

from jsonutil import JSONParseError, loads_loose


def test_plain_object():
    assert loads_loose('{"a": 1}') == {"a": 1}


def test_code_fence():
    assert loads_loose('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}


def test_prose_around_json():
    assert loads_loose('Here you go: {"kind": "index"} hope that helps') == {"kind": "index"}


def test_garbage_raises():
    with pytest.raises(JSONParseError):
        loads_loose("no json here")
```

- [ ] **Step 3: Run it to see it fail**

Run: `python -m pytest tests/test_jsonutil.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jsonutil'`

- [ ] **Step 4: Implement `jsonutil.py`**

```python
"""Tolerant JSON parsing and JSON-mode completions for the model-facing modules."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai_client import AIError, NebiusClient

logger = logging.getLogger(__name__)


class JSONParseError(ValueError):
    """The model's reply held no parseable JSON."""


def strip_code_fence(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    return fenced.group(1).strip() if fenced else text.strip()


def loads_loose(raw: str) -> Any:
    """Parse JSON that may be fenced or wrapped in prose."""
    text = strip_code_fence(raw or "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if not match:
        raise JSONParseError("no JSON found in model output")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JSONParseError(str(exc)) from exc


async def complete_json(
    client: NebiusClient,
    messages: list[dict],
    *,
    max_tokens: int = 3000,
    temperature: float = 0.4,
) -> str:
    """Ask for JSON, retrying once without response_format if the API rejects it."""
    try:
        return await client.complete_raw(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    except AIError as exc:
        logger.warning("JSON mode failed (%s); retrying without response_format", exc.detail)
        return await client.complete_raw(messages, max_tokens=max_tokens, temperature=temperature)
```

- [ ] **Step 5: Point `mcq.py` at it**

In `mcq.py`: delete `_strip_code_fence` and `_complete_json`, drop the `json` and `re` imports, and import `from jsonutil import JSONParseError, complete_json, loads_loose`. Replace the parsing block at the top of `parse_questions` with:

```python
    try:
        payload = loads_loose(raw)
    except JSONParseError as exc:
        raise MCQError("The model did not return usable JSON.") from exc
```

Replace both `raw = await _complete_json(client, messages)` lines with:

```python
    raw = await complete_json(client, messages)
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest -v`
Expected: 4 passed. Then `python -c "import mcq"` exits cleanly.

- [ ] **Step 7: Commit**

```bash
git add pytest.ini requirements-dev.txt tests jsonutil.py mcq.py
git commit -m "Move tolerant JSON parsing into jsonutil and add a test suite"
```

---

### Task 2: Embeddings in the AI client

**Files:**

- Modify: `config.py`, `ai_client.py`, `.env.example`
- Test: `tests/test_ai_client.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ai_client.py`:

```python
import asyncio

from ai_client import NebiusClient


def test_embed_orders_by_index_and_posts_to_embeddings():
    client = NebiusClient(api_key="k", base_url="https://example.test/v1", model="m")
    seen = {}

    async def fake_post(payload, endpoint=None):
        seen["payload"], seen["endpoint"] = payload, endpoint
        return {"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}

    client._post_with_retries = fake_post
    vectors = asyncio.run(client.embed(["a", "b"], model="emb", dimensions=2))

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["endpoint"] == "https://example.test/v1/embeddings"
    assert seen["payload"] == {"model": "emb", "input": ["a", "b"], "dimensions": 2}
    asyncio.run(client.aclose())


def test_embed_empty_input_makes_no_call():
    client = NebiusClient(api_key="k", base_url="https://example.test/v1", model="m")
    assert asyncio.run(client.embed([], model="emb", dimensions=2)) == []
    asyncio.run(client.aclose())
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m pytest tests/test_ai_client.py -v`
Expected: FAIL — `AttributeError: 'NebiusClient' object has no attribute 'embed'`

- [ ] **Step 3: Implement**

In `ai_client.py`, add after `complete_raw`:

```python
    async def embed(self, texts: list[str], model: str, dimensions: int) -> list[list[float]]:
        """Embed texts in one request; results come back in input order."""
        if not texts:
            return []
        payload = {"model": model, "input": texts, "dimensions": dimensions}
        data = await self._post_with_retries(payload, endpoint=f"{self._base_url}/embeddings")
        try:
            rows = sorted(data["data"], key=lambda row: row["index"])
            return [row["embedding"] for row in rows]
        except (KeyError, TypeError) as exc:
            raise AIError(FRIENDLY_GENERIC, detail=f"bad embeddings response: {exc!r}") from exc
```

Change `_post_with_retries` to take the endpoint:

```python
    async def _post_with_retries(
        self, payload: dict[str, Any], endpoint: str | None = None
    ) -> dict[str, Any]:
        last_error: AIError | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(endpoint or self._endpoint, json=payload)
```

(the rest of the method is unchanged).

In `config.py`, after `NEBIUS_VISION_MODEL`:

```python
# Embeddings for searching the user's saved pages and PYQs.
NEBIUS_EMBED_MODEL: str = _optional("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
```

and add `"NEBIUS_EMBED_MODEL",` to `__all__`. In `.env.example`, under the vision model line:

```
# NEBIUS_EMBED_MODEL=Qwen/Qwen3-Embedding-8B
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ai_client.py config.py .env.example tests/test_ai_client.py
git commit -m "Add an embeddings call to the Nebius client"
```

---

### Task 3: `syllabus.py`

**Files:**

- Create: `syllabus.py`
- Test: `tests/test_syllabus.py`

- [ ] **Step 1: Write the failing test**

```python
import syllabus


def test_book_lookup():
    assert syllabus.book_subject("karim") == "History"
    assert syllabus.book_title("lucent").startswith("General Science")
    assert syllabus.book_subject("nope") == "General"


def test_normalise_subject():
    assert syllabus.normalise_subject("polity") == "Polity"
    assert syllabus.normalise_subject(" current affairs ") == "Current Affairs"
    assert syllabus.normalise_subject("astrology") == "General"
    assert syllabus.normalise_subject(None) == "General"


def test_uncovered_subjects_excludes_the_four_books():
    gaps = syllabus.uncovered_subjects()
    assert "Reasoning" in gaps and "Telangana" in gaps
    assert not {"Polity", "History", "Arithmetic", "Science"} & set(gaps)


def test_estimates_sum_to_one():
    assert abs(sum(syllabus.ESTIMATED_SHARE.values()) - 1.0) < 1e-9
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m pytest tests/test_syllabus.py -v` → FAIL, no module.

- [ ] **Step 3: Implement `syllabus.py`**

```python
"""The user's books, subject names, and fallback weightage estimates."""

from __future__ import annotations

from typing import Any

from exam_prompts import SUBJECTS

BOOKS: dict[str, dict[str, str]] = {
    "laxmikanth": {"title": "Indian Polity — M. Laxmikanth", "subject": "Polity"},
    "karim": {"title": "Indian History — M. Abdul Kareem", "subject": "History"},
    "rs_aggarwal": {"title": "Quantitative Aptitude — R.S. Aggarwal", "subject": "Arithmetic"},
    "lucent": {"title": "General Science — Lucent's", "subject": "Science"},
}

# Rough share of the SI/PC papers per subject. Used only until enough PYQs are
# stored to count the real distribution; always shown to the user as an estimate.
ESTIMATED_SHARE: dict[str, float] = {
    "Arithmetic": 0.20,
    "Reasoning": 0.20,
    "History": 0.12,
    "Science": 0.10,
    "Telangana": 0.10,
    "Polity": 0.08,
    "Geography": 0.08,
    "Economy": 0.06,
    "Current Affairs": 0.06,
}

MIN_PYQS_FOR_REAL_WEIGHTS = 30

_SUBJECT_LOOKUP = {s.lower(): s for s in SUBJECTS}


def book_subject(key: str) -> str:
    return BOOKS.get(key, {}).get("subject", "General")


def book_title(key: str) -> str:
    return BOOKS.get(key, {}).get("title", key)


def normalise_subject(value: Any) -> str:
    if not isinstance(value, str):
        return "General"
    return _SUBJECT_LOOKUP.get(value.strip().lower(), "General")


def uncovered_subjects() -> list[str]:
    """Syllabus subjects that none of the user's books covers, biggest first."""
    covered = {book["subject"] for book in BOOKS.values()}
    gaps = [s for s in ESTIMATED_SHARE if s not in covered]
    return sorted(gaps, key=lambda s: -ESTIMATED_SHARE[s])
```

- [ ] **Step 4: Run tests** → pass.
- [ ] **Step 5: Commit**

```bash
git add syllabus.py tests/test_syllabus.py
git commit -m "Add the syllabus: the four books, subjects and estimated weightage"
```

---

### Task 4: Page-reading and intent prompts

**Files:**

- Modify: `exam_prompts.py` (append)

- [ ] **Step 1: Append the prompts**

```python
_SUBJECT_LIST = ", ".join(SUBJECTS)

PAGE_READ_PROMPT = (
    """You read photos of pages from a TSLPRB (Telangana police SI/PC) aspirant's study books and return JSON only.

THE USER'S BOOKS (put the key in "book"):
- laxmikanth: Indian Polity by M. Laxmikanth
- karim: Indian History by M. Abdul Kareem (Max Publications)
- rs_aggarwal: Quantitative Aptitude by R.S. Aggarwal
- lucent: General Science by Lucent's
Use "" if the page is from none of these or you cannot tell.

PAGE KINDS:
- cover: a book cover or title page
- index: a contents/index page listing chapters or topics with page numbers
- pyq: a page of previous-year exam questions (marked with exams/years such as "SI 2019", "PC 2022", "TSLPRB", "APPSC")
- content: explanatory text from a chapter (theory, notes, tables, worked examples)
- question: one or a few questions the user is working on, not marked as previous-year
- other: anything else

RETURN EXACTLY THIS SHAPE:
{"kind": "", "confidence": 0.0, "book": "", "subject": "", "topic": "", "page_no": null, "exam": "", "year": null, "text": "", "chapters": [], "pyqs": []}

RULES:
- confidence: 0.0-1.0, how sure you are of "kind".
- subject: one of """
    + _SUBJECT_LIST
    + """.
- topic: short specific label, e.g. "Mauryan administration", "Fundamental Rights", "Time and Work".
- page_no: the printed page number if visible, else null.
- text: faithful transcription of the readable text, in reading order, at most about 5000 characters. Nothing that is not printed.
- chapters (index pages only): [{"number": 6, "title": "The Mauryan Age", "page_start": 138, "page_end": 171, "topics": ["Extent of the empire", "Ashoka's Dhamma"]}]. Every chapter visible, even partly.
- pyqs (pyq pages only): [{"number": 14, "question": "", "options": ["", "", "", ""], "answer": "", "exam": "SI", "year": 2019}]. Copy question numbers exactly as printed. exam is "SI", "PC" or "". answer only if printed on the page, else "".
- exam / year at top level: the exam and year printed as the page heading, if any.
- Never invent chapters, questions, numbers or years. If the page is unreadable, set confidence below 0.3."""
)

INTENT_PROMPT = (
    """You route messages for a TSLPRB exam-prep Telegram bot. Return JSON only:
{"intent": "", "number": null, "subject": null, "hour": null, "query": ""}

INTENTS:
- study_plan: asks what to study, what to prioritise, which chapter or topic first, weightage, how to plan
- lookup: refers to a specific question by number ("Q14", "question 7", "the 12th one") or to "this question" / "this one". number = the question number, or null for "this one".
- search: asks what their books or saved PYQs say about something ("PYQs on Ashoka", "what did Karim say about rajukas", "questions on the Preamble")
- quiz: wants to be quizzed, tested or drilled
- stats: asks about their progress, accuracy, score or weak areas
- daily: wants a quiz every day at a time; hour = 0-23 in IST
- daily_off: wants to stop the daily quiz
- solve: anything else — a question to answer, a doubt, a calculation, chat

subject, when the message names one, is one of: """
    + _SUBJECT_LIST
    + """. Otherwise null.
query: the message rewritten as a standalone search query, resolving "this" / "it" using the current focus."""
)
```

- [ ] **Step 2: Check it imports**

Run: `python -c "import exam_prompts; print(len(exam_prompts.PAGE_READ_PROMPT), len(exam_prompts.INTENT_PROMPT))"`
Expected: two numbers, no error.

- [ ] **Step 3: Commit**

```bash
git add exam_prompts.py
git commit -m "Add prompts for reading pages and routing messages"
```

---

### Task 5: `router.py`

**Files:**

- Create: `router.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

```python
import json

import router


def test_index_page_parsed():
    raw = json.dumps({
        "kind": "index", "confidence": 0.9, "book": "karim", "subject": "history",
        "topic": "Contents", "page_no": None, "exam": "", "year": None, "text": "CHAPTER-6 ...",
        "chapters": [
            {"number": "6", "title": "The Mauryan Age", "page_start": 138, "page_end": "171",
             "topics": ["Ashoka's Dhamma", ""]},
            {"title": ""},
        ],
        "pyqs": [],
    })
    read = router.parse_page_read(raw)
    assert read.kind == "index" and read.book == "karim" and read.subject == "History"
    assert read.chapters == [{"number": 6, "title": "The Mauryan Age", "page_start": 138,
                              "page_end": 171, "topics": ["Ashoka's Dhamma"]}]


def test_pyq_inherits_page_exam_and_year():
    raw = json.dumps({
        "kind": "pyq", "confidence": 0.8, "book": "", "subject": "Polity", "exam": "si", "year": 2019,
        "pyqs": [{"number": 14, "question": "Who wrote the Preamble?", "options": ["a", "b", "c", "d"],
                  "answer": "b"}],
    })
    read = router.parse_page_read(raw)
    assert read.pyqs[0]["exam"] == "SI" and read.pyqs[0]["year"] == 2019
    assert read.pyqs[0]["number"] == 14


def test_bad_output_falls_back_to_other():
    read = router.parse_page_read("sorry, cannot help")
    assert read.kind == "other" and read.confidence == 0.0


def test_unknown_kind_and_book_are_cleaned():
    read = router.parse_page_read('{"kind": "poster", "book": "ncert", "confidence": 5}')
    assert read.kind == "other" and read.book == "" and read.confidence == 1.0


def test_index_without_chapters_is_low_confidence():
    read = router.parse_page_read('{"kind": "index", "confidence": 0.9, "chapters": []}')
    assert read.confidence <= 0.3


def test_quick_intent_question_numbers():
    for text, number in [("Q14", 14), ("help with q 7", 7), ("explain question 12", 12),
                         ("solve no. 3", 3), ("Q.21 please", 21)]:
        intent = router.quick_intent(text)
        assert intent is not None and intent.name == "lookup" and intent.number == number, text


def test_quick_intent_ignores_other_text():
    assert router.quick_intent("what should I study") is None
    assert router.quick_intent("15% of 640") is None


def test_parse_intent():
    intent = router.parse_intent('{"intent": "quiz", "subject": "polity"}', "quiz me on polity")
    assert intent.name == "quiz" and intent.subject == "Polity" and intent.query == "quiz me on polity"
    daily = router.parse_intent('{"intent": "daily", "hour": 30}', "daily at 30")
    assert daily.name == "daily" and daily.hour is None
    assert router.parse_intent("nonsense", "hi").name == "solve"
```

- [ ] **Step 2: Run to see it fail** → no module `router`.
- [ ] **Step 3: Implement `router.py`**

```python
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
    """Catch 'Q14'-style messages without a model call."""
    match = _QUESTION_NUMBER.match(text or "")
    if not match:
        return None
    return Intent(name="lookup", number=int(match.group(1)), query=text.strip())


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
```

- [ ] **Step 4: Run tests** → `python -m pytest tests/test_router.py -v` passes.
- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "Add the router: read a page photo, pick an intent for text"
```

---

### Task 6: SQLite library tables and functions

**Files:**

- Modify: `db.py`
- Test: `tests/test_db_library.py`

- [ ] **Step 1: Write the failing tests**

```python
import asyncio


def run(coro):
    return asyncio.run(coro)


def _page(**over):
    page = {"book_id": None, "chapter_id": None, "page_no": 150, "kind": "pyq",
            "subject": "History", "topic": "Mauryan Age", "text": "Q14 ...",
            "file_id": "F1", "file_unique_id": "U1"}
    page.update(over)
    return page


def _pyq(**over):
    item = {"book_id": None, "chapter_id": None, "page_id": None, "exam": "SI", "year": 2019,
            "number": 14, "question": "Who was Ashoka's father?", "options": ["a", "b", "c", "d"],
            "answer": "b", "subject": "History", "topic": "Mauryan Age"}
    item.update(over)
    return item


def test_book_upsert_is_idempotent(sqlite_db):
    first = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    again = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    assert first == again


def test_chapters_and_page_lookup(sqlite_db):
    book = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    cid, created = run(sqlite_db.upsert_chapter(1, book, {
        "number": 6, "title": "The Mauryan Age", "page_start": 138, "page_end": 171,
        "topics": ["Dhamma"]}, "b1"))
    assert created
    cid2, created2 = run(sqlite_db.upsert_chapter(1, book, {
        "number": 6, "title": "The Mauryan Age", "page_start": None, "page_end": None,
        "topics": []}, "b2"))
    assert cid2 == cid and not created2
    found = run(sqlite_db.find_chapter_for_page(1, book, 150))
    assert found["id"] == cid and found["topics"] == ["Dhamma"] and found["page_start"] == 138
    assert run(sqlite_db.find_chapter_for_page(1, book, 500)) is None


def test_page_duplicate_returns_none(sqlite_db):
    assert run(sqlite_db.add_page(1, _page(), "b1")) is not None
    assert run(sqlite_db.add_page(1, _page(), "b2")) is None


def test_pyq_duplicate_and_lookup_precedence(sqlite_db):
    page_a = run(sqlite_db.add_page(1, _page(), "b1"))
    page_b = run(sqlite_db.add_page(1, _page(file_unique_id="U2"), "b2"))
    first = run(sqlite_db.add_pyq(1, _pyq(page_id=page_a), "b1"))
    assert run(sqlite_db.add_pyq(1, _pyq(page_id=page_a), "b1")) is None
    second = run(sqlite_db.add_pyq(1, _pyq(page_id=page_b, question="Capital of Magadha?"), "b2"))
    assert run(sqlite_db.find_pyq(1, 14, page_id=page_a))["id"] == first
    latest = run(sqlite_db.find_pyq(1, 14))
    assert latest["id"] == second and latest["options"] == ["a", "b", "c", "d"]
    assert run(sqlite_db.find_pyq(1, 99)) is None


def test_vector_search_orders_by_similarity(sqlite_db):
    run(sqlite_db.add_chunks(1, "page", 1, [("ashoka", [1.0, 0.0]), ("gupta", [0.0, 1.0])],
                             "History", "t", "b1"))
    run(sqlite_db.add_chunks(2, "page", 9, [("other user", [1.0, 0.0])], "History", "t", "b9"))
    hits = run(sqlite_db.search_chunks(1, [0.9, 0.1], 5))
    assert [h["text"] for h in hits] == ["ashoka", "gupta"]
    assert hits[0]["score"] > 0.9
    assert run(sqlite_db.search_chunks(1, [1.0, 0.0], 5, ["pyq"])) == []


def test_undo_batch_removes_everything_from_that_batch(sqlite_db):
    page_id = run(sqlite_db.add_page(1, _page(), "b1"))
    run(sqlite_db.add_pyq(1, _pyq(page_id=page_id), "b1"))
    run(sqlite_db.add_chunks(1, "pyq", 1, [("x", [1.0])], "History", "t", "b1"))
    run(sqlite_db.add_questions(1, [{"question": "q?", "options": ["a", "b", "c", "d"],
                                     "correct_index": 0}], "src", batch_id="b1"))
    keep = run(sqlite_db.add_page(1, _page(file_unique_id="U9"), "b2"))
    assert run(sqlite_db.undo_batch(1, "b1")) == 4
    assert run(sqlite_db.get_page(1, page_id)) is None
    assert run(sqlite_db.get_page(1, keep)) is not None
    assert run(sqlite_db.undo_batch(1, "")) == 0


def test_counts_and_progress(sqlite_db):
    book = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    cid, _ = run(sqlite_db.upsert_chapter(1, book, {"number": 6, "title": "The Mauryan Age",
                                                     "page_start": 138, "page_end": 171,
                                                     "topics": []}, "b1"))
    run(sqlite_db.add_pyq(1, _pyq(chapter_id=cid), "b1"))
    run(sqlite_db.add_pyq(1, _pyq(chapter_id=cid, exam="PC", question="Other?"), "b1"))
    run(sqlite_db.add_page(1, _page(kind="content", chapter_id=cid), "b1"))
    counts = run(sqlite_db.pyq_counts(1))
    assert {(r["title"], r["exam"], r["n"]) for r in counts} == {
        ("The Mauryan Age", "SI", 1), ("The Mauryan Age", "PC", 1)}
    progress = run(sqlite_db.topic_progress(1))
    assert progress["pages"][0]["chapter_id"] == cid and progress["pages"][0]["pages"] == 1
    assert progress["attempts"] == []
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m pytest tests/test_db_library.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'upsert_book'`

- [ ] **Step 3: Implement in `db.py`**

Add imports at the top: `import json`, `import math`, `from array import array`.

Append to `SCHEMA` (inside the string, after `prefs`):

```sql
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    book_id     INTEGER NOT NULL,
    number      INTEGER,
    title       TEXT    NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER,
    topics      TEXT    NOT NULL DEFAULT '[]',
    batch_id    TEXT    NOT NULL DEFAULT '',
    UNIQUE (book_id, title)
);

CREATE TABLE IF NOT EXISTS pages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    book_id        INTEGER,
    chapter_id     INTEGER,
    page_no        INTEGER,
    kind           TEXT    NOT NULL,
    subject        TEXT    NOT NULL,
    topic          TEXT    NOT NULL,
    text           TEXT    NOT NULL,
    file_id        TEXT    NOT NULL,
    file_unique_id TEXT    NOT NULL,
    batch_id       TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    UNIQUE (user_id, file_unique_id)
);

CREATE TABLE IF NOT EXISTS pyqs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    book_id     INTEGER,
    chapter_id  INTEGER,
    page_id     INTEGER,
    exam        TEXT    NOT NULL DEFAULT '',
    year        INTEGER,
    number      INTEGER,
    question    TEXT    NOT NULL,
    options     TEXT    NOT NULL DEFAULT '[]',
    answer      TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, question)
);
CREATE INDEX IF NOT EXISTS idx_pyqs_number ON pyqs(user_id, number);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    source_id   INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id, source);
```

In `_connect`, after `executescript(SCHEMA)`:

```python
        for column in ("batch_id TEXT NOT NULL DEFAULT ''", "chapter_id INTEGER"):
            try:
                _conn.execute(f"ALTER TABLE questions ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already added on an earlier start
```

Replace `_add_questions` / `add_questions` so MCQs carry the batch and chapter:

```python
def _add_questions(
    conn, user_id: int, items: Iterable[dict], source: str, batch_id: str, chapter_id: int | None
) -> int:
    added = 0
    for item in items:
        options = item["options"]
        try:
            conn.execute(
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at, batch_id, chapter_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    item.get("subject", "General"),
                    item.get("topic", "General"),
                    item["question"],
                    options[0], options[1], options[2], options[3],
                    int(item["correct_index"]),
                    item.get("explanation", ""),
                    source,
                    now_iso(),
                    batch_id,
                    chapter_id,
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue  # duplicate question text for this user - skip silently
    return added


async def add_questions(
    user_id: int, items: list[dict], source: str, batch_id: str = "", chapter_id: int | None = None
) -> int:
    """Insert generated questions, skipping exact duplicates. Returns count added."""
    return await call(_add_questions, user_id, items, source, batch_id, chapter_id)
```

Add a new section before `close()`:

```python
# -------------------------------------------------------------------- library


def _json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _to_blob(vector: Iterable[float]) -> bytes:
    return array("f", vector).tobytes()


def _from_blob(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _upsert_book(conn, user_id: int, key: str, title: str, subject: str) -> int:
    conn.execute(
        """INSERT INTO books (user_id, key, title, subject, created_at) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, key) DO NOTHING""",
        (user_id, key, title, subject, now_iso()),
    )
    return conn.execute(
        "SELECT id FROM books WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()["id"]


async def upsert_book(user_id: int, key: str, title: str, subject: str) -> int:
    """Create the book once per user; returns its id either way."""
    return await call(_upsert_book, user_id, key, title, subject)


def _upsert_chapter(conn, user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    topics = json.dumps(chapter.get("topics") or [])
    row = conn.execute(
        "SELECT id FROM chapters WHERE book_id = ? AND title = ?", (book_id, chapter["title"])
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE chapters SET
                 number = COALESCE(?, number),
                 page_start = COALESCE(?, page_start),
                 page_end = COALESCE(?, page_end),
                 topics = CASE WHEN ? = '[]' THEN topics ELSE ? END
               WHERE id = ?""",
            (chapter.get("number"), chapter.get("page_start"), chapter.get("page_end"),
             topics, topics, row["id"]),
        )
        return row["id"], False
    cursor = conn.execute(
        """INSERT INTO chapters (user_id, book_id, number, title, page_start, page_end, topics, batch_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, book_id, chapter.get("number"), chapter["title"], chapter.get("page_start"),
         chapter.get("page_end"), topics, batch_id),
    )
    return cursor.lastrowid, True


async def upsert_chapter(user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    """Insert or fill in a chapter. Returns (id, created)."""
    return await call(_upsert_chapter, user_id, book_id, chapter, batch_id)


def _chapter_dict(row) -> dict:
    data = dict(row)
    data["topics"] = _json_list(data.get("topics"))
    return data


def _find_chapter_for_page(conn, user_id: int, book_id: int, page_no: int):
    return conn.execute(
        """SELECT * FROM chapters
           WHERE user_id = ? AND book_id = ? AND page_start <= ?
             AND (page_end IS NULL OR page_end >= ?)
           ORDER BY page_start DESC LIMIT 1""",
        (user_id, book_id, page_no, page_no),
    ).fetchone()


async def find_chapter_for_page(user_id: int, book_id: int, page_no: int) -> dict | None:
    row = await call(_find_chapter_for_page, user_id, book_id, page_no)
    return _chapter_dict(row) if row else None


def _add_page(conn, user_id: int, page: dict, batch_id: str) -> int | None:
    try:
        cursor = conn.execute(
            """INSERT INTO pages (user_id, book_id, chapter_id, page_no, kind, subject, topic,
                                  text, file_id, file_unique_id, batch_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, page.get("book_id"), page.get("chapter_id"), page.get("page_no"),
             page["kind"], page["subject"], page["topic"], page["text"], page["file_id"],
             page["file_unique_id"], batch_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None  # this exact photo was stored before
    return cursor.lastrowid


async def add_page(user_id: int, page: dict, batch_id: str) -> int | None:
    """Store a photo's reading. Returns None when the same photo was already stored."""
    return await call(_add_page, user_id, page, batch_id)


def _get_page(conn, user_id: int, page_id: int):
    return conn.execute(
        "SELECT * FROM pages WHERE user_id = ? AND id = ?", (user_id, page_id)
    ).fetchone()


async def get_page(user_id: int, page_id: int) -> dict | None:
    row = await call(_get_page, user_id, page_id)
    return dict(row) if row else None


def _add_pyq(conn, user_id: int, item: dict, batch_id: str) -> int | None:
    try:
        cursor = conn.execute(
            """INSERT INTO pyqs (user_id, book_id, chapter_id, page_id, exam, year, number,
                                 question, options, answer, subject, topic, batch_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, item.get("book_id"), item.get("chapter_id"), item.get("page_id"),
             item.get("exam", ""), item.get("year"), item.get("number"), item["question"],
             json.dumps(item.get("options") or []), item.get("answer", ""), item["subject"],
             item["topic"], batch_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None  # same question text already stored
    return cursor.lastrowid


async def add_pyq(user_id: int, item: dict, batch_id: str) -> int | None:
    """Store one previous-year question. Returns None for an exact duplicate."""
    return await call(_add_pyq, user_id, item, batch_id)


def _pyq_dict(row) -> dict:
    data = dict(row)
    data["options"] = _json_list(data.get("options"))
    return data


def _find_pyq(conn, user_id: int, number: int, page_id: int | None, chapter_id: int | None):
    for column, value in (("page_id", page_id), ("chapter_id", chapter_id)):
        if value is None:
            continue
        row = conn.execute(
            f"""SELECT * FROM pyqs WHERE user_id = ? AND number = ? AND {column} = ?
                ORDER BY id DESC LIMIT 1""",
            (user_id, number, value),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM pyqs WHERE user_id = ? AND number = ? ORDER BY id DESC LIMIT 1",
        (user_id, number),
    ).fetchone()


async def find_pyq(
    user_id: int, number: int, page_id: int | None = None, chapter_id: int | None = None
) -> dict | None:
    """Question `number` from the given page, else that chapter, else the newest one."""
    row = await call(_find_pyq, user_id, number, page_id, chapter_id)
    return _pyq_dict(row) if row else None


def _pyq_counts(conn, user_id: int):
    return conn.execute(
        """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                  COALESCE(c.title, p.topic) AS title, p.exam AS exam, COUNT(*) AS n
           FROM pyqs p LEFT JOIN chapters c ON c.id = p.chapter_id
           WHERE p.user_id = ?
           GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic), p.exam""",
        (user_id,),
    ).fetchall()


async def pyq_counts(user_id: int) -> list[dict]:
    return [dict(row) for row in await call(_pyq_counts, user_id)]


def _topic_progress(conn, user_id: int) -> dict:
    pages = conn.execute(
        """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                  COALESCE(c.title, p.topic) AS title, COUNT(*) AS pages
           FROM pages p LEFT JOIN chapters c ON c.id = p.chapter_id
           WHERE p.user_id = ? AND p.kind = 'content'
           GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic)""",
        (user_id,),
    ).fetchall()
    attempts = conn.execute(
        """SELECT q.subject AS subject, q.chapter_id AS chapter_id,
                  COALESCE(c.title, q.topic) AS title,
                  COUNT(*) AS attempts, COALESCE(SUM(a.is_correct), 0) AS correct
           FROM attempts a
           JOIN questions q ON q.id = a.question_id
           LEFT JOIN chapters c ON c.id = q.chapter_id
           WHERE a.user_id = ?
           GROUP BY q.subject, q.chapter_id, COALESCE(c.title, q.topic)""",
        (user_id,),
    ).fetchall()
    return {"pages": [dict(r) for r in pages], "attempts": [dict(r) for r in attempts]}


async def topic_progress(user_id: int) -> dict:
    """Content pages read and quiz results, per chapter (or topic when no chapter)."""
    return await call(_topic_progress, user_id)


def _add_chunks(conn, user_id, source, source_id, rows, subject, topic, batch_id) -> None:
    conn.executemany(
        """INSERT INTO chunks (user_id, source, source_id, subject, topic, text, embedding, batch_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(user_id, source, source_id, subject, topic, text, _to_blob(vector), batch_id)
         for text, vector in rows],
    )


async def add_chunks(
    user_id: int,
    source: str,
    source_id: int,
    rows: list[tuple[str, list[float]]],
    subject: str,
    topic: str,
    batch_id: str,
) -> None:
    """Store (text, embedding) pairs for one page, PYQ or chapter."""
    await call(_add_chunks, user_id, source, source_id, rows, subject, topic, batch_id)


def _search_chunks(conn, user_id, vector, k, sources) -> list[dict]:
    sql = "SELECT id, source, source_id, subject, topic, text, embedding FROM chunks WHERE user_id = ?"
    params: list[Any] = [user_id]
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    scored = []
    for row in conn.execute(sql, params):
        hit = {key: row[key] for key in ("id", "source", "source_id", "subject", "topic", "text")}
        hit["score"] = _cosine(vector, _from_blob(row["embedding"]))
        scored.append(hit)
    scored.sort(key=lambda hit: hit["score"], reverse=True)
    return scored[:k]


async def search_chunks(
    user_id: int, vector: list[float], k: int, sources: list[str] | None = None
) -> list[dict]:
    """Nearest chunks by cosine similarity (brute force - fine for a few thousand rows)."""
    return await call(_search_chunks, user_id, vector, k, sources)


def _undo_batch(conn, user_id: int, batch_id: str) -> int:
    if not batch_id:
        return 0
    removed = 0
    for table in ("chunks", "pyqs", "pages", "chapters", "questions"):
        removed += conn.execute(
            f"DELETE FROM {table} WHERE user_id = ? AND batch_id = ?", (user_id, batch_id)
        ).rowcount
    return removed


async def undo_batch(user_id: int, batch_id: str) -> int:
    """Delete every row a single save created. Returns rows removed (chunks excluded from nothing)."""
    return await call(_undo_batch, user_id, batch_id)
```

In the backend switch, add:

```python
    upsert_book = db_pg.upsert_book              # type: ignore[assignment]
    upsert_chapter = db_pg.upsert_chapter        # type: ignore[assignment]
    find_chapter_for_page = db_pg.find_chapter_for_page  # type: ignore[assignment]
    add_page = db_pg.add_page                    # type: ignore[assignment]
    get_page = db_pg.get_page                    # type: ignore[assignment]
    add_pyq = db_pg.add_pyq                      # type: ignore[assignment]
    find_pyq = db_pg.find_pyq                    # type: ignore[assignment]
    pyq_counts = db_pg.pyq_counts                # type: ignore[assignment]
    topic_progress = db_pg.topic_progress        # type: ignore[assignment]
    add_chunks = db_pg.add_chunks                # type: ignore[assignment]
    search_chunks = db_pg.search_chunks          # type: ignore[assignment]
    undo_batch = db_pg.undo_batch                # type: ignore[assignment]
```

Note: the undo test expects 4 — page, pyq, chunk and MCQ each count one row. Fix the `undo_batch` docstring to: `"""Delete every row one save created. Returns the number of rows removed."""`

- [ ] **Step 4: Run tests** → `python -m pytest -v` all pass.
- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db_library.py
git commit -m "Store books, chapters, pages, PYQs and embeddings in SQLite"
```

---

### Task 7: Postgres equivalents (pgvector)

**Files:**

- Modify: `db_pg.py`

No automated test (needs a live Postgres); verified in Task 13 against the real database.

- [ ] **Step 1: Schema**

Add `import json` at the top. Add after the `SCHEMA` string:

```python
EMBED_DIMENSIONS = 1024

SCHEMA_LIBRARY = f"""
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE questions ADD COLUMN IF NOT EXISTS batch_id TEXT NOT NULL DEFAULT '';
ALTER TABLE questions ADD COLUMN IF NOT EXISTS chapter_id BIGINT;

CREATE TABLE IF NOT EXISTS books (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    key         TEXT        NOT NULL,
    title       TEXT        NOT NULL,
    subject     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS chapters (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT  NOT NULL,
    book_id     BIGINT  NOT NULL,
    number      INTEGER,
    title       TEXT    NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER,
    topics      TEXT    NOT NULL DEFAULT '[]',
    batch_id    TEXT    NOT NULL DEFAULT '',
    UNIQUE (book_id, title)
);

CREATE TABLE IF NOT EXISTS pages (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT      NOT NULL,
    book_id        BIGINT,
    chapter_id     BIGINT,
    page_no        INTEGER,
    kind           TEXT        NOT NULL,
    subject        TEXT        NOT NULL,
    topic          TEXT        NOT NULL,
    text           TEXT        NOT NULL,
    file_id        TEXT        NOT NULL,
    file_unique_id TEXT        NOT NULL,
    batch_id       TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, file_unique_id)
);

CREATE TABLE IF NOT EXISTS pyqs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    book_id     BIGINT,
    chapter_id  BIGINT,
    page_id     BIGINT,
    exam        TEXT        NOT NULL DEFAULT '',
    year        INTEGER,
    number      INTEGER,
    question    TEXT        NOT NULL,
    options     TEXT        NOT NULL DEFAULT '[]',
    answer      TEXT        NOT NULL DEFAULT '',
    subject     TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    batch_id    TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, question)
);
CREATE INDEX IF NOT EXISTS idx_pyqs_number ON pyqs(user_id, number);

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT  NOT NULL,
    source      TEXT    NOT NULL,
    source_id   BIGINT  NOT NULL,
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   vector({EMBED_DIMENSIONS}) NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id, source);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
"""
```

In `connect`, after `await conn.execute(SCHEMA)`: `await conn.execute(SCHEMA_LIBRARY)`.

- [ ] **Step 2: `add_questions` with batch/chapter**

Change the signature to `add_questions(user_id, items, source, batch_id: str = "", chapter_id: int | None = None)` and the insert to:

```python
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at, batch_id, chapter_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                   ON CONFLICT DO NOTHING""",
```

passing `batch_id, chapter_id` after `now_ist()`.

- [ ] **Step 3: Library functions**

Add before `aclose`:

```python
# -------------------------------------------------------------------- library


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def _json_list(value) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _rowcount(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def upsert_book(user_id: int, key: str, title: str, subject: str) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO books (user_id, key, title, subject) VALUES ($1,$2,$3,$4)
               ON CONFLICT (user_id, key) DO NOTHING""",
            user_id, key, title, subject,
        )
        return await conn.fetchval(
            "SELECT id FROM books WHERE user_id = $1 AND key = $2", user_id, key
        )


async def upsert_chapter(user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    pool = _require_pool()
    topics = json.dumps(chapter.get("topics") or [])
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM chapters WHERE book_id = $1 AND title = $2", book_id, chapter["title"]
        )
        if existing:
            await conn.execute(
                """UPDATE chapters SET
                     number = COALESCE($1, number),
                     page_start = COALESCE($2, page_start),
                     page_end = COALESCE($3, page_end),
                     topics = CASE WHEN $4 = '[]' THEN topics ELSE $4 END
                   WHERE id = $5""",
                chapter.get("number"), chapter.get("page_start"), chapter.get("page_end"),
                topics, existing,
            )
            return existing, False
        new_id = await conn.fetchval(
            """INSERT INTO chapters (user_id, book_id, number, title, page_start, page_end, topics, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            user_id, book_id, chapter.get("number"), chapter["title"], chapter.get("page_start"),
            chapter.get("page_end"), topics, batch_id,
        )
        return new_id, True


async def find_chapter_for_page(user_id: int, book_id: int, page_no: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM chapters
               WHERE user_id = $1 AND book_id = $2 AND page_start <= $3
                 AND (page_end IS NULL OR page_end >= $3)
               ORDER BY page_start DESC LIMIT 1""",
            user_id, book_id, page_no,
        )
    if row is None:
        return None
    data = dict(row)
    data["topics"] = _json_list(data.get("topics"))
    return data


async def add_page(user_id: int, page: dict, batch_id: str) -> int | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO pages (user_id, book_id, chapter_id, page_no, kind, subject, topic,
                                  text, file_id, file_unique_id, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (user_id, file_unique_id) DO NOTHING
               RETURNING id""",
            user_id, page.get("book_id"), page.get("chapter_id"), page.get("page_no"),
            page["kind"], page["subject"], page["topic"], page["text"], page["file_id"],
            page["file_unique_id"], batch_id,
        )


async def get_page(user_id: int, page_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pages WHERE user_id = $1 AND id = $2", user_id, page_id
        )
    return dict(row) if row else None


async def add_pyq(user_id: int, item: dict, batch_id: str) -> int | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO pyqs (user_id, book_id, chapter_id, page_id, exam, year, number,
                                 question, options, answer, subject, topic, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (user_id, question) DO NOTHING
               RETURNING id""",
            user_id, item.get("book_id"), item.get("chapter_id"), item.get("page_id"),
            item.get("exam", ""), item.get("year"), item.get("number"), item["question"],
            json.dumps(item.get("options") or []), item.get("answer", ""), item["subject"],
            item["topic"], batch_id,
        )


async def find_pyq(
    user_id: int, number: int, page_id: int | None = None, chapter_id: int | None = None
) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = None
        for column, value in (("page_id", page_id), ("chapter_id", chapter_id)):
            if value is None:
                continue
            row = await conn.fetchrow(
                f"""SELECT * FROM pyqs WHERE user_id = $1 AND number = $2 AND {column} = $3
                    ORDER BY id DESC LIMIT 1""",
                user_id, number, value,
            )
            if row:
                break
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM pyqs WHERE user_id = $1 AND number = $2 ORDER BY id DESC LIMIT 1",
                user_id, number,
            )
    if row is None:
        return None
    data = dict(row)
    data["options"] = _json_list(data.get("options"))
    return data


async def pyq_counts(user_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                      COALESCE(c.title, p.topic) AS title, p.exam AS exam, COUNT(*) AS n
               FROM pyqs p LEFT JOIN chapters c ON c.id = p.chapter_id
               WHERE p.user_id = $1
               GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic), p.exam""",
            user_id,
        )
    return [dict(row) for row in rows]


async def topic_progress(user_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        pages = await conn.fetch(
            """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                      COALESCE(c.title, p.topic) AS title, COUNT(*) AS pages
               FROM pages p LEFT JOIN chapters c ON c.id = p.chapter_id
               WHERE p.user_id = $1 AND p.kind = 'content'
               GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic)""",
            user_id,
        )
        attempts = await conn.fetch(
            """SELECT q.subject AS subject, q.chapter_id AS chapter_id,
                      COALESCE(c.title, q.topic) AS title, COUNT(*) AS attempts,
                      COALESCE(SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS correct
               FROM attempts a
               JOIN questions q ON q.id = a.question_id
               LEFT JOIN chapters c ON c.id = q.chapter_id
               WHERE a.user_id = $1
               GROUP BY q.subject, q.chapter_id, COALESCE(c.title, q.topic)""",
            user_id,
        )
    return {"pages": [dict(r) for r in pages], "attempts": [dict(r) for r in attempts]}


async def add_chunks(
    user_id: int,
    source: str,
    source_id: int,
    rows: list[tuple[str, list[float]]],
    subject: str,
    topic: str,
    batch_id: str,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO chunks (user_id, source, source_id, subject, topic, text, embedding, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8)""",
            [(user_id, source, source_id, subject, topic, text, _vector_literal(vector), batch_id)
             for text, vector in rows],
        )


async def search_chunks(
    user_id: int, vector: list[float], k: int, sources: list[str] | None = None
) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, source, source_id, subject, topic, text,
                      1 - (embedding <=> $2::vector) AS score
               FROM chunks
               WHERE user_id = $1 AND ($3::text[] IS NULL OR source = ANY($3::text[]))
               ORDER BY embedding <=> $2::vector
               LIMIT $4""",
            user_id, _vector_literal(vector), sources, k,
        )
    return [dict(row) for row in rows]


async def undo_batch(user_id: int, batch_id: str) -> int:
    if not batch_id:
        return 0
    pool = _require_pool()
    removed = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in ("chunks", "pyqs", "pages", "chapters", "questions"):
                status = await conn.execute(
                    f"DELETE FROM {table} WHERE user_id = $1 AND batch_id = $2", user_id, batch_id
                )
                removed += _rowcount(status)
    return removed
```

- [ ] **Step 4: Import check**

Run: `python -c "import db_pg"` → no error. `python -m pytest -v` → still green (SQLite path).

- [ ] **Step 5: Commit**

```bash
git add db_pg.py
git commit -m "Store the library and embeddings in Postgres with pgvector"
```

---

### Task 8: `retrieval.py`

**Files:**

- Create: `retrieval.py`
- Test: `tests/test_retrieval.py`

- [ ] **Step 1: Write the failing tests**

```python
import asyncio

import retrieval


def test_short_text_is_one_chunk():
    assert retrieval.chunk_text("  hello  ") == ["hello"]
    assert retrieval.chunk_text("   ") == []


def test_long_text_chunks_overlap_and_cover_everything():
    words = [f"w{i}" for i in range(600)]
    text = " ".join(words)
    chunks = retrieval.chunk_text(text, size=200, overlap=50)
    assert all(len(c) <= 200 for c in chunks)
    joined = " ".join(chunks)
    assert all(w in joined for w in words)
    assert chunks[0].split()[-1] in chunks[1]  # overlap carries context across the cut


class FakeClient:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, model, dimensions):
        self.calls.append(len(texts))
        return [[float(len(t)), 1.0] for t in texts]


def test_embed_batches(monkeypatch):
    monkeypatch.setattr(retrieval, "EMBED_BATCH", 2)
    client = FakeClient()
    vectors = asyncio.run(retrieval.embed(client, ["a", "bb", "ccc"]))
    assert client.calls == [2, 1] and vectors[2] == [3.0, 1.0]


def test_render_context_numbers_hits():
    text = retrieval.render_context([
        {"source": "pyq", "topic": "Mauryan Age", "text": "Q14 Who ..."},
        {"source": "page", "topic": "Dhamma", "text": "Ashoka's Dhamma ..."},
    ])
    assert text.startswith("[1] PYQ · Mauryan Age\nQ14 Who ...")
    assert "[2] Book page · Dhamma" in text
```

- [ ] **Step 2: Run to see it fail** → no module.
- [ ] **Step 3: Implement `retrieval.py`**

```python
"""Embeddings and similarity search over everything the user has saved."""

from __future__ import annotations

import logging

import db
from ai_client import AIError, NebiusClient
from config import NEBIUS_EMBED_MODEL

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 1024  # must match vector(1024) in db_pg.SCHEMA_LIBRARY
EMBED_BATCH = 32
CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
MIN_SCORE = 0.45  # below this a hit is noise, not context
DUPLICATE_SCORE = 0.97
CHAPTER_MATCH_SCORE = 0.5

_SOURCE_LABELS = {"page": "Book page", "pyq": "PYQ", "chapter": "Chapter"}


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on line or word boundaries into overlapping chunks of at most `size` chars."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + size // 2, end)
            if cut != -1:
                end = cut
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        space = text.find(" ", next_start, end)
        start = space + 1 if space != -1 else next_start  # never start mid-word
    return chunks


async def embed(client: NebiusClient, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(
            await client.embed(texts[i : i + EMBED_BATCH], NEBIUS_EMBED_MODEL, EMBED_DIMENSIONS)
        )
    return vectors


async def index_texts(
    client: NebiusClient,
    user_id: int,
    source: str,
    source_id: int,
    texts: list[str],
    subject: str,
    topic: str,
    batch_id: str,
) -> int:
    """Embed and store texts for one saved item. Returns how many were indexed."""
    texts = [t for t in texts if t.strip()]
    if not texts:
        return 0
    vectors = await embed(client, texts)
    await db.add_chunks(user_id, source, source_id, list(zip(texts, vectors)), subject, topic, batch_id)
    return len(texts)


async def search(
    client: NebiusClient,
    user_id: int,
    query: str,
    k: int = 6,
    sources: list[str] | None = None,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    """Closest saved chunks to the query. Empty on embedding failure - answers still work."""
    if not query.strip():
        return []
    try:
        [vector] = await embed(client, [query[:2000]])
    except AIError as exc:
        logger.warning("Embedding the query failed: %s", exc.detail)
        return []
    hits = await db.search_chunks(user_id, vector, k, sources)
    return [hit for hit in hits if hit["score"] >= min_score]


def render_context(hits: list[dict]) -> str:
    blocks = []
    for number, hit in enumerate(hits, 1):
        label = _SOURCE_LABELS.get(hit["source"], hit["source"])
        topic = f" · {hit['topic']}" if hit.get("topic") else ""
        blocks.append(f"[{number}] {label}{topic}\n{hit['text']}")
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run tests** → pass.
- [ ] **Step 5: Commit**

```bash
git add retrieval.py tests/test_retrieval.py
git commit -m "Add retrieval: chunking, batched embeddings, search"
```

---

### Task 9: `ranking.py`

**Files:**

- Create: `ranking.py`
- Test: `tests/test_ranking.py`

- [ ] **Step 1: Write the failing tests**

```python
import ranking


def _pyq(chapter_id, title, exam, n, subject="History"):
    return {"subject": subject, "chapter_id": chapter_id, "title": title, "exam": exam, "n": n}


def test_build_units_merges_by_chapter():
    units = ranking.build_units(
        [_pyq(1, "Mauryan Age", "SI", 5), _pyq(1, "Mauryan Age", "PC", 2), _pyq(None, "Preamble", "", 3, "Polity")],
        {"pages": [{"subject": "History", "chapter_id": 1, "title": "Mauryan Age", "pages": 4}],
         "attempts": [{"subject": "History", "chapter_id": 1, "title": "Mauryan Age",
                       "attempts": 10, "correct": 4}]},
    )
    by_title = {u.title: u for u in units}
    mauryan = by_title["Mauryan Age"]
    assert (mauryan.si, mauryan.pc, mauryan.pages, mauryan.attempts, mauryan.correct) == (5, 2, 4, 10, 4)
    assert by_title["Preamble"].other == 3


def test_untouched_high_weight_beats_mastered():
    units = ranking.build_units(
        [_pyq(1, "Mastered", "SI", 10), _pyq(2, "Untouched", "SI", 8)],
        {"pages": [{"subject": "History", "chapter_id": 1, "title": "Mastered", "pages": 3}],
         "attempts": [{"subject": "History", "chapter_id": 1, "title": "Mastered",
                       "attempts": 10, "correct": 10}]},
    )
    ranked = ranking.score_units(units)
    assert ranked[0].title == "Untouched"


def test_weak_topic_is_boosted():
    units = ranking.build_units(
        [_pyq(1, "Weak", "SI", 5), _pyq(2, "Fresh", "SI", 5)],
        {"pages": [], "attempts": [{"subject": "History", "chapter_id": 1, "title": "Weak",
                                    "attempts": 4, "correct": 1}]},
    )
    assert ranking.score_units(units)[0].title == "Weak"


def test_plan_uses_estimates_when_few_pyqs():
    text = ranking.render_plan(ranking.build_units([_pyq(1, "Mauryan Age", "SI", 3)], {}))
    assert "estimate" in text.lower()
    assert "Reasoning" in text and "no book" in text


def test_plan_ranks_when_enough_pyqs():
    rows = [_pyq(i, f"Chapter {i}", "SI", 10 - i) for i in range(1, 6)]
    text = ranking.render_plan(ranking.build_units(rows, {}))
    assert text.index("Chapter 1") < text.index("Chapter 2")
    assert "No book yet for" in text
```

- [ ] **Step 2: Run to see it fail** → no module.
- [ ] **Step 3: Implement `ranking.py`**

```python
"""Rank chapters by PYQ weight, how much is covered and how weak the user is; render the plan."""

from __future__ import annotations

from dataclasses import dataclass

import syllabus

TOP_N = 8
STRONG_ACCURACY = 0.7
MIN_ATTEMPTS_FOR_STRONG = 3
_STATUS = {0.0: "not started", 0.5: "in progress", 1.0: "strong"}


@dataclass
class Unit:
    key: str
    subject: str
    title: str
    si: int = 0
    pc: int = 0
    other: int = 0
    pages: int = 0
    attempts: int = 0
    correct: int = 0
    score: float = 0.0

    @property
    def pyqs(self) -> int:
        return self.si + self.pc + self.other


def _key(row: dict) -> str:
    if row.get("chapter_id") is not None:
        return f"c{row['chapter_id']}"
    return f"t:{row['subject']}|{(row.get('title') or '').strip().lower()}"


def build_units(pyq_rows: list[dict], progress: dict) -> list[Unit]:
    units: dict[str, Unit] = {}

    def unit(row: dict) -> Unit:
        key = _key(row)
        if key not in units:
            units[key] = Unit(key=key, subject=row["subject"], title=row.get("title") or row["subject"])
        return units[key]

    for row in pyq_rows:
        target = unit(row)
        exam = row.get("exam") or ""
        if exam == "SI":
            target.si += row["n"]
        elif exam == "PC":
            target.pc += row["n"]
        else:
            target.other += row["n"]
    for row in progress.get("pages", []):
        unit(row).pages += row["pages"]
    for row in progress.get("attempts", []):
        target = unit(row)
        target.attempts += row["attempts"]
        target.correct += row["correct"]
    return list(units.values())


def coverage(unit: Unit) -> float:
    if unit.pages == 0 and unit.attempts == 0:
        return 0.0
    if unit.attempts >= MIN_ATTEMPTS_FOR_STRONG and unit.correct / unit.attempts >= STRONG_ACCURACY:
        return 1.0
    return 0.5


def weakness(unit: Unit) -> float:
    return 0.0 if unit.attempts == 0 else 1 - unit.correct / unit.attempts


def _exam_count(unit: Unit, exam: str) -> int:
    # PYQs with no exam marked count toward both exams.
    return (unit.si if exam == "SI" else unit.pc) + unit.other


def score_units(units: list[Unit], exam: str = "SI") -> list[Unit]:
    total = sum(_exam_count(u, exam) for u in units)
    for u in units:
        weight = _exam_count(u, exam) / total if total else 0.0
        u.score = weight * (1 - 0.7 * coverage(u)) * (1 + weakness(u))
    return sorted(units, key=lambda u: (-u.score, -u.pyqs, u.title))


def _unit_line(number: int, unit: Unit) -> str:
    weak = f", {round(weakness(unit) * 100)}% wrong in quizzes" if unit.attempts else ""
    return (
        f"{number}. {unit.title} ({unit.subject}) — SI {unit.si + unit.other} · PC {unit.pc + unit.other}"
        f" PYQs · {_STATUS[coverage(unit)]}{weak}"
    )


def render_plan(units: list[Unit], exam: str = "SI") -> str:
    total = sum(u.pyqs for u in units)
    book_subjects = {b["subject"] for b in syllabus.BOOKS.values()}
    lines: list[str] = []

    if total < syllabus.MIN_PYQS_FOR_REAL_WEIGHTS:
        lines.append(
            f"Only {total} PYQs saved so far, so this is an estimate until about "
            f"{syllabus.MIN_PYQS_FOR_REAL_WEIGHTS} are in."
        )
        lines.append("")
        lines.append("Share of the paper by subject (estimate):")
        for subject, share in sorted(syllabus.ESTIMATED_SHARE.items(), key=lambda kv: -kv[1]):
            mark = "📘 your book" if subject in book_subjects else "⚠️ no book"
            lines.append(f"• {subject} ~{round(share * 100)}% — {mark}")
        ranked = [u for u in score_units(units, exam) if u.pyqs][:TOP_N]
        if ranked:
            lines.append("")
            lines.append("From the PYQs you've saved:")
            lines.extend(_unit_line(i, u) for i, u in enumerate(ranked, 1))
        lines.append("")
        lines.append("Send PYQ pages from your books and this becomes a real ranking.")
        return "\n".join(lines)

    ranked = score_units(units, exam)[:TOP_N]
    lines.append(f"Study next — ranked from {total} PYQs ({exam} first):")
    lines.append("")
    lines.extend(_unit_line(i, u) for i, u in enumerate(ranked, 1))
    gaps = syllabus.uncovered_subjects()
    if gaps:
        lines.append("")
        lines.append("No book yet for: " + ", ".join(gaps))
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests** → pass.
- [ ] **Step 5: Commit**

```bash
git add ranking.py tests/test_ranking.py
git commit -m "Add ranking: PYQ weight, coverage and weakness into a study plan"
```

---

### Task 10: `library.py`

**Files:**

- Create: `library.py`
- Test: `tests/test_library.py`

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import json

import library


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """complete_raw returns queued JSON replies; embed maps keywords to fixed vectors."""

    vision_model = "vision"
    KEYWORDS = ("maurya", "ashoka", "gupta", "preamble")

    def __init__(self, replies):
        self.replies = list(replies)

    async def complete_raw(self, messages, **kwargs):
        return self.replies.pop(0)

    async def embed(self, texts, model, dimensions):
        return [[1.0 if k in t.lower() else 0.0 for k in self.KEYWORDS] + [0.1] for t in texts]


INDEX = json.dumps({
    "kind": "index", "confidence": 0.9, "book": "karim", "subject": "History", "text": "contents",
    "chapters": [
        {"number": 6, "title": "The Mauryan Age", "page_start": 138, "page_end": 171,
         "topics": ["Ashoka's Dhamma"]},
        {"number": 8, "title": "The Age of Guptas", "page_start": 206, "page_end": 233, "topics": []},
    ],
})

PYQ = json.dumps({
    "kind": "pyq", "confidence": 0.9, "book": "karim", "subject": "History", "exam": "SI",
    "year": 2019, "text": "Q14 ...",
    "pyqs": [{"number": 14, "question": "Which Mauryan king issued the Dhamma edicts?",
              "options": ["Ashoka", "Bindusara", "Chandragupta", "Dasharatha"], "answer": "a"}],
})


def test_index_then_pyq_page(sqlite_db):
    client = FakeClient([INDEX, PYQ])
    saved = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    assert saved.kind == "index" and "2 chapters" in saved.summary and not saved.needs_answer

    pyq = run(library.save_photo(client, 1, b"img", "F2", "U2", batch_id="b2"))
    assert pyq.kind == "pyq" and "1 PYQ" in pyq.summary
    stored = run(sqlite_db.find_pyq(1, 14))
    assert stored["exam"] == "SI" and stored["topic"] == "The Mauryan Age"  # mapped by similarity


def test_same_photo_twice_is_not_stored_again(sqlite_db):
    client = FakeClient([INDEX, INDEX])
    run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    again = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b2"))
    assert again.duplicate and again.page_id is None


def test_question_page_needs_answer(sqlite_db):
    client = FakeClient([json.dumps({"kind": "question", "confidence": 0.9, "text": "15% of 640?"})])
    saved = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    assert saved.needs_answer and saved.summary == "" and saved.page_id is not None


def test_focus_expires():
    chat_data = {}
    library.set_focus(chat_data, topic="Mauryan Age", page_id=3)
    assert library.get_focus(chat_data)["page_id"] == 3
    chat_data[library.FOCUS_KEY]["until"] = 0
    assert library.get_focus(chat_data) == {}


def test_render_pyq():
    text = library.render_pyq({"number": 14, "exam": "SI", "year": 2019, "question": "Who?",
                               "options": ["A", "B"], "answer": "a"})
    assert text.splitlines()[0] == "SI 2019 · Q14"
    assert "(a) A" in text and "(b) B" in text
```

- [ ] **Step 2: Run to see it fail** → no module.
- [ ] **Step 3: Implement `library.py`**

```python
"""Store every photo: work out what it is, file it in the right tables, index it for search."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

import db
import mcq
import retrieval
import router
import syllabus
from ai_client import AIError, NebiusClient
from router import PageRead

logger = logging.getLogger(__name__)

FOCUS_KEY = "focus"
FOCUS_SECONDS = 60 * 60
QUESTIONS_PER_PAGE = 8
ANSWER_KINDS = ("question", "other")
KIND_LABELS = {
    "cover": "cover", "index": "index", "pyq": "PYQ", "content": "notes",
    "question": "question", "other": "other",
}


@dataclass
class Saved:
    kind: str
    page_id: int | None
    chapter_id: int | None
    topic: str
    summary: str  # one line for the user; empty when the answer itself is the reply
    read: PageRead
    needs_answer: bool
    duplicate: bool = False


def new_batch_id() -> str:
    return secrets.token_hex(4)


# ------------------------------------------------------------------ focus


def get_focus(chat_data: dict | None) -> dict:
    if chat_data is None:
        return {}
    focus = chat_data.get(FOCUS_KEY)
    if not isinstance(focus, dict) or focus.get("until", 0) < time.time():
        chat_data.pop(FOCUS_KEY, None)
        return {}
    return focus


def set_focus(chat_data: dict | None, **fields) -> None:
    if chat_data is None:
        return
    focus = dict(get_focus(chat_data))
    focus.update({k: v for k, v in fields.items() if v not in (None, "")})
    focus["until"] = time.time() + FOCUS_SECONDS
    chat_data[FOCUS_KEY] = focus


def focus_from_saved(chat_data: dict | None, saved: Saved) -> None:
    if saved.page_id is None:
        return
    set_focus(
        chat_data,
        page_id=saved.page_id,
        chapter_id=saved.chapter_id,
        book=saved.read.book,
        topic=saved.topic,
        kind=saved.kind,
    )


def focus_summary(focus: dict) -> str:
    if not focus:
        return ""
    parts = [f"last photo: {KIND_LABELS.get(focus.get('kind', ''), 'page')} page"]
    if focus.get("topic"):
        parts.append(f"topic {focus['topic']}")
    if focus.get("book"):
        parts.append(syllabus.book_title(focus["book"]))
    return ", ".join(parts)


# ------------------------------------------------------------------ saving


async def _book_id(user_id: int, read: PageRead) -> int:
    """The page's book, or a per-subject catch-all so index/PYQ pages always have one."""
    if read.book:
        return await db.upsert_book(
            user_id, read.book, syllabus.book_title(read.book), syllabus.book_subject(read.book)
        )
    subject = read.subject
    return await db.upsert_book(user_id, f"other-{subject.lower()}", f"{subject} (other book)", subject)


async def _safe_index(client, user_id, source, source_id, texts, subject, topic, batch_id) -> bool:
    try:
        await retrieval.index_texts(client, user_id, source, source_id, texts, subject, topic, batch_id)
        return True
    except AIError as exc:
        logger.error("Indexing %s %s failed: %s", source, source_id, exc.detail)
        return False


async def save_photo(
    client: NebiusClient,
    user_id: int,
    image_bytes: bytes,
    file_id: str,
    file_unique_id: str,
    caption: str = "",
    forced_kind: str | None = None,
    batch_id: str = "",
) -> Saved:
    """Read one photo, store it and everything on it, and describe what was stored."""
    batch_id = batch_id or new_batch_id()
    read = await router.read_page(client, image_bytes, caption, forced_kind)
    wants_answer = read.kind in ANSWER_KINDS or bool(caption.strip())

    subject = read.subject
    if subject == "General" and read.book:
        subject = syllabus.book_subject(read.book)
        read.subject = subject
    book_id = await _book_id(user_id, read) if read.book or read.kind in ("index", "pyq") else None
    chapter = (
        await db.find_chapter_for_page(user_id, book_id, read.page_no)
        if book_id and read.page_no
        else None
    )
    chapter_id = chapter["id"] if chapter else None
    topic = (chapter["title"] if chapter else "") or read.topic

    page_id = await db.add_page(user_id, {
        "book_id": book_id, "chapter_id": chapter_id, "page_no": read.page_no,
        "kind": read.kind, "subject": subject, "topic": topic, "text": read.text,
        "file_id": file_id, "file_unique_id": file_unique_id,
    }, batch_id)
    if page_id is None:
        return Saved(read.kind, None, chapter_id, topic, "Already saved this photo earlier.",
                     read, wants_answer, duplicate=True)

    indexed = await _safe_index(client, user_id, "page", page_id,
                                retrieval.chunk_text(read.text), subject, topic, batch_id)

    if read.kind == "cover":
        summary = await _save_cover(read)
    elif read.kind == "index":
        summary = await _save_index(client, user_id, book_id, read, batch_id)
    elif read.kind == "pyq":
        summary, topic = await _save_pyqs(client, user_id, book_id, chapter_id, page_id, read, topic, batch_id)
    elif read.kind == "content":
        summary = await _save_content(client, user_id, chapter_id, read, topic, batch_id)
    else:
        summary = ""
    if summary and not indexed:
        summary += " (Search index missed this one — it's saved, just not searchable.)"
    return Saved(read.kind, page_id, chapter_id, topic, summary, read, wants_answer)


async def _save_cover(read: PageRead) -> str:
    if read.book:
        return f"📘 {syllabus.book_title(read.book)}. Send the index pages next."
    return f"📘 Saved the cover (filed under {read.subject})."


async def _save_index(client, user_id, book_id, read: PageRead, batch_id) -> str:
    title = syllabus.book_title(read.book) if read.book else f"your {read.subject} book"
    added = 0
    for chapter in read.chapters:
        chapter_id, created = await db.upsert_chapter(user_id, book_id, chapter, batch_id)
        if not created:
            continue
        added += 1
        number = f"Chapter {chapter['number']}: " if chapter.get("number") else ""
        topics = f" Topics: {', '.join(chapter['topics'])}." if chapter.get("topics") else ""
        await _safe_index(client, user_id, "chapter", chapter_id,
                          [f"{title} — {number}{chapter['title']}.{topics}"],
                          read.subject, chapter["title"], batch_id)
    names = ", ".join(c["title"] for c in read.chapters[:3])
    more = "…" if len(read.chapters) > 3 else ""
    if not read.chapters:
        return f"🗂 Saved the index page of {title}, but couldn't read any chapters on it."
    updated = len(read.chapters) - added
    tail = f" ({updated} already known, updated)" if updated else ""
    return f"🗂 Saved {len(read.chapters)} chapters of {title}: {names}{more}{tail}"


async def _save_pyqs(client, user_id, book_id, chapter_id, page_id, read: PageRead, topic, batch_id):
    texts = [
        " ".join([q["question"], *q["options"], q["answer"]]).strip() for q in read.pyqs
    ]
    try:
        vectors = await retrieval.embed(client, texts)
    except AIError as exc:
        logger.error("Embedding PYQs failed: %s", exc.detail)
        vectors = [None] * len(texts)

    saved, duplicates, topics = 0, 0, []
    for q, text, vector in zip(read.pyqs, texts, vectors):
        q_chapter, q_topic = chapter_id, topic
        if vector is not None:
            nearest = await db.search_chunks(user_id, vector, 1, ["pyq"])
            if nearest and nearest[0]["score"] >= retrieval.DUPLICATE_SCORE:
                duplicates += 1
                continue
            if q_chapter is None:
                match = await db.search_chunks(user_id, vector, 1, ["chapter"])
                if match and match[0]["score"] >= retrieval.CHAPTER_MATCH_SCORE:
                    q_chapter, q_topic = match[0]["source_id"], match[0]["topic"]
        q_topic = q_topic or read.topic or read.subject
        pyq_id = await db.add_pyq(user_id, {
            **q, "book_id": book_id, "chapter_id": q_chapter, "page_id": page_id,
            "subject": read.subject, "topic": q_topic,
        }, batch_id)
        if pyq_id is None:
            duplicates += 1
            continue
        saved += 1
        topics.append(q_topic)
        if vector is not None:
            await db.add_chunks(user_id, "pyq", pyq_id, [(text, vector)], read.subject, q_topic, batch_id)

    exam = " ".join(str(x) for x in (read.exam, read.year) if x)
    exam = f" ({exam})" if exam else ""
    top_topics = ", ".join(dict.fromkeys(topics)) or read.subject
    summary = f"📝 Saved {saved} PYQ{'s' if saved != 1 else ''}{exam} — {top_topics}."
    if duplicates:
        summary += f" {duplicates} already saved."
    return summary, (topics[0] if topics else topic)


async def _save_content(client, user_id, chapter_id, read: PageRead, topic, batch_id) -> str:
    label = topic or read.subject
    if not read.text:
        return f"📖 Saved a {label} page, but couldn't read its text."
    try:
        questions, _ = await mcq.generate_from_text(client, read.text, QUESTIONS_PER_PAGE, hint=label)
    except (AIError, mcq.MCQError) as exc:
        logger.error("MCQ generation failed: %s", getattr(exc, "detail", exc))
        questions = []
    added = await db.add_questions(user_id, questions, source=label, batch_id=batch_id,
                                   chapter_id=chapter_id) if questions else 0
    practice = f", {added} practice questions added" if added else ""
    return f"📖 {label} — saved{practice}."


# ------------------------------------------------------------ using the library


async def lookup_pyq(user_id: int, number: int, focus: dict) -> dict | None:
    return await db.find_pyq(user_id, number, focus.get("page_id"), focus.get("chapter_id"))


def render_pyq(pyq: dict) -> str:
    head = " ".join(str(x) for x in (pyq.get("exam"), pyq.get("year")) if x)
    number = f"Q{pyq['number']}" if pyq.get("number") is not None else "PYQ"
    lines = [f"{head} · {number}" if head else number, pyq["question"]]
    for letter, option in zip("abcde", pyq.get("options") or []):
        lines.append(f"({letter}) {option}")
    if pyq.get("answer"):
        lines.append(f"Printed answer: {pyq['answer']}")
    return "\n".join(lines)


async def grounded_prompt(
    client: NebiusClient, user_id: int, question: str, focus: dict
) -> tuple[str, list[dict]]:
    """Prefix the question with the closest material from the user's own saved pages."""
    query = f"{question}\n{focus.get('topic', '')}".strip()
    hits = await retrieval.search(client, user_id, query)
    if not hits:
        return question, []
    prompt = (
        "Material from the user's own books and saved PYQs. Use it when it is relevant, "
        "cite it as [n], and ignore it when it is not:\n\n"
        f"{retrieval.render_context(hits)}\n\nThe user's message:\n{question}"
    )
    return prompt, hits


def render_similar(hits: list[dict], limit: int = 3) -> str:
    pyqs = [h for h in hits if h["source"] == "pyq" and h["score"] >= 0.6][:limit]
    if not pyqs:
        return ""
    lines = ["", "", "Asked before:"]
    for hit in pyqs:
        snippet = hit["text"][:120] + ("…" if len(hit["text"]) > 120 else "")
        lines.append(f"• {snippet}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests** → `python -m pytest -v` all pass.
- [ ] **Step 5: Commit**

```bash
git add library.py tests/test_library.py
git commit -m "Add the library: save every photo by kind, focus, grounded prompts"
```

---

### Task 11: Photos in `main.py`

**Files:**

- Modify: `main.py`

- [ ] **Step 1: Imports and constants**

Add imports: `import library`, `import ranking`, `import retrieval`, `import router`, and `from telegram import InlineKeyboardButton, InlineKeyboardMarkup` (merge into the existing `from telegram import Update`). Delete `ADD_MODE_KEY`, `ADD_MODE_TIMEOUT_SECONDS`, `QUESTIONS_PER_PAGE`, `add_command`, `done_command`, `_add_mode`, `_handle_page_photo`, `help_command`, `reset`, `HELP_EXTRA`.

- [ ] **Step 2: Replace the photo pipeline**

Replace `handle_photo`, `_flush_album` (keep `_queue_album_photo` as is) and `_solve_photos` with:

```python
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every photo is stored and indexed; questions on it get answered."""
    message = update.effective_message
    if message is None or not message.photo:
        return
    if message.media_group_id:
        _queue_album_photo(message, context)
        return
    await _process_photos(context, [message])


async def _flush_album(key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wait for the album to go quiet, then process every photo in it together."""
    while True:
        delay = _pending_albums[key]["last"] + ALBUM_SETTLE_SECONDS - time.monotonic()
        if delay <= 0:
            break
        await asyncio.sleep(delay)
    album = _pending_albums.pop(key)
    await _process_photos(context, sorted(album["messages"], key=lambda m: m.message_id))


async def _save_photo_message(context, message, user_id: int, caption: str, batch_id: str):
    image = await _download_photo(message, context)
    if image is None:
        return None
    photo = message.photo[-1]
    try:
        saved = await library.save_photo(
            _client(context), user_id, image, photo.file_id, photo.file_unique_id,
            caption=caption, batch_id=batch_id,
        )
    except AIError as exc:
        logger.error("Saving a photo failed: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return None
    return saved, image


async def _process_photos(context: ContextTypes.DEFAULT_TYPE, messages: list) -> None:
    """Save every photo (one batch), report what was filed, then answer if asked."""
    captioned = [m for m in messages if (m.caption or "").strip()]
    anchor = captioned[0] if captioned else messages[0]
    caption = (anchor.caption or "").strip()[:MAX_PROMPT_CHARS]
    user = anchor.from_user
    if user is None or context.chat_data is None:
        return

    batch_id = library.new_batch_id()
    logger.info("Saving %d photo(s) from chat %s", len(messages), anchor.chat_id)
    async with typing(context, anchor.chat_id):
        results = await asyncio.gather(
            *(_save_photo_message(context, m, user.id, caption, batch_id) for m in messages)
        )
    done = [r for r in results if r is not None]
    if not done:
        return
    for saved, _ in done:
        library.focus_from_saved(context.chat_data, saved)
    await _reply_saved(anchor, done, batch_id)

    if caption:
        focus = library.get_focus(context.chat_data)
        intent = router.quick_intent(caption) or await router.classify_text(
            _client(context), caption, library.focus_summary(focus)
        )
        if intent.name != "solve":
            await _TEXT_ROUTES[intent.name](anchor, context, user.id, caption, intent, focus)
            return
        to_answer = done
    else:
        to_answer = [(s, img) for s, img in done if s.needs_answer]
    if not to_answer:
        return

    question = caption or IMAGE_DEFAULT_PROMPT
    page_text = "\n\n".join(s.read.text for s, _ in to_answer if s.read.text)[:3000]
    if page_text:
        question += f"\n\nText read from the photo(s):\n{page_text}"
    await _grounded_answer(
        anchor, context, user.id, question,
        remember_as=f"[photo] {caption or 'solve this'}",
        images=[img for _, img in to_answer],
    )


def _undo_button(batch_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("↩️ Undo", callback_data=f"u:{batch_id}")


def _kind_keyboard(batch_id: str, page_id: int) -> InlineKeyboardMarkup:
    kinds = ("index", "pyq", "content", "question", "cover", "other")
    buttons = [
        InlineKeyboardButton(library.KIND_LABELS[k].capitalize(), callback_data=f"k:{batch_id}:{page_id}:{k}")
        for k in kinds
    ]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:], [_undo_button(batch_id)]])


async def _reply_saved(anchor, done: list, batch_id: str) -> None:
    lines = [saved.summary for saved, _ in done if saved.summary]
    if not lines:
        return
    fresh = [saved for saved, _ in done if saved.page_id is not None]
    if not fresh:
        await anchor.reply_text("\n".join(lines))
        return
    single = fresh[0] if len(done) == 1 else None
    if single is not None and single.read.confidence < router.LOW_CONFIDENCE:
        lines.append(f"\nNot sure this is a {library.KIND_LABELS[single.kind]} page — what is it?")
        markup = _kind_keyboard(batch_id, single.page_id)
    elif single is not None:
        markup = InlineKeyboardMarkup([[
            _undo_button(batch_id),
            InlineKeyboardButton("🔁 Wrong type", callback_data=f"w:{batch_id}:{single.page_id}"),
        ]])
    else:
        markup = InlineKeyboardMarkup([[_undo_button(batch_id)]])
    await anchor.reply_text("\n".join(lines), reply_markup=markup)


async def _grounded_answer(
    message, context, user_id: int, question: str, remember_as: str, images: list | None = None
) -> None:
    """Answer with the user's own saved material retrieved into the prompt."""
    client = _client(context)
    focus = library.get_focus(context.chat_data)
    try:
        async with typing(context, message.chat_id):
            prompt, hits = await library.grounded_prompt(client, user_id, question, focus)
            if images:
                answer = await client.ask_image(prompt, images, "image/jpeg", history=_history(context))
            else:
                answer = await client.ask_text(prompt, history=_history(context))
    except AIError as exc:
        logger.error("AI error on answer: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return
    _remember(context, remember_as, answer)
    await _send_answer_to(message, answer + library.render_similar(hits))
```

- [ ] **Step 3: Button callbacks**

```python
async def on_library_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo a save, show the kind picker, or re-read a photo as a chosen kind."""
    query, user = update.callback_query, update.effective_user
    if query is None or user is None:
        return
    parts = (query.data or "").split(":")

    if parts[0] == "u" and len(parts) == 2:
        removed = await db.undo_batch(user.id, parts[1])
        await query.answer("Undone." if removed else "Nothing left to undo.")
        text = (query.message.text if query.message else "") or ""
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(f"{text}\n\n↩️ Undone.", reply_markup=None)
        return

    if parts[0] == "w" and len(parts) == 3 and parts[2].isdigit():
        await query.answer()
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(_kind_keyboard(parts[1], int(parts[2])))
        return

    if parts[0] == "k" and len(parts) == 4 and parts[2].isdigit() and parts[3] in router.PAGE_KINDS:
        await _reread_as(query, context, user.id, parts[1], int(parts[2]), parts[3])
        return

    await query.answer()


async def _reread_as(query, context, user_id: int, batch_id: str, page_id: int, kind: str) -> None:
    page = await db.get_page(user_id, page_id)
    if page is None:
        await query.answer("That photo was already removed.", show_alert=True)
        return
    await query.answer(f"Re-reading as {library.KIND_LABELS[kind]}…")
    try:
        telegram_file = await context.bot.get_file(page["file_id"])
        raw = bytes(await telegram_file.download_as_bytearray())
        image, _ = await asyncio.to_thread(_compress_image, raw)
    except (TelegramError, UnidentifiedImageError, OSError, ValueError) as exc:
        logger.error("Re-download failed: %s", exc)
        await query.answer(DOWNLOAD_ERROR, show_alert=True)
        return

    await db.undo_batch(user_id, batch_id)
    new_batch = library.new_batch_id()
    try:
        saved = await library.save_photo(
            _client(context), user_id, image, page["file_id"], page["file_unique_id"],
            forced_kind=kind, batch_id=new_batch,
        )
    except AIError as exc:
        logger.error("Re-read failed: %s", exc.detail)
        await query.answer(exc.user_message, show_alert=True)
        return
    library.focus_from_saved(context.chat_data, saved)
    text = saved.summary or f"Saved as a {library.KIND_LABELS[kind]}."
    with contextlib.suppress(TelegramError):
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup([[_undo_button(new_batch)]])
        )
    if saved.needs_answer and query.message is not None:
        await _grounded_answer(
            query.message, context, user_id,
            f"{IMAGE_DEFAULT_PROMPT}\n\nText read from the photo:\n{saved.read.text[:3000]}",
            remember_as="[photo] solve this", images=[image],
        )
```

Register in `build_application` before the message handlers:

```python
    application.add_handler(CallbackQueryHandler(on_library_button, pattern=r"^[uwk]:"))
```

- [ ] **Step 4: Import check**

Run: `python -c "import main"` — will fail until Task 12 defines `_TEXT_ROUTES`; that is expected. Do not commit yet; continue to Task 12.

---

### Task 12: Text routing and removing commands

**Files:**

- Modify: `main.py`, `quiz.py`

- [ ] **Step 1: Replace `handle_text` and add the routes**

```python
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or context.chat_data is None:
        return
    user_text = (message.text or "").strip()
    if not user_text:
        await message.reply_text(EMPTY_TEXT_REPLY)
        return
    user_text = user_text[:MAX_PROMPT_CHARS]

    focus = library.get_focus(context.chat_data)
    intent = router.quick_intent(user_text)
    if intent is None:
        async with typing(context, message.chat_id):
            intent = await router.classify_text(_client(context), user_text, library.focus_summary(focus))
    logger.info("Text from chat %s routed to %s", message.chat_id, intent.name)
    await _TEXT_ROUTES.get(intent.name, _route_solve)(message, context, user.id, user_text, intent, focus)


async def _route_solve(message, context, user_id, text, intent, focus) -> None:
    await _grounded_answer(message, context, user_id, text, remember_as=text)


async def _route_study_plan(message, context, user_id, text, intent, focus) -> None:
    units = ranking.build_units(await db.pyq_counts(user_id), await db.topic_progress(user_id))
    await message.reply_text(ranking.render_plan(units))


async def _route_lookup(message, context, user_id, text, intent, focus) -> None:
    if intent.number is None:
        page = await db.get_page(user_id, focus["page_id"]) if focus.get("page_id") else None
        if page is None:
            await _route_solve(message, context, user_id, text, intent, focus)
            return
        question = f"{text}\n\nThe page the user means:\n{page['text']}"
    else:
        pyq = await library.lookup_pyq(user_id, intent.number, focus)
        if pyq is None:
            await message.reply_text(
                f"I don't have Q{intent.number} saved yet. Send a photo of that page and ask again."
            )
            return
        library.set_focus(context.chat_data, chapter_id=pyq.get("chapter_id"),
                          page_id=pyq.get("page_id"), topic=pyq.get("topic"))
        question = f"{library.render_pyq(pyq)}\n\n{text}"
    await _grounded_answer(message, context, user_id, question, remember_as=text)


async def _route_search(message, context, user_id, text, intent, focus) -> None:
    client = _client(context)
    async with typing(context, message.chat_id):
        hits = await retrieval.search(client, user_id, intent.query or text, k=8)
    if not hits:
        await message.reply_text("Nothing about that in what you've saved yet.")
        return
    prompt = (
        "Answer using ONLY the material below from the user's saved pages and PYQs. "
        "List any matching PYQs with their exam and year. Cite as [n].\n\n"
        f"{retrieval.render_context(hits)}\n\nThe user's request:\n{text}"
    )
    try:
        async with typing(context, message.chat_id):
            answer = await client.ask_text(prompt, history=_history(context))
    except AIError as exc:
        logger.error("AI error on search: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return
    _remember(context, text, answer)
    await _send_answer_to(message, answer)


async def _route_quiz(message, context, user_id, text, intent, focus) -> None:
    count = DEFAULT_QUIZ_LENGTH
    questions = await db.pick_quiz(user_id, count, intent.subject)
    if not questions:
        where = f" in {intent.subject}" if intent.subject else ""
        await message.reply_text(
            f"No practice questions{where} yet. Send photos of your notes pages and I'll make some."
        )
        return
    session = quiz.start_session(context.chat_data, questions, intent.subject)
    await _send_current_question(message, session)


async def _route_stats(message, context, user_id, text, intent, focus) -> None:
    await _reply_md(message, quiz.render_stats(await db.stats(user_id)))
    weak = await db.weak_topics(user_id)
    if weak:
        await _reply_md(message, quiz.render_weak(weak))


async def _route_daily(message, context, user_id, text, intent, focus) -> None:
    if intent.hour is None:
        await message.reply_text("What time? For example: “daily quiz at 6am”.")
        return
    await db.set_daily(user_id, message.chat_id, intent.hour, DEFAULT_QUIZ_LENGTH)
    _schedule_daily(context.application, user_id, message.chat_id, intent.hour, DEFAULT_QUIZ_LENGTH)
    await message.reply_text(
        f"⏰ Daily quiz set: {DEFAULT_QUIZ_LENGTH} questions at {intent.hour:02d}:00 IST. "
        "Say “stop daily quiz” to cancel."
    )


async def _route_daily_off(message, context, user_id, text, intent, focus) -> None:
    await db.set_daily(user_id, message.chat_id, None)
    for job in context.application.job_queue.get_jobs_by_name(f"daily-{user_id}"):
        job.schedule_removal()
    await message.reply_text("Daily quiz cancelled.")


_TEXT_ROUTES = {
    "solve": _route_solve,
    "study_plan": _route_study_plan,
    "lookup": _route_lookup,
    "search": _route_search,
    "quiz": _route_quiz,
    "stats": _route_stats,
    "daily": _route_daily,
    "daily_off": _route_daily_off,
}
```

Delete `quiz_command`, `stats_command`, `weak_command`, `bank_command`, `daily_command`, `nodaily_command`, and `MAX_QUIZ_LENGTH` if now unused. In `on_answer`, change the ended-quiz alert to `"That quiz has ended. Say “quiz me” to start a new one."`.

- [ ] **Step 2: Welcome text and handlers**

```python
WELCOME = (
    "🎯 *TSLPRB Prep Bot*\n\n"
    "No commands — just send photos and ask\\.\n\n"
    "📷 *Photos* of your index pages, PYQ pages, notes or a question — "
    "I file every one by book, chapter and topic\\.\n\n"
    "*Then ask things like*\n"
    "• what should I study?\n"
    "• help with Q14\n"
    "• PYQs on Ashoka\n"
    "• quiz me on polity\n"
    "• how am I doing?\n"
    "• daily quiz at 6am"
)
```

`build_application` keeps only:

```python
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(on_answer, pattern=r"^q:"))
    application.add_handler(CallbackQueryHandler(on_library_button, pattern=r"^[uwk]:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_error_handler(on_error)
```

Remove now-unused imports (`SUBJECTS` from `exam_prompts`, `mcq` if unused) — check with `python -m pyflakes main.py` if available, else by reading.

- [ ] **Step 3: `quiz.py` text**

Run: `grep -n "/[a-z]" quiz.py`. Replace every user-facing `/quiz`, `/weak`, `/stats`, `/add` mention with the phrasing the user would type ("say “quiz me”", "send photos of your notes pages").

- [ ] **Step 4: Verify**

Run: `python -c "import main"` → no error.
Run: `python -m pytest -v` → all pass.
Run: `grep -n "add_mode\|_add_mode\|/add\|/quiz\|/done" main.py quiz.py` → no matches.

- [ ] **Step 5: Commit**

```bash
git add main.py quiz.py
git commit -m "Route every photo and message automatically; drop slash commands"
```

---

### Task 13: BotFather menu, README, end-to-end check

**Files:**

- Modify: `setup_botfather.py`, `README.md`

- [ ] **Step 1: `setup_botfather.py`**

```python
COMMANDS = [
    {"command": "start", "description": "What this bot does"},
]

DESCRIPTION = (
    "TSLPRB SI/PC prep. Send photos of your books — index pages, PYQs, notes — and I file "
    "them by chapter, rank what to study from real PYQs, and answer from your own pages."
)

SHORT_DESCRIPTION = "TSLPRB SI/PC prep that learns your books."
```

- [ ] **Step 2: README**

Replace the usage/commands section with the "no commands" flow from the welcome text, and add under configuration:

```
NEBIUS_EMBED_MODEL   optional, default Qwen/Qwen3-Embedding-8B
```

and a note: "On Postgres the bot runs `CREATE EXTENSION IF NOT EXISTS vector` at startup (Neon and Supabase both include pgvector)."

- [ ] **Step 3: End-to-end against the real database**

⚠️ This creates the pgvector extension and five new tables in the production database (additive; existing tables only gain two nullable/defaulted columns). Confirm with the user first.

Run: `stop_bot.bat`, then `python main.py`
Expected log lines: `Postgres pool ready`, `Storage backend: postgres`, no traceback.

In Telegram:

1. Send the Karim index photo → reply "🗂 Saved N chapters of Indian History — M. Abdul Kareem…" with Undo / Wrong type.
2. Send a PYQ page → "📝 Saved N PYQs (SI 2019) — …".
3. Type "Q14" → the question from that page, answered.
4. Type "what should I study" → estimate-mode plan listing Reasoning/Telangana as no-book gaps.
5. Tap ↩️ Undo on step 2 → "↩️ Undone."; "Q14" now says it isn't saved.

- [ ] **Step 4: Commit**

```bash
git add setup_botfather.py README.md
git commit -m "Update the bot menu and README for the command-free flow"
```

Then run `python setup_botfather.py` to push the new menu.
