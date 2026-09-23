"""Rank chapters by PYQ weight, how much is covered and how weak the user is; render the plan."""

from __future__ import annotations

from dataclasses import dataclass

import syllabus

TOP_N = 8
PER_BOOK = 8
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


def _share(subject: str) -> str:
    return f"~{round(syllabus.ESTIMATED_SHARE.get(subject, 0) * 100)}%"


def _chapter_lines(chapters: list[dict], units: list[Unit]) -> list[str]:
    """Per saved book: estimated high-priority chapters first, low ones parked at the end."""
    by_key = {u.key: u for u in units}
    books: dict[str, list[dict]] = {}
    for chapter in chapters:
        books.setdefault(chapter["book_key"], []).append(chapter)
    order = sorted(books, key=lambda k: -syllabus.ESTIMATED_SHARE.get(books[k][0]["subject"], 0))

    lines: list[str] = []
    for book_key in order:
        rows = books[book_key]
        first = rows[0]
        rated = []
        for row in rows:
            unit = by_key.get(f"c{row['id']}")
            done = coverage(unit) if unit else 0.0
            priority = syllabus.chapter_priority(book_key, row["title"])
            rated.append((syllabus.PRIORITY_RANK[priority], done, row.get("number") or 999, row, priority))
        rated.sort(key=lambda item: item[:3])
        todo = [item for item in rated if item[4] != "low" and item[1] < 1.0]
        low = [item[3]["title"] for item in rated if item[4] == "low"]

        lines.append(f"📘 {first['book_title']} ({first['subject']} {_share(first['subject'])})")
        if todo:
            lines.append("Start with:")
            for i, (_, done, _, row, priority) in enumerate(todo[:PER_BOOK], 1):
                number = f" — ch {row['number']}" if row.get("number") else ""
                flag = " · in progress" if done else ""
                lines.append(f"{i}. {row['title']}{number}{flag}")
            extra = [t[3]["title"] for t in todo[PER_BOOK:] if t[4] == "high"]
            if extra:
                lines.append("Then: " + ", ".join(extra))
        if low:
            lines.append("Leave for last: " + ", ".join(low))
        lines.append("")
    return lines


def render_plan(units: list[Unit], chapters: list[dict] | None = None, exam: str = "SI") -> str:
    total = sum(u.pyqs for u in units)
    book_subjects = {b["subject"] for b in syllabus.BOOKS.values()}
    chapters = chapters or []
    lines: list[str] = []

    if total < syllabus.MIN_PYQS_FOR_REAL_WEIGHTS:
        if total == 0:
            lines.append("No PYQs saved yet, so this plan uses estimated priorities.")
        else:
            lines.append(
                f"Only {total} PYQ{'s' if total != 1 else ''} saved so far, so this plan uses "
                f"estimated priorities until about {syllabus.MIN_PYQS_FOR_REAL_WEIGHTS} are in."
            )
        lines.append("")
        if chapters:
            lines.extend(_chapter_lines(chapters, units))
        else:
            lines.append("Share of the paper by subject (estimate):")
            for subject, share in sorted(syllabus.ESTIMATED_SHARE.items(), key=lambda kv: -kv[1]):
                mark = "📘 your book" if subject in book_subjects else "⚠️ no book"
                lines.append(f"• {subject} ~{round(share * 100)}% — {mark}")
            lines.append("")
        ranked = [u for u in score_units(units, exam) if u.pyqs][:TOP_N]
        if ranked:
            lines.append("From the PYQs you've saved:")
            lines.extend(_unit_line(i, u) for i, u in enumerate(ranked, 1))
            lines.append("")
        if chapters:
            gaps = syllabus.uncovered_subjects()
            if gaps:
                lines.append("⚠️ No book yet for: " + ", ".join(f"{g} {_share(g)}" for g in gaps))
            saved_books = {c["book_key"] for c in chapters}
            missing = [syllabus.book_title(k) for k in syllabus.BOOKS if k not in saved_books]
            if missing:
                lines.append("Send the index pages of " + " and ".join(missing) + " to add them.")
            lines.append("")
        lines.append("Photograph previous papers and this switches to real PYQ counts.")
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
