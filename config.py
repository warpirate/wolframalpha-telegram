"""Environment configuration for the bot.

All settings come from environment variables (optionally loaded from a local
`.env` file). Missing required values fail loudly at import time so the bot
never starts in a half-configured state.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _is_placeholder(value: str) -> bool:
    """True for the untouched .env.example values (your_..._here, changeme, ...)."""
    lowered = value.lower()
    if lowered in ("changeme", "none", "null", "todo", "xxx"):
        return True
    return lowered.startswith("your_") and lowered.endswith("_here")


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill in your keys."
        )
    if _is_placeholder(value):
        raise RuntimeError(
            f"{name} is still set to the placeholder from .env.example. "
            "Put the real value in .env before starting the bot."
        )
    return value


def _optional(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


TELEGRAM_BOT_TOKEN: str = _required("TELEGRAM_BOT_TOKEN")
NEBIUS_API_KEY: str = _required("NEBIUS_API_KEY")
NEBIUS_BASE_URL: str = _optional("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1").rstrip("/")
NEBIUS_MODEL: str = _optional("NEBIUS_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash")
# Images go to the same model unless a dedicated vision model is configured.
# Set this if NEBIUS_MODEL is a text-only model.
NEBIUS_VISION_MODEL: str = _optional("NEBIUS_VISION_MODEL", NEBIUS_MODEL)
# Embeddings for searching the user's saved pages and PYQs.
NEBIUS_EMBED_MODEL: str = _optional("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")

# Storage backend. Unset means a local SQLite file (laptop); set to a Postgres
# connection string to use a hosted database (cloud hosts with no persistent disk).
DATABASE_URL: str = _optional("DATABASE_URL", "")

# Set by hosts like Render that require the process to bind a port. When present,
# the bot also serves a tiny health endpoint so the platform sees it as alive.
PORT: int = int(_optional("PORT", "0") or 0)

# Access control. With BOT_PASSWORD set, a new user must send it once and then
# give a name before the bot does anything for them. Unset means anyone can use it.
BOT_PASSWORD: str = _optional("BOT_PASSWORD", "")
# Telegram user ids (comma separated) that skip the password, get told about each
# new user, and can send /users to see everyone registered.
ADMIN_USER_IDS: frozenset[int] = frozenset(
    int(part) for part in _optional("ADMIN_USER_IDS", "").replace(" ", "").split(",") if part.isdigit()
)

# Optional tuning knobs (safe defaults, no validation errors if unset).
LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()
HISTORY_TURNS: int = max(0, int(_optional("HISTORY_TURNS", "4")))

__all__ = [
    "TELEGRAM_BOT_TOKEN",
    "NEBIUS_API_KEY",
    "NEBIUS_BASE_URL",
    "NEBIUS_MODEL",
    "NEBIUS_VISION_MODEL",
    "NEBIUS_EMBED_MODEL",
    "DATABASE_URL",
    "PORT",
    "BOT_PASSWORD",
    "ADMIN_USER_IDS",
    "LOG_LEVEL",
    "HISTORY_TURNS",
]
