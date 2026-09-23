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
