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
