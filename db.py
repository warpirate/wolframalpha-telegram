"""SQLite storage for the exam-prep bot: question bank, attempts, spaced repetition.

All public functions are async wrappers that run the blocking sqlite3 work in a
worker thread, so handlers never block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
from array import array
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import DATABASE_URL

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

CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    book_id     INTEGER NOT NULL,
    number      INTEGER,
    title       TEXT    NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER,
    topics      TEXT    NOT NULL DEFAULT '[]',
    batch_id    TEXT    NOT NULL DEFAULT '',
    UNIQUE (book_id, title)
);

CREATE TABLE IF NOT EXISTS pages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    book_id        INTEGER,
    chapter_id     INTEGER,
    page_no        INTEGER,
    kind           TEXT    NOT NULL,
    subject        TEXT    NOT NULL,
    topic          TEXT    NOT NULL,
    text           TEXT    NOT NULL,
    file_id        TEXT    NOT NULL,
    file_unique_id TEXT    NOT NULL,
    batch_id       TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    UNIQUE (user_id, file_unique_id)
);

CREATE TABLE IF NOT EXISTS pyqs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    book_id     INTEGER,
    chapter_id  INTEGER,
    page_id     INTEGER,
    exam        TEXT    NOT NULL DEFAULT '',
    year        INTEGER,
    number      INTEGER,
    question    TEXT    NOT NULL,
    options     TEXT    NOT NULL DEFAULT '[]',
    answer      TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, question)
);
CREATE INDEX IF NOT EXISTS idx_pyqs_number ON pyqs(user_id, number);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    source_id   INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    batch_id    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id, source);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    username      TEXT    NOT NULL DEFAULT '',
    registered_at TEXT    NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        for column in ("batch_id TEXT NOT NULL DEFAULT ''", "chapter_id INTEGER"):
            try:
                _conn.execute(f"ALTER TABLE questions ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already added on an earlier start
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


def _add_questions(
    conn, user_id: int, items: Iterable[dict], source: str, batch_id: str, chapter_id: int | None
) -> int:
    added = 0
    for item in items:
        options = item["options"]
        try:
            conn.execute(
                """INSERT INTO questions
                   (user_id, subject, topic, question, opt_a, opt_b, opt_c, opt_d,
                    correct, explanation, source, created_at, batch_id, chapter_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    batch_id,
                    chapter_id,
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue  # duplicate question text for this user - skip silently
    return added


async def add_questions(
    user_id: int, items: list[dict], source: str, batch_id: str = "", chapter_id: int | None = None
) -> int:
    """Insert generated questions, skipping exact duplicates. Returns count added."""
    return await call(_add_questions, user_id, items, source, batch_id, chapter_id)


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


# ---------------------------------------------------------------------- users


def _add_user(conn, user_id: int, name: str, username: str) -> None:
    conn.execute(
        """INSERT INTO users (user_id, name, username, registered_at) VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username""",
        (user_id, name, username, now_iso()),
    )


async def add_user(user_id: int, name: str, username: str = "") -> None:
    await call(_add_user, user_id, name, username)


def _get_user(conn, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


async def get_user(user_id: int) -> dict | None:
    row = await call(_get_user, user_id)
    return dict(row) if row else None


def _list_users(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY registered_at").fetchall()


async def list_users() -> list[dict]:
    return [dict(row) for row in await call(_list_users)]


# -------------------------------------------------------------------- library


def _json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _to_blob(vector: Iterable[float]) -> bytes:
    return array("f", vector).tobytes()


def _from_blob(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _upsert_book(conn, user_id: int, key: str, title: str, subject: str) -> int:
    conn.execute(
        """INSERT INTO books (user_id, key, title, subject, created_at) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, key) DO NOTHING""",
        (user_id, key, title, subject, now_iso()),
    )
    return conn.execute(
        "SELECT id FROM books WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()["id"]


async def upsert_book(user_id: int, key: str, title: str, subject: str) -> int:
    """Create the book once per user; returns its id either way."""
    return await call(_upsert_book, user_id, key, title, subject)


def _upsert_chapter(conn, user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    topics = json.dumps(chapter.get("topics") or [])
    row = conn.execute(
        "SELECT id FROM chapters WHERE book_id = ? AND title = ?", (book_id, chapter["title"])
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE chapters SET
                 number = COALESCE(?, number),
                 page_start = COALESCE(?, page_start),
                 page_end = COALESCE(?, page_end),
                 topics = CASE WHEN ? = '[]' THEN topics ELSE ? END
               WHERE id = ?""",
            (chapter.get("number"), chapter.get("page_start"), chapter.get("page_end"),
             topics, topics, row["id"]),
        )
        return row["id"], False
    cursor = conn.execute(
        """INSERT INTO chapters (user_id, book_id, number, title, page_start, page_end, topics, batch_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, book_id, chapter.get("number"), chapter["title"], chapter.get("page_start"),
         chapter.get("page_end"), topics, batch_id),
    )
    return cursor.lastrowid, True


async def upsert_chapter(user_id: int, book_id: int, chapter: dict, batch_id: str) -> tuple[int, bool]:
    """Insert or fill in a chapter. Returns (id, created)."""
    return await call(_upsert_chapter, user_id, book_id, chapter, batch_id)


def _chapter_dict(row) -> dict:
    data = dict(row)
    data["topics"] = _json_list(data.get("topics"))
    return data


def _list_chapters(conn, user_id: int):
    return conn.execute(
        """SELECT c.id, c.number, c.title, c.page_start, c.page_end, c.topics,
                  b.key AS book_key, b.title AS book_title, b.subject AS subject
           FROM chapters c JOIN books b ON b.id = c.book_id
           WHERE c.user_id = ?
           ORDER BY b.id, c.number, c.id""",
        (user_id,),
    ).fetchall()


async def list_chapters(user_id: int) -> list[dict]:
    """Every saved chapter with its book, in book order."""
    return [_chapter_dict(row) for row in await call(_list_chapters, user_id)]


def _find_chapter_for_page(conn, user_id: int, book_id: int, page_no: int):
    return conn.execute(
        """SELECT * FROM chapters
           WHERE user_id = ? AND book_id = ? AND page_start <= ?
             AND (page_end IS NULL OR page_end >= ?)
           ORDER BY page_start DESC LIMIT 1""",
        (user_id, book_id, page_no, page_no),
    ).fetchone()


async def find_chapter_for_page(user_id: int, book_id: int, page_no: int) -> dict | None:
    row = await call(_find_chapter_for_page, user_id, book_id, page_no)
    return _chapter_dict(row) if row else None


def _add_page(conn, user_id: int, page: dict, batch_id: str) -> int | None:
    try:
        cursor = conn.execute(
            """INSERT INTO pages (user_id, book_id, chapter_id, page_no, kind, subject, topic,
                                  text, file_id, file_unique_id, batch_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, page.get("book_id"), page.get("chapter_id"), page.get("page_no"),
             page["kind"], page["subject"], page["topic"], page["text"], page["file_id"],
             page["file_unique_id"], batch_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None  # this exact photo was stored before
    return cursor.lastrowid


async def add_page(user_id: int, page: dict, batch_id: str) -> int | None:
    """Store a photo's reading. Returns None when the same photo was already stored."""
    return await call(_add_page, user_id, page, batch_id)


def _get_page(conn, user_id: int, page_id: int):
    return conn.execute(
        "SELECT * FROM pages WHERE user_id = ? AND id = ?", (user_id, page_id)
    ).fetchone()


async def get_page(user_id: int, page_id: int) -> dict | None:
    row = await call(_get_page, user_id, page_id)
    return dict(row) if row else None


def _add_pyq(conn, user_id: int, item: dict, batch_id: str) -> int | None:
    try:
        cursor = conn.execute(
            """INSERT INTO pyqs (user_id, book_id, chapter_id, page_id, exam, year, number,
                                 question, options, answer, subject, topic, batch_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, item.get("book_id"), item.get("chapter_id"), item.get("page_id"),
             item.get("exam", ""), item.get("year"), item.get("number"), item["question"],
             json.dumps(item.get("options") or []), item.get("answer", ""), item["subject"],
             item["topic"], batch_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None  # same question text already stored
    return cursor.lastrowid


async def add_pyq(user_id: int, item: dict, batch_id: str) -> int | None:
    """Store one previous-year question. Returns None for an exact duplicate."""
    return await call(_add_pyq, user_id, item, batch_id)


def _pyq_dict(row) -> dict:
    data = dict(row)
    data["options"] = _json_list(data.get("options"))
    return data


def _find_pyq(conn, user_id: int, number: int, page_id: int | None, chapter_id: int | None):
    for column, value in (("page_id", page_id), ("chapter_id", chapter_id)):
        if value is None:
            continue
        row = conn.execute(
            f"""SELECT * FROM pyqs WHERE user_id = ? AND number = ? AND {column} = ?
                ORDER BY id DESC LIMIT 1""",
            (user_id, number, value),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM pyqs WHERE user_id = ? AND number = ? ORDER BY id DESC LIMIT 1",
        (user_id, number),
    ).fetchone()


async def find_pyq(
    user_id: int, number: int, page_id: int | None = None, chapter_id: int | None = None
) -> dict | None:
    """Question `number` from the given page, else that chapter, else the newest one."""
    row = await call(_find_pyq, user_id, number, page_id, chapter_id)
    return _pyq_dict(row) if row else None


def _pyq_counts(conn, user_id: int):
    return conn.execute(
        """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                  COALESCE(c.title, p.topic) AS title, p.exam AS exam, COUNT(*) AS n
           FROM pyqs p LEFT JOIN chapters c ON c.id = p.chapter_id
           WHERE p.user_id = ?
           GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic), p.exam""",
        (user_id,),
    ).fetchall()


async def pyq_counts(user_id: int) -> list[dict]:
    return [dict(row) for row in await call(_pyq_counts, user_id)]


def _topic_progress(conn, user_id: int) -> dict:
    pages = conn.execute(
        """SELECT p.subject AS subject, p.chapter_id AS chapter_id,
                  COALESCE(c.title, p.topic) AS title, COUNT(*) AS pages
           FROM pages p LEFT JOIN chapters c ON c.id = p.chapter_id
           WHERE p.user_id = ? AND p.kind = 'content'
           GROUP BY p.subject, p.chapter_id, COALESCE(c.title, p.topic)""",
        (user_id,),
    ).fetchall()
    attempts = conn.execute(
        """SELECT q.subject AS subject, q.chapter_id AS chapter_id,
                  COALESCE(c.title, q.topic) AS title,
                  COUNT(*) AS attempts, COALESCE(SUM(a.is_correct), 0) AS correct
           FROM attempts a
           JOIN questions q ON q.id = a.question_id
           LEFT JOIN chapters c ON c.id = q.chapter_id
           WHERE a.user_id = ?
           GROUP BY q.subject, q.chapter_id, COALESCE(c.title, q.topic)""",
        (user_id,),
    ).fetchall()
    return {"pages": [dict(r) for r in pages], "attempts": [dict(r) for r in attempts]}


async def topic_progress(user_id: int) -> dict:
    """Content pages read and quiz results, per chapter (or topic when no chapter)."""
    return await call(_topic_progress, user_id)


def _add_chunks(conn, user_id, source, source_id, rows, subject, topic, batch_id) -> None:
    conn.executemany(
        """INSERT INTO chunks (user_id, source, source_id, subject, topic, text, embedding, batch_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(user_id, source, source_id, subject, topic, text, _to_blob(vector), batch_id)
         for text, vector in rows],
    )


async def add_chunks(
    user_id: int,
    source: str,
    source_id: int,
    rows: list[tuple[str, list[float]]],
    subject: str,
    topic: str,
    batch_id: str,
) -> None:
    """Store (text, embedding) pairs for one page, PYQ or chapter."""
    await call(_add_chunks, user_id, source, source_id, rows, subject, topic, batch_id)


def _search_chunks(conn, user_id, vector, k, sources) -> list[dict]:
    sql = "SELECT id, source, source_id, subject, topic, text, embedding FROM chunks WHERE user_id = ?"
    params: list[Any] = [user_id]
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    scored = []
    for row in conn.execute(sql, params):
        hit = {key: row[key] for key in ("id", "source", "source_id", "subject", "topic", "text")}
        hit["score"] = _cosine(vector, _from_blob(row["embedding"]))
        scored.append(hit)
    scored.sort(key=lambda hit: hit["score"], reverse=True)
    return scored[:k]


async def search_chunks(
    user_id: int, vector: list[float], k: int, sources: list[str] | None = None
) -> list[dict]:
    """Nearest chunks by cosine similarity (brute force - fine for a few thousand rows)."""
    return await call(_search_chunks, user_id, vector, k, sources)


def _undo_batch(conn, user_id: int, batch_id: str) -> int:
    if not batch_id:
        return 0
    removed = 0
    for table in ("chunks", "pyqs", "pages", "chapters", "questions"):
        removed += conn.execute(
            f"DELETE FROM {table} WHERE user_id = ? AND batch_id = ?", (user_id, batch_id)
        ).rowcount
    return removed


async def undo_batch(user_id: int, batch_id: str) -> int:
    """Delete every row one save created. Returns the number of rows removed."""
    return await call(_undo_batch, user_id, batch_id)


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# --------------------------------------------------------------- backend switch
#
# With DATABASE_URL set the public functions above are replaced by the Postgres
# implementations. Everything that imports this module keeps calling the same
# names with the same arguments and gets the same shapes back.

USING_POSTGRES = bool(DATABASE_URL)

if USING_POSTGRES:
    import db_pg

    add_questions = db_pg.add_questions          # type: ignore[assignment]
    pick_quiz = db_pg.pick_quiz                  # type: ignore[assignment]
    get_question = db_pg.get_question            # type: ignore[assignment]
    count_questions = db_pg.count_questions      # type: ignore[assignment]
    record_answer = db_pg.record_answer          # type: ignore[assignment]
    stats = db_pg.stats                          # type: ignore[assignment]
    weak_topics = db_pg.weak_topics              # type: ignore[assignment]
    set_daily = db_pg.set_daily                  # type: ignore[assignment]
    all_daily = db_pg.all_daily                  # type: ignore[assignment]
    add_user = db_pg.add_user                    # type: ignore[assignment]
    get_user = db_pg.get_user                    # type: ignore[assignment]
    list_users = db_pg.list_users                # type: ignore[assignment]
    upsert_book = db_pg.upsert_book              # type: ignore[assignment]
    upsert_chapter = db_pg.upsert_chapter        # type: ignore[assignment]
    list_chapters = db_pg.list_chapters                # type: ignore[assignment]
    find_chapter_for_page = db_pg.find_chapter_for_page# type: ignore[assignment]
    add_page = db_pg.add_page                    # type: ignore[assignment]
    get_page = db_pg.get_page                    # type: ignore[assignment]
    add_pyq = db_pg.add_pyq                      # type: ignore[assignment]
    find_pyq = db_pg.find_pyq                    # type: ignore[assignment]
    pyq_counts = db_pg.pyq_counts                # type: ignore[assignment]
    topic_progress = db_pg.topic_progress        # type: ignore[assignment]
    add_chunks = db_pg.add_chunks                # type: ignore[assignment]
    search_chunks = db_pg.search_chunks          # type: ignore[assignment]
    undo_batch = db_pg.undo_batch                # type: ignore[assignment]


async def init() -> None:
    """Open the backend. Awaited once at startup."""
    if USING_POSTGRES:
        await db_pg.connect(DATABASE_URL)
    else:
        await asyncio.to_thread(_connect)


async def shutdown() -> None:
    """Close the backend. Awaited once at exit."""
    if USING_POSTGRES:
        await db_pg.aclose()
    else:
        close()


def backend_name() -> str:
    return "postgres" if USING_POSTGRES else f"sqlite ({DB_PATH})"
