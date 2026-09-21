"""SQLite storage for the exam-prep bot: question bank, attempts, spaced repetition.

All public functions are async wrappers that run the blocking sqlite3 work in a
worker thread, so handlers never block the event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

DB_PATH = "exam.db"

# Hours until a question comes back, indexed by how many times in a row it has
# been answered correctly. Wrong answers reset to index 0.
REVIEW_INTERVALS_HOURS = (1.0, 12.0, 24.0, 72.0, 168.0, 336.0, 720.0)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    question    TEXT    NOT NULL,
    opt_a       TEXT    NOT NULL,
    opt_b       TEXT    NOT NULL,
    opt_c       TEXT    NOT NULL,
    opt_d       TEXT    NOT NULL,
    correct     INTEGER NOT NULL,
    explanation TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_unique
    ON questions(user_id, question);
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(user_id, subject);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    chosen      INTEGER NOT NULL,
    is_correct  INTEGER NOT NULL,
    answered_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, answered_at);

CREATE TABLE IF NOT EXISTS review (
    user_id     INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    due_at      TEXT    NOT NULL,
    streak      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_review_due ON review(user_id, due_at);

CREATE TABLE IF NOT EXISTS prefs (
    user_id     INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    daily_hour  INTEGER,
    daily_count INTEGER NOT NULL DEFAULT 10
);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _run(fn, *args):
    with _lock:
        conn = _connect()
        result = fn(conn, *args)
        conn.commit()
        return result


async def call(fn, *args):
    return await asyncio.to_thread(_run, fn, *args)


def now_iso() -> str:
    return datetime.now(IST).isoformat()


# ------------------------------------------------------------------ questions


def _add_questions(conn, user_id: int, items: Iterable[dict], source: str) -> int:
    added = 0
    for item in items:
        options = item["options"]
        try:
            conn.execute(
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue  # duplicate question text for this user - skip silently
    return added


async def add_questions(user_id: int, items: list[dict], source: str) -> int:
    """Insert generated questions, skipping exact duplicates. Returns count added."""
    return await call(_add_questions, user_id, items, source)


def _pick_quiz(conn, user_id: int, limit: int, subject: str | None) -> list[sqlite3.Row]:
    now = now_iso()
    where_subject = "AND q.subject = ? COLLATE NOCASE" if subject else ""
    params: list[Any] = [user_id, now]
    if subject:
        params.append(subject)
    params.append(limit)

    # Questions that are due for review come first (oldest due first), then
    # questions never attempted, then anything else at random.
    rows = conn.execute(
        f"""
        SELECT q.*,
               CASE WHEN r.due_at IS NULL THEN 1 ELSE 0 END AS is_new,
               r.due_at AS due_at
        FROM questions q
        LEFT JOIN review r ON r.question_id = q.id AND r.user_id = q.user_id
        WHERE q.user_id = ?
          AND (r.due_at IS NULL OR r.due_at <= ?)
          {where_subject}
        ORDER BY is_new ASC, r.due_at ASC, RANDOM()
        LIMIT ?
        """,
        params,
    ).fetchall()

    if len(rows) < limit:  # nothing due - top up with anything the user has
        have = {row["id"] for row in rows}
        params2: list[Any] = [user_id]
        if subject:
            params2.append(subject)
        params2.append(limit * 3)
        extra = conn.execute(
            f"""SELECT q.*, 1 AS is_new, NULL AS due_at FROM questions q
                WHERE q.user_id = ? {where_subject.replace('q.subject', 'q.subject')}
                ORDER BY RANDOM() LIMIT ?""",
            params2,
        ).fetchall()
        for row in extra:
            if row["id"] not in have:
                rows.append(row)
                have.add(row["id"])
            if len(rows) >= limit:
                break
    return rows[:limit]


async def pick_quiz(user_id: int, limit: int, subject: str | None = None) -> list[dict]:
    rows = await call(_pick_quiz, user_id, limit, subject)
    return [dict(row) for row in rows]


def _get_question(conn, question_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


async def get_question(question_id: int) -> dict | None:
    row = await call(_get_question, question_id)
    return dict(row) if row else None


def _count_questions(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT subject, COUNT(*) AS n FROM questions
           WHERE user_id = ? GROUP BY subject ORDER BY n DESC""",
        (user_id,),
    ).fetchall()


async def count_questions(user_id: int) -> list[dict]:
    return [dict(row) for row in await call(_count_questions, user_id)]


# ------------------------------------------------------- attempts and reviews


def _record(conn, user_id: int, question_id: int, chosen: int, is_correct: bool) -> None:
    conn.execute(
        """INSERT INTO attempts (user_id, question_id, chosen, is_correct, answered_at)
           VALUES (?,?,?,?,?)""",
        (user_id, question_id, chosen, int(is_correct), now_iso()),
    )

    row = conn.execute(
        "SELECT streak FROM review WHERE user_id = ? AND question_id = ?",
        (user_id, question_id),
    ).fetchone()
    streak = (row["streak"] if row else 0) + 1 if is_correct else 0
    hours = REVIEW_INTERVALS_HOURS[min(streak, len(REVIEW_INTERVALS_HOURS) - 1)]
    due = (datetime.now(IST) + timedelta(hours=hours)).isoformat()

    conn.execute(
        """INSERT INTO review (user_id, question_id, due_at, streak) VALUES (?,?,?,?)
           ON CONFLICT(user_id, question_id)
           DO UPDATE SET due_at = excluded.due_at, streak = excluded.streak""",
        (user_id, question_id, due, streak),
    )


async def record_answer(user_id: int, question_id: int, chosen: int, is_correct: bool) -> None:
    await call(_record, user_id, question_id, chosen, is_correct)


def _stats(conn, user_id: int) -> dict:
    overall = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(is_correct), 0) AS ok
           FROM attempts WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    by_subject = conn.execute(
        """SELECT q.subject AS name,
                  COUNT(*) AS n,
                  COALESCE(SUM(a.is_correct), 0) AS ok
           FROM attempts a JOIN questions q ON q.id = a.question_id
           WHERE a.user_id = ?
           GROUP BY q.subject ORDER BY n DESC""",
        (user_id,),
    ).fetchall()
    today = datetime.now(IST).date().isoformat()
    today_row = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(is_correct), 0) AS ok
           FROM attempts WHERE user_id = ? AND answered_at LIKE ?""",
        (user_id, f"{today}%"),
    ).fetchone()
    due_now = conn.execute(
        "SELECT COUNT(*) AS n FROM review WHERE user_id = ? AND due_at <= ?",
        (user_id, now_iso()),
    ).fetchone()
    return {
        "total": overall["n"],
        "correct": overall["ok"],
        "today": today_row["n"],
        "today_correct": today_row["ok"],
        "due": due_now["n"],
        "subjects": [dict(row) for row in by_subject],
    }


async def stats(user_id: int) -> dict:
    return await call(_stats, user_id)


def _weak(conn, user_id: int, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT q.topic AS name, q.subject AS subject,
                  COUNT(*) AS n, COALESCE(SUM(a.is_correct), 0) AS ok
           FROM attempts a JOIN questions q ON q.id = a.question_id
           WHERE a.user_id = ?
           GROUP BY q.topic, q.subject
           HAVING n >= 2
           ORDER BY (CAST(ok AS REAL) / n) ASC, n DESC
           LIMIT ?""",
        (user_id, limit),
    ).fetchall()


async def weak_topics(user_id: int, limit: int = 8) -> list[dict]:
    return [dict(row) for row in await call(_weak, user_id, limit)]


# ---------------------------------------------------------------- preferences


def _set_daily(conn, user_id: int, chat_id: int, hour: int | None, count: int) -> None:
    conn.execute(
        """INSERT INTO prefs (user_id, chat_id, daily_hour, daily_count) VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             chat_id = excluded.chat_id,
             daily_hour = excluded.daily_hour,
             daily_count = excluded.daily_count""",
        (user_id, chat_id, hour, count),
    )


async def set_daily(user_id: int, chat_id: int, hour: int | None, count: int = 10) -> None:
    await call(_set_daily, user_id, chat_id, hour, count)


def _all_daily(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM prefs WHERE daily_hour IS NOT NULL"
    ).fetchall()


async def all_daily() -> list[dict]:
    return [dict(row) for row in await call(_all_daily)]


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
