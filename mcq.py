"""Turn a photographed book page into validated multiple-choice questions."""

from __future__ import annotations

import base64
import logging
from typing import Any

from ai_client import AIError, NebiusClient
from exam_prompts import MCQ_SYSTEM_PROMPT, SUBJECTS, mcq_user_prompt
from jsonutil import JSONParseError, complete_json, loads_loose

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 400
MAX_OPTION_CHARS = 160
_SUBJECT_LOOKUP = {s.lower(): s for s in SUBJECTS}


class MCQError(Exception):
    """Raised when a page could not be turned into usable questions."""


def _find_question_list(node: Any, depth: int = 0) -> list | None:
    """Locate the list of question dicts inside whatever wrapper the model used.

    Models sometimes answer with a bare list, sometimes {"questions": [...]},
    and this one has been seen returning {"type": "json_object", "content": [...]}.
    """
    if depth > 4:
        return None
    if isinstance(node, list):
        if node and all(isinstance(i, dict) for i in node) and any(
            "question" in i for i in node
        ):
            return node
        return None
    if isinstance(node, dict):
        for key in ("questions", "content", "data", "items", "result", "mcqs"):
            if key in node:
                found = _find_question_list(node[key], depth + 1)
                if found is not None:
                    return found
        for value in node.values():
            found = _find_question_list(value, depth + 1)
            if found is not None:
                return found
    return None


def parse_questions(raw: str) -> tuple[list[dict], str]:
    """Parse and validate the model's JSON. Returns (questions, note)."""
    try:
        payload = loads_loose(raw)
    except JSONParseError as exc:
        raise MCQError("The model did not return usable JSON.") from exc

    note = ""
    if isinstance(payload, dict):
        raw_note = payload.get("note")
        if isinstance(raw_note, str):
            note = raw_note.strip()

    items = _find_question_list(payload)
    if items is None:
        return [], note or "No questions found on that page."

    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in items:
        question = _clean(item.get("question"), MAX_QUESTION_CHARS)
        options = item.get("options")
        if not question or not isinstance(options, list) or len(options) != 4:
            continue

        opts = [_clean(o, MAX_OPTION_CHARS) for o in options]
        if not all(opts) or len({o.lower() for o in opts}) != 4:
            continue  # blank or duplicate options make the question unusable

        try:
            correct = int(item.get("correct_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= correct <= 3:
            continue

        key = question.lower()
        if key in seen:
            continue
        seen.add(key)

        subject = _SUBJECT_LOOKUP.get(str(item.get("subject", "")).strip().lower(), "General")
        topic = _clean(item.get("topic"), 60) or "General"

        cleaned.append({
            "question": question,
            "options": opts,
            "correct_index": correct,
            "explanation": _clean(item.get("explanation"), 500),
            "subject": subject,
            "topic": topic,
        })

    return cleaned, note


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[:limit]


async def generate_from_image(
    client: NebiusClient,
    image_bytes: bytes,
    count: int = 8,
    hint: str = "",
) -> tuple[list[dict], str]:
    """Generate MCQs from a photographed page. Returns (questions, note)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": MCQ_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": mcq_user_prompt(count, hint)},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        },
    ]
    raw = await complete_json(client, messages)
    return parse_questions(raw)


async def generate_from_text(
    client: NebiusClient,
    passage: str,
    count: int = 8,
    hint: str = "",
) -> tuple[list[dict], str]:
    """Generate MCQs from pasted text."""
    messages = [
        {"role": "system", "content": MCQ_SYSTEM_PROMPT},
        {"role": "user", "content": f"{mcq_user_prompt(count, hint)}\n\nPAGE TEXT:\n{passage}"},
    ]
    raw = await complete_json(client, messages)
    return parse_questions(raw)

