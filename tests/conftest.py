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
