"""Async wrapper around the Nebius Token Factory (OpenAI-compatible) API.

Supports plain text chat completions and multimodal (vision) completions where
the image is inlined as a base64 data URL.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Iterable

import httpx

from prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 120.0  # reasoning models need room; empty-answer retry doubles this
MAX_ATTEMPTS = 4  # 1 initial attempt + 3 retries
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
TEMPERATURE = 0.3
# DeepSeek-V4.1-Flash is a reasoning model: hidden reasoning tokens are billed
# against max_tokens. With a small budget an open-ended question can spend the
# entire allowance thinking and return an empty answer, so keep real headroom.
MAX_TOKENS = 4000
RETRY_MAX_TOKENS = 6000
MAX_HISTORY_MESSAGES = 12

FRIENDLY_TIMEOUT = "The model took too long to answer. Please try again in a moment."
FRIENDLY_NETWORK = "I couldn't reach the AI service. Please try again in a moment."
FRIENDLY_RATE_LIMIT = "The AI service is rate-limiting right now. Please try again shortly."
FRIENDLY_SERVER = "The AI service is having trouble right now. Please try again shortly."
FRIENDLY_AUTH = "The AI service rejected my credentials. Please check the NEBIUS_API_KEY."
FRIENDLY_TOO_LARGE = "That input was too large for the model. Try a smaller image or a shorter question."
FRIENDLY_GENERIC = "The AI service returned an unexpected error. Please try again."
FRIENDLY_EMPTY = "The model returned an empty answer. Please rephrase and try again."


class AIError(Exception):
    """Raised for any AI failure, carrying a message safe to show to the user."""

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail or user_message


class NebiusClient:
    """Minimal async client for the Nebius chat-completions endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        vision_model: str | None = None,
    ) -> None:
        self.model = model
        # Images use the same model unless a dedicated vision model is given.
        self.vision_model = vision_model or model
        self._base_url = base_url.rstrip("/")
        self._endpoint = f"{self._base_url}/chat/completions"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # ------------------------------------------------------------------ public

    async def ask_text(self, user_text: str, history: list[dict] | None = None) -> str:
        """Answer a plain text question, optionally with prior conversation turns."""
        user_text = (user_text or "").strip()
        if not user_text:
            raise AIError("Send me a question and I'll answer it.")

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_sanitize_history(history))
        messages.append({"role": "user", "content": user_text})
        return await self._complete(messages)

    async def ask_image(
        self,
        user_text: str,
        images: bytes | list[bytes],
        mime_type: str,
        history: list[dict] | None = None,
    ) -> str:
        """Answer a question about one or more images, sent as inline base64 data URLs.

        Several images (a Telegram album) go in a single user turn, so the model
        answers the question once with every page in view.
        """
        if isinstance(images, (bytes, bytearray)):
            images = [bytes(images)]
        images = [img for img in images if img]
        if not images:
            raise AIError("I couldn't read that image. Please send it again.")

        prompt = (user_text or "").strip() or "Solve or explain what is in this image."
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_bytes in images:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
            )

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_sanitize_history(history))
        messages.append({"role": "user", "content": content})
        return await self._complete(messages, model=self.vision_model)

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = TEMPERATURE,
        max_tokens: int = MAX_TOKENS,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Run an arbitrary message list. Used by features with their own prompts.

        Unlike ask_text/ask_image this does not prepend the solver system prompt;
        the caller supplies the whole conversation.
        """
        if not messages:
            raise AIError("Nothing to send to the model.")
        has_image = any(isinstance(m.get("content"), list) for m in messages)
        payload: dict[str, Any] = {
            "model": model or (self.vision_model if has_image else self.model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        return await self._send(payload)

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

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    # ----------------------------------------------------------------- internal

    async def _complete(self, messages: list[dict[str, Any]], model: str | None = None) -> str:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }
        return await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> str:
        """POST once, and recover if reasoning consumed the whole token budget."""
        data = await self._post_with_retries(payload)
        content = _extract_content(data)
        if content:
            return content

        if _was_truncated(data):
            # The model thought until it ran out of room without writing an
            # answer. Retry with a bigger budget and reasoning switched off.
            logger.warning(
                "Empty answer (finish_reason=length, reasoning_tokens=%s); retrying without reasoning",
                _reasoning_tokens(data),
            )
            retry = dict(payload)
            retry["max_tokens"] = max(
                int(payload.get("max_tokens") or MAX_TOKENS) * 2, RETRY_MAX_TOKENS
            )
            retry["reasoning_effort"] = "none"
            try:
                data = await self._post_with_retries(retry)
            except AIError:
                # Some models reject reasoning_effort outright - retry on budget alone.
                retry.pop("reasoning_effort", None)
                data = await self._post_with_retries(retry)
            content = _extract_content(data)
            if content:
                return content

        raise AIError(FRIENDLY_EMPTY, detail="Model returned no content")

    async def _post_with_retries(
        self, payload: dict[str, Any], endpoint: str | None = None
    ) -> dict[str, Any]:
        last_error: AIError | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(endpoint or self._endpoint, json=payload)
            except httpx.TimeoutException as exc:
                last_error = AIError(FRIENDLY_TIMEOUT, detail=f"timeout: {exc!r}")
            except httpx.HTTPError as exc:
                last_error = AIError(FRIENDLY_NETWORK, detail=f"transport error: {exc!r}")
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise AIError(
                            FRIENDLY_GENERIC, detail=f"invalid JSON from API: {exc!r}"
                        ) from exc

                status = response.status_code
                detail = f"HTTP {status}: {_safe_body(response)}"
                if status not in RETRYABLE_STATUS_CODES:
                    raise AIError(_friendly_for_status(status), detail=detail)
                last_error = AIError(_friendly_for_status(status), detail=detail)

            if attempt < MAX_ATTEMPTS - 1:
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "Nebius request failed (attempt %d/%d): %s - retrying in %.0fs",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    last_error.detail if last_error else "unknown",
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_error is not None  # loop always sets it before exhausting
        logger.error("Nebius request failed permanently: %s", last_error.detail)
        raise last_error


# ---------------------------------------------------------------------- helpers


def _was_truncated(data: dict[str, Any]) -> bool:
    """True when the model stopped because it hit max_tokens."""
    try:
        return data["choices"][0].get("finish_reason") == "length"
    except (KeyError, IndexError, TypeError):
        return False


def _reasoning_tokens(data: dict[str, Any]) -> Any:
    usage = data.get("usage") if isinstance(data, dict) else None
    return usage.get("reasoning_tokens") if isinstance(usage, dict) else None


def _friendly_for_status(status: int) -> str:
    if status in (401, 403):
        return FRIENDLY_AUTH
    if status == 413:
        return FRIENDLY_TOO_LARGE
    if status == 429:
        return FRIENDLY_RATE_LIMIT
    if status >= 500:
        return FRIENDLY_SERVER
    return FRIENDLY_GENERIC


def _safe_body(response: httpx.Response, limit: int = 300) -> str:
    """Short, log-safe excerpt of an error body (never contains our API key)."""
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit]


def _sanitize_history(history: Iterable[dict] | None) -> list[dict[str, Any]]:
    """Keep only well-formed user/assistant text turns, most recent last."""
    if not history:
        return []
    cleaned: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content})
    return cleaned[-MAX_HISTORY_MESSAGES:]


def _extract_content(data: dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenAI-style response body."""
    if not isinstance(data, dict):
        raise AIError(FRIENDLY_GENERIC, detail=f"unexpected response type: {type(data)!r}")

    error = data.get("error")
    if error:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise AIError(FRIENDLY_GENERIC, detail=f"API error: {detail}")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIError(FRIENDLY_GENERIC, detail="response contained no choices")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise AIError(FRIENDLY_GENERIC, detail="choice contained no message")

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    # Some providers return a content array of typed parts.
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts).strip()

    # Deliberately NOT falling back to message["reasoning_content"]: that is raw
    # chain of thought ("We need answer user asks...") and must never be shown.
    return ""


__all__ = ["NebiusClient", "AIError"]
