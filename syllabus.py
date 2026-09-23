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

# Estimated exam priority of each chapter, matched by keyword against the chapter
# titles read from the user's index pages. First match wins, so more specific
# keywords ("post-gupta") come before the general ones they contain ("gupta").
# Judgement calls from the usual police-exam pattern - always shown as estimates.
CHAPTER_PRIORITY: dict[str, list[tuple[str, str]]] = {
    "rs_aggarwal": [
        ("number system", "high"), ("h.c.f", "high"), ("simplification", "high"),
        ("average", "high"), ("percentage", "high"), ("profit", "high"),
        ("ratio", "high"), ("time and work", "high"), ("time and distance", "high"),
        ("trains", "high"), ("simple interest", "high"), ("compound interest", "high"),
        ("decimal", "medium"), ("square root", "medium"), ("problems on numbers", "medium"),
        ("ages", "medium"), ("partnership", "medium"), ("chain rule", "medium"),
        ("pipes", "medium"), ("boats", "medium"), ("alligation", "medium"),
        ("volume", "medium"), ("area", "medium"), ("calendar", "medium"), ("clocks", "medium"),
        ("odd man", "medium"), ("tabulation", "medium"), ("bar graph", "medium"),
        ("pie chart", "medium"), ("line graph", "medium"),
        ("surds", "low"), ("logarithm", "low"), ("races", "low"), ("stocks", "low"),
        ("permutation", "low"), ("probability", "low"), ("true discount", "low"),
        ("banker", "low"), ("height and distance", "low"),
    ],
    "karim": [
        ("pre-historic", "low"), ("sources", "low"),
        ("pre-mauryan", "high"), ("post-mauryan", "medium"), ("mauryan", "high"),
        ("post-gupta", "medium"), ("gupta", "high"),
        ("indus", "high"), ("vedic", "high"), ("aryan", "high"),
        ("satavahana", "high"), ("kakatiya", "high"), ("qutb", "high"), ("asaf jah", "high"),
        ("national movement", "high"), ("freedom", "high"), ("1857", "high"),
        ("gandhi", "high"), ("british", "high"), ("governor", "medium"),
        ("sultanate", "medium"), ("mughal", "medium"), ("vijayanagar", "medium"),
        ("bhakti", "medium"), ("miscellaneous", "medium"),
    ],
    "laxmikanth": [
        ("preamble", "high"), ("fundamental rights", "high"), ("directive", "high"),
        ("fundamental duties", "high"), ("amendment", "high"), ("emergency", "high"),
        ("president", "high"), ("prime minister", "high"), ("parliament", "high"),
        ("supreme court", "high"), ("panchayat", "high"), ("citizenship", "medium"),
        ("governor", "medium"), ("high court", "medium"), ("election", "medium"),
        ("municipalit", "medium"), ("historical background", "medium"),
        ("making of the constitution", "medium"), ("salient features", "medium"),
        ("union and its territory", "medium"), ("appendi", "low"), ("schedule", "low"),
    ],
    "lucent": [
        ("human", "high"), ("disease", "high"), ("vitamin", "high"), ("nutrition", "high"),
        ("blood", "high"), ("light", "high"), ("unit", "high"), ("electric", "medium"),
        ("sound", "medium"), ("heat", "medium"), ("motion", "medium"), ("acid", "medium"),
        ("metal", "medium"), ("cell", "medium"), ("plant", "medium"),
        ("computer", "low"), ("nuclear", "low"), ("fuel", "low"),
    ],
}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

_SUBJECT_LOOKUP = {s.lower(): s for s in SUBJECTS}


def chapter_priority(book_key: str, title: str) -> str:
    """Estimated priority of a chapter; 'medium' when no keyword matches."""
    lowered = (title or "").lower()
    for keyword, priority in CHAPTER_PRIORITY.get(book_key, []):
        if keyword in lowered:
            return priority
    return "medium"


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
