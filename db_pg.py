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

import json
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

EMBED_DIMENSIONS = 1024

SCHEMA_LIBRARY = f"""
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE questions ADD COLUMN IF NOT EXISTS batch_id TEXT NOT NULL DEFAULT '';
ALTER TABLE questions ADD COLUMN IF NOT EXISTS chapter_id BIGINT;

CREATE TABLE IF NOT EXISTS books (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    key         TEXT        NOT NULL,
    title       TEXT        NOT NULL,
    subject     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS chapters (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT  NOT NULL,
    book_id     BIGINT  NOT NULL,
    number      INTEGER,
    title       TEXT    NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER,
    topics      TEXT    NOT NULL DEFAULT '[]',
    batch_id    TEXT    NOT NULL DEFAULT '',
    UNIQUE (book_id, title)
);

CREATE TABLE IF NOT EXISTS pages (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT      NOT NULL,
    book_id        BIGINT,
    chapter_id     BIGINT,
    page_no        INTEGER,
    kind           TEXT        NOT NULL,
    subject        TEXT        NOT NULL,
    topic          TEXT        NOT NULL,
    text           TEXT        NOT NULL,
    file_id        TEXT        NOT NULL,
    file_unique_id TEXT        NOT NULL,
    batch_id       TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, file_unique_id)
);

CREATE TABLE IF NOT EXISTS pyqs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    book_id     BIGINT,
    chapter_id  BIGINT,
    page_id     BIGINT,
    exam        TEXT        NOT NULL DEFAULT '',
    year        INTEGER,
    number      INTEGER,
    question    TEXT        NOT NULL,
    options     TEXT        NOT NULL DEFAULT '[]',
    answer      TEXT        NOT NULL DEFAULT '',
    subject     TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    batch_id    TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, question)
);
CREATE INDEX IF NOT EXISTS idx_pyqs_number ON pyqs(user_id, number);

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT  NOT NULL,
    source      TEXT    NOT NULL,
    source_id   BIGINT  NOT NULL,
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   vector({EMBED_DIMENSIONS}) NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id, source);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
"""



async def connect(dsn: str) -> None:
    """Create the pool and ensure the schema exists. Safe to call repeatedly."""
    global _pool
    if _pool is not None:
        return
    # statement_cache_size=0 is required when the DSN points at a connection
    # pooler (Neon's "-pooler" host, PgBouncer in transaction mode): server-side
    # prepared statements do not survive being handed a different backend
    # between queries, and asyncpg would fail with "prepared statement does not
    # exist". It costs a little planning time and is harmless on a direct DSN.
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        command_timeout=30,
        statement_cache_size=0,
        max_inactive_connection_lifetime=180,  # free Postgres suspends idle links
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(SCHEMA_LIBRARY)
    logger.info("Postgres pool ready")


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db_pg.connect() was never awaited")
    return _pool


def now_ist() -> datetime:
    return datetime.now(IST)


# ------------------------------------------------------------------ questions


async def add_questions(
    user_id: int,
    items: Iterable[dict],
    source: str,
    batch_id: str = "",
    chapter_id: int | None = None,
) -> int:
    """Insert generated questions, skipping exact duplicates. Returns count added."""
    pool = _require_pool()
    added = 0
    async with pool.acquire() as conn:
        for item in items:
            options = item["options"]
            result = await conn.execute(
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at, batch_id, chapter_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
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
                batch_id,
                chapter_id,
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


# -------------------------------------------------------------------- library


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def _json_list(value) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _rowcount(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def upsert_book(user_id: int, key: str, title: str, subject: str) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO books (user_id, key, title, subject) VALUES ($1,$2,$3,$4)
               ON CONFLICT (user_id, key) DO NOTHING""",
            user_id, key, title, subject,
        )
        return await conn.fetchval(
            "SELECT id FROM books WHERE user_id = $1 AND key = $2", user_id, key
        )


async def upsert_chapter(user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    pool = _require_pool()
    topics = json.dumps(chapter.get("topics") or [])
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM chapters WHERE book_id = $1 AND title = $2", book_id, chapter["title"]
        )
        if existing:
            await conn.execute(
                """UPDATE chapters SET
                     number = COALESCE($1, number),
                     page_start = COALESCE($2, page_start),
                     page_end = COALESCE($3, page_end),
                     topics = CASE WHEN $4 = '[]' THEN topics ELSE $4 END
                   WHERE id = $5""",
                chapter.get("number"), chapter.get("page_start"), chapter.get("page_end"),
                topics, existing,
            )
            return existing, False
        new_id = await conn.fetchval(
            """INSERT INTO chapters (user_id, book_id, number, title, page_start, page_end, topics, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            user_id, book_id, chapter.get("number"), chapter["title"], chapter.get("page_start"),
            chapter.get("page_end"), topics, batch_id,
        )
        return new_id, True


async def find_chapter_for_page(user_id: int, book_id: int, page_no: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM chapters
               WHERE user_id = $1 AND book_id = $2 AND page_start <= $3
                 AND (page_end IS NULL OR page_end >= $3)
               ORDER BY page_start DESC LIMIT 1""",
            user_id, book_id, page_no,
        )
    if row is None:
        return None
    data = dict(row)
    data["topics"] = _json_list(data.get("topics"))
    return data


async def add_page(user_id: int, page: dict, batch_id: str) -> int | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO pages (user_id, book_id, chapter_id, page_no, kind, subject, topic,
                                  text, file_id, file_unique_id, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (user_id, file_unique_id) DO NOTHING
               RETURNING id""",
            user_id, page.get("book_id"), page.get("chapter_id"), page.get("page_no"),
            page["kind"], page["subject"], page["topic"], page["text"], page["file_id"],
            page["file_unique_id"], batch_id,
        )


async def get_page(user_id: int, page_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pages WHERE user_id = $1 AND id = $2", user_id, page_id
        )
    return dict(row) if row else None


async def add_pyq(user_id: int, item: dict, batch_id: str) -> int | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO pyqs (user_id, book_id, chapter_id, page_id, exam, year, number,
                                 question, options, answer, subject, topic, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (user_id, question) DO NOTHING
               RETURNING id""",
            user_id, item.get("book_id"), item.get("chapter_id"), item.get("page_id"),
            item.get("exam", ""), item.get("year"), item.get("number"), item["question"],
            json.dumps(item.get("options") or []), item.get("answer", ""), item["subject"],
            item["topic"], batch_id,
        )


async def find_pyq(
    user_id: int, number: int, page_id: int | None = None, chapter_id: int | None = None
) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = None
        for column, value in (("page_id", page_id), ("chapter_id", chapter_id)):
            if value is None:
                continue
            row = await conn.fetchrow(
                f"""SELECT * FROM pyqs WHERE user_id = $1 AND number = $2 AND {column} = $3
                    ORDER BY id DESC LIMIT 1""",
                user_id, number, value,
            )
            if row:
                break
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM pyqs WHERE user_id = $1 AND number = $2 ORDER BY id DESC LIMIT 1",
                user_id, number,
            )
    if row is None:
        return None
    data = dict(row)
    data["options"] = _json_list(data.get("options"))
    return data


async def pyq_counts(user_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                      COALESCE(c.title, p.topic) AS title, p.exam AS exam, COUNT(*) AS n
               FROM pyqs p LEFT JOIN chapters c ON c.id = p.chapter_id
               WHERE p.user_id = $1
               GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic), p.exam""",
            user_id,
        )
    return [dict(row) for row in rows]


async def topic_progress(user_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        pages = await conn.fetch(
            """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                      COALESCE(c.title, p.topic) AS title, COUNT(*) AS pages
               FROM pages p LEFT JOIN chapters c ON c.id = p.chapter_id
               WHERE p.user_id = $1 AND p.kind = 'content'
               GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic)""",
            user_id,
        )
        attempts = await conn.fetch(
            """SELECT q.subject AS subject, q.chapter_id AS chapter_id,
                      COALESCE(c.title, q.topic) AS title, COUNT(*) AS attempts,
                      COALESCE(SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS correct
               FROM attempts a
               JOIN questions q ON q.id = a.question_id
               LEFT JOIN chapters c ON c.id = q.chapter_id
               WHERE a.user_id = $1
               GROUP BY q.subject, q.chapter_id, COALESCE(c.title, q.topic)""",
            user_id,
        )
    return {"pages": [dict(r) for r in pages], "attempts": [dict(r) for r in attempts]}


async def add_chunks(
    user_id: int,
    source: str,
    source_id: int,
    rows: list[tuple[str, list[float]]],
    subject: str,
    topic: str,
    batch_id: str,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO chunks (user_id, source, source_id, subject, topic, text, embedding, batch_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8)""",
            [(user_id, source, source_id, subject, topic, text, _vector_literal(vector), batch_id)
             for text, vector in rows],
        )


async def search_chunks(
    user_id: int, vector: list[float], k: int, sources: list[str] | None = None
) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, source, source_id, subject, topic, text,
                      1 - (embedding <=> $2::vector) AS score
               FROM chunks
               WHERE user_id = $1 AND ($3::text[] IS NULL OR source = ANY($3::text[]))
               ORDER BY embedding <=> $2::vector
               LIMIT $4""",
            user_id, _vector_literal(vector), sources, k,
        )
    return [dict(row) for row in rows]


async def undo_batch(user_id: int, batch_id: str) -> int:
    if not batch_id:
        return 0
    pool = _require_pool()
    removed = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in ("chunks", "pyqs", "pages", "chapters", "questions"):
                status = await conn.execute(
                    f"DELETE FROM {table} WHERE user_id = $1 AND batch_id = $2", user_id, batch_id
                )
                removed += _rowcount(status)
    return removed


async def aclose() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
