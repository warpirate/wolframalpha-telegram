"""Postgres backend for the question bank.

Mirrors the public interface of the SQLite backend in db.py exactly, so the rest
of the bot neither knows nor cares which one is in use. Selected by setting
DATABASE_URL; used on hosts that give you no persistent disk.

SQLite differences handled here:
  * AUTOINCREMENT        -> GENERATED ... AS IDENTITY
  * ? placeholders       -> $1, $2, ...
  * RANDOM()             -> random()
  * datetime text        -> timestamptz, so comparisons are real time comparisons
  * COLLATE NOCASE       -> lower(...) comparison
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import asyncpg

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Hours until a question comes back, indexed by consecutive correct answers.
REVIEW_INTERVALS_HOURS = (1.0, 12.0, 24.0, 72.0, 168.0, 336.0, 720.0)

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    subject     TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    question    TEXT        NOT NULL,
    opt_a       TEXT        NOT NULL,
    opt_b       TEXT        NOT NULL,
    opt_c       TEXT        NOT NULL,
    opt_d       TEXT        NOT NULL,
    correct     INTEGER     NOT NULL,
    explanation TEXT        NOT NULL DEFAULT '',
    source      TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_unique
    ON questions(user_id, md5(question));
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(user_id, subject);

CREATE TABLE IF NOT EXISTS attempts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    question_id BIGINT      NOT NULL,
    chosen      INTEGER     NOT NULL,
    is_correct  BOOLEAN     NOT NULL,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id, answered_at);

CREATE TABLE IF NOT EXISTS review (
    user_id     BIGINT      NOT NULL,
    question_id BIGINT      NOT NULL,
    due_at      TIMESTAMPTZ NOT NULL,
    streak      INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_review_due ON review(user_id, due_at);

CREATE TABLE IF NOT EXISTS prefs (
    user_id     BIGINT PRIMARY KEY,
    chat_id     BIGINT  NOT NULL,
    daily_hour  INTEGER,
    daily_count INTEGER NOT NULL DEFAULT 10
);
"""


async def connect(dsn: str) -> None:
    """Create the pool and ensure the schema exists. Safe to call repeatedly."""
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=30)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("Postgres pool ready")


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db_pg.connect() was never awaited")
    return _pool


def now_ist() -> datetime:
    return datetime.now(IST)


# ------------------------------------------------------------------ questions


async def add_questions(user_id: int, items: Iterable[dict], source: str) -> int:
    """Insert generated questions, skipping exact duplicates. Returns count added."""
    pool = _require_pool()
    added = 0
    async with pool.acquire() as conn:
        for item in items:
            options = item["options"]
            result = await conn.execute(
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                   ON CONFLICT DO NOTHING""",
                user_id,
                item.get("subject", "General"),
                item.get("topic", "General"),
                item["question"],
                options[0], options[1], options[2], options[3],
                int(item["correct_index"]),
                item.get("explanation", ""),
                source,
                now_ist(),
            )
            if result.endswith("1"):
                added += 1
    return added


async def pick_quiz(user_id: int, limit: int, subject: str | None = None) -> list[dict]:
    """Due reviews first, then unseen questions, then anything at random."""
    pool = _require_pool()
    now = now_ist()
    async with pool.acquire() as conn:
        if subject:
            rows = await conn.fetch(
                """SELECT q.*,
                          CASE WHEN r.due_at IS NULL THEN 1 ELSE 0 END AS is_new,
                          r.due_at
                   FROM questions q
                   LEFT JOIN review r
                          ON r.question_id = q.id AND r.user_id = q.user_id
                   WHERE q.user_id = $1
                     AND (r.due_at IS NULL OR r.due_at <= $2)
                     AND lower(q.subject) = lower($3)
                   ORDER BY is_new ASC, r.due_at ASC NULLS LAST, random()
                   LIMIT $4""",
                user_id, now, subject, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT q.*,
                          CASE WHEN r.due_at IS NULL THEN 1 ELSE 0 END AS is_new,
                          r.due_at
                   FROM questions q
                   LEFT JOIN review r
                          ON r.question_id = q.id AND r.user_id = q.user_id
                   WHERE q.user_id = $1
                     AND (r.due_at IS NULL OR r.due_at <= $2)
                   ORDER BY is_new ASC, r.due_at ASC NULLS LAST, random()
                   LIMIT $3""",
                user_id, now, limit,
            )

        picked = [dict(row) for row in rows]
        if len(picked) < limit:  # nothing due - top up with anything they have
            have = {row["id"] for row in picked}
            if subject:
                extra = await conn.fetch(
                    """SELECT * FROM questions
                       WHERE user_id = $1 AND lower(subject) = lower($2)
                       ORDER BY random() LIMIT $3""",
                    user_id, subject, limit * 3,
                )
            else:
                extra = await conn.fetch(
                    """SELECT * FROM questions WHERE user_id = $1
                       ORDER BY random() LIMIT $2""",
                    user_id, limit * 3,
                )
            for row in extra:
                if row["id"] not in have:
                    picked.append(dict(row))
                    have.add(row["id"])
                if len(picked) >= limit:
                    break
    return picked[:limit]


async def get_question(question_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM questions WHERE id = $1", question_id)
    return dict(row) if row else None


async def count_questions(user_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT subject, COUNT(*) AS n FROM questions
               WHERE user_id = $1 GROUP BY subject ORDER BY n DESC""",
            user_id,
        )
    return [dict(row) for row in rows]


# ------------------------------------------------------- attempts and reviews


async def record_answer(user_id: int, question_id: int, chosen: int, is_correct: bool) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO attempts (user_id, question_id, chosen, is_correct, answered_at)
                   VALUES ($1,$2,$3,$4,$5)""",
                user_id, question_id, chosen, bool(is_correct), now_ist(),
            )
            current = await conn.fetchval(
                "SELECT streak FROM review WHERE user_id = $1 AND question_id = $2",
                user_id, question_id,
            )
            streak = (current or 0) + 1 if is_correct else 0
            hours = REVIEW_INTERVALS_HOURS[min(streak, len(REVIEW_INTERVALS_HOURS) - 1)]
            due = now_ist() + timedelta(hours=hours)
            await conn.execute(
                """INSERT INTO review (user_id, question_id, due_at, streak)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (user_id, question_id)
                   DO UPDATE SET due_at = EXCLUDED.due_at, streak = EXCLUDED.streak""",
                user_id, question_id, due, streak,
            )


async def stats(user_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        overall = await conn.fetchrow(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN is_correct THEN 1 ELSE 0 END), 0) AS ok
               FROM attempts WHERE user_id = $1""",
            user_id,
        )
        by_subject = await conn.fetch(
            """SELECT q.subject AS name, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS ok
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.user_id = $1
               GROUP BY q.subject ORDER BY n DESC""",
            user_id,
        )
        start_of_day = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
        today = await conn.fetchrow(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN is_correct THEN 1 ELSE 0 END), 0) AS ok
               FROM attempts WHERE user_id = $1 AND answered_at >= $2""",
            user_id, start_of_day,
        )
        due = await conn.fetchval(
            "SELECT COUNT(*) FROM review WHERE user_id = $1 AND due_at <= $2",
            user_id, now_ist(),
        )
    return {
        "total": overall["n"],
        "correct": overall["ok"],
        "today": today["n"],
        "today_correct": today["ok"],
        "due": due or 0,
        "subjects": [dict(row) for row in by_subject],
    }


async def weak_topics(user_id: int, limit: int = 8) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT q.topic AS name, q.subject AS subject, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS ok
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.user_id = $1
               GROUP BY q.topic, q.subject
               HAVING COUNT(*) >= 2
               ORDER BY (SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END)::float / COUNT(*)) ASC,
                        COUNT(*) DESC
               LIMIT $2""",
            user_id, limit,
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------- preferences


async def set_daily(user_id: int, chat_id: int, hour: int | None, count: int = 10) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO prefs (user_id, chat_id, daily_hour, daily_count)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (user_id) DO UPDATE SET
                 chat_id = EXCLUDED.chat_id,
                 daily_hour = EXCLUDED.daily_hour,
                 daily_count = EXCLUDED.daily_count""",
            user_id, chat_id, hour, count,
        )


async def all_daily() -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM prefs WHERE daily_hour IS NOT NULL")
    return [dict(row) for row in rows]


async def aclose() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
