"""Quiz session flow: serving questions, grading answers, rendering results."""

from __future__ import annotations

import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db
from formatter import escape_markdown_v2

SESSION_KEY = "quiz_session"
LETTERS = ("A", "B", "C", "D")
CALLBACK_PREFIX = "q"


def build_keyboard(question_id: int, token: int) -> InlineKeyboardMarkup:
    """Four answer buttons. `token` ties a press back to the session that sent it."""
    row = [
        InlineKeyboardButton(
            letter, callback_data=f"{CALLBACK_PREFIX}:{question_id}:{index}:{token}"
        )
        for index, letter in enumerate(LETTERS)
    ]
    return InlineKeyboardMarkup([row[:2], row[2:]])


def parse_callback(data: str) -> tuple[int, int, int] | None:
    """Returns (question_id, chosen_index, token) or None if the data is foreign."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None


def start_session(chat_data: dict, questions: list[dict], subject: str | None) -> dict:
    session = {
        "questions": questions,
        "index": 0,
        "correct": 0,
        "answered": [],
        "subject": subject,
        "token": int(time.time()),
        "started_at": time.time(),
    }
    chat_data[SESSION_KEY] = session
    return session


def get_session(chat_data: dict) -> dict | None:
    session = chat_data.get(SESSION_KEY)
    return session if isinstance(session, dict) else None


def end_session(chat_data: dict) -> None:
    chat_data.pop(SESSION_KEY, None)


def current_question(session: dict) -> dict | None:
    index = session["index"]
    questions = session["questions"]
    return questions[index] if index < len(questions) else None


def render_question(question: dict, number: int, total: int) -> str:
    """MarkdownV2 body for one question."""
    head = escape_markdown_v2(f"Q{number}/{total}")
    subject = escape_markdown_v2(f"{question['subject']} · {question['topic']}")
    body = escape_markdown_v2(question["question"])
    lines = [f"*{head}*  _{subject}_", "", body, ""]
    for letter, key in zip(LETTERS, ("opt_a", "opt_b", "opt_c", "opt_d")):
        lines.append(f"*{letter}\\.*  {escape_markdown_v2(question[key])}")
    return "\n".join(lines)


def render_feedback(question: dict, chosen: int, is_correct: bool) -> str:
    """MarkdownV2 body shown after an answer, with the question kept visible."""
    options = [question["opt_a"], question["opt_b"], question["opt_c"], question["opt_d"]]
    correct = question["correct"]

    lines = [escape_markdown_v2(question["question"]), ""]
    for index, option in enumerate(options):
        mark = "✅" if index == correct else ("❌" if index == chosen else "  ")
        text = escape_markdown_v2(f"{LETTERS[index]}. {option}")
        if index == correct:
            lines.append(f"{mark} *{text}*")
        else:
            lines.append(f"{mark} {text}")

    verdict = "✅ *Correct*" if is_correct else "❌ *Wrong*"
    lines.extend(["", verdict])
    if question.get("explanation"):
        lines.append("")
        lines.append("💡 " + escape_markdown_v2(question["explanation"]))
    return "\n".join(lines)


def render_summary(session: dict) -> str:
    total = len(session["answered"])
    correct = session["correct"]
    if total == 0:
        return escape_markdown_v2("Quiz ended. No questions answered.")

    pct = round(100 * correct / total)
    elapsed = max(1, int(time.time() - session["started_at"]))
    per_q = elapsed / total

    if pct >= 85:
        verdict = "Strong. Keep this pace."
    elif pct >= 70:
        verdict = "Decent. Review the misses."
    elif pct >= 50:
        verdict = "Shaky. Re-read this topic before the next drill."
    else:
        verdict = "Weak area. Go back to the book on this one."

    wrong_topics: dict[str, int] = {}
    for item in session["answered"]:
        if not item["is_correct"]:
            wrong_topics[item["topic"]] = wrong_topics.get(item["topic"], 0) + 1

    lines = [
        "🏁 *Quiz complete*",
        "",
        escape_markdown_v2(f"Score: {correct}/{total}  ({pct}%)"),
        escape_markdown_v2(f"Time: {_mmss(elapsed)}  ({per_q:.0f}s per question)"),
        escape_markdown_v2(verdict),
    ]
    if wrong_topics:
        ranked = sorted(wrong_topics.items(), key=lambda kv: -kv[1])
        lines.extend(["", "*Missed in:*"])
        lines.extend(
            escape_markdown_v2(f"• {topic} ({n})") for topic, n in ranked[:5]
        )
    lines.extend(["", escape_markdown_v2("Wrong answers come back sooner. Say “quiz me” for more.")])
    return "\n".join(lines)


def _mmss(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60:02d}s"


def render_stats(data: dict) -> str:
    if data["total"] == 0:
        return escape_markdown_v2(
            "No attempts yet. Send photos of your notes pages, then say “quiz me”."
        )
    pct = round(100 * data["correct"] / data["total"])
    lines = [
        "📊 *Your stats*",
        "",
        escape_markdown_v2(f"Overall: {data['correct']}/{data['total']}  ({pct}%)"),
    ]
    if data["today"]:
        tpct = round(100 * data["today_correct"] / data["today"])
        lines.append(
            escape_markdown_v2(f"Today: {data['today_correct']}/{data['today']}  ({tpct}%)")
        )
    lines.append(escape_markdown_v2(f"Due for review now: {data['due']}"))

    if data["subjects"]:
        lines.extend(["", "*By subject*"])
        for row in data["subjects"]:
            spct = round(100 * row["ok"] / row["n"]) if row["n"] else 0
            bar = _bar(spct)
            lines.append(
                escape_markdown_v2(f"{row['name']:<14} {row['ok']:>3}/{row['n']:<3} {spct:>3}% ")
                + escape_markdown_v2(bar)
            )
    return "\n".join(lines)


def _bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def render_weak(rows: list[dict]) -> str:
    if not rows:
        return escape_markdown_v2(
            "Not enough attempts yet to find weak topics. Answer at least 2 questions per topic."
        )
    lines = ["🎯 *Weakest topics*", "", escape_markdown_v2("Lowest accuracy first:"), ""]
    for row in rows:
        pct = round(100 * row["ok"] / row["n"])
        lines.append(
            escape_markdown_v2(f"{pct:>3}%  {row['name']} ({row['subject']}) — {row['ok']}/{row['n']}")
        )
    lines.extend(["", escape_markdown_v2("Drill one by saying “quiz me on <subject>”.")])
    return "\n".join(lines)
