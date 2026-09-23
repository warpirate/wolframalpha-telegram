"""Store every photo: work out what it is, file it in the right tables, index it for search."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

import db
import mcq
import retrieval
import router
import syllabus
from ai_client import AIError, NebiusClient
from router import PageRead

logger = logging.getLogger(__name__)

FOCUS_KEY = "focus"
FOCUS_SECONDS = 60 * 60
QUESTIONS_PER_PAGE = 8
ANSWER_KINDS = ("question", "other")
KIND_LABELS = {
    "cover": "cover", "index": "index", "pyq": "PYQ", "content": "notes",
    "question": "question", "other": "other",
}


@dataclass
class Saved:
    kind: str
    page_id: int | None
    chapter_id: int | None
    topic: str
    summary: str  # one line for the user; empty when the answer itself is the reply
    read: PageRead
    needs_answer: bool
    duplicate: bool = False


def new_batch_id() -> str:
    return secrets.token_hex(4)


# ------------------------------------------------------------------ focus


def get_focus(chat_data: dict | None) -> dict:
    if chat_data is None:
        return {}
    focus = chat_data.get(FOCUS_KEY)
    if not isinstance(focus, dict) or focus.get("until", 0) < time.time():
        chat_data.pop(FOCUS_KEY, None)
        return {}
    return focus


def set_focus(chat_data: dict | None, **fields) -> None:
    if chat_data is None:
        return
    focus = dict(get_focus(chat_data))
    focus.update({k: v for k, v in fields.items() if v not in (None, "")})
    focus["until"] = time.time() + FOCUS_SECONDS
    chat_data[FOCUS_KEY] = focus


def focus_from_saved(chat_data: dict | None, saved: Saved) -> None:
    if saved.page_id is None:
        return
    set_focus(
        chat_data,
        page_id=saved.page_id,
        chapter_id=saved.chapter_id,
        book=saved.read.book,
        topic=saved.topic,
        kind=saved.kind,
    )


def focus_summary(focus: dict) -> str:
    if not focus:
        return ""
    parts = [f"last photo: {KIND_LABELS.get(focus.get('kind', ''), 'page')} page"]
    if focus.get("topic"):
        parts.append(f"topic {focus['topic']}")
    if focus.get("book"):
        parts.append(syllabus.book_title(focus["book"]))
    return ", ".join(parts)


# ------------------------------------------------------------------ saving


async def _book_id(user_id: int, read: PageRead) -> int:
    """The page's book, or a per-subject catch-all so index/PYQ pages always have one."""
    if read.book:
        return await db.upsert_book(
            user_id, read.book, syllabus.book_title(read.book), syllabus.book_subject(read.book)
        )
    subject = read.subject
    return await db.upsert_book(user_id, f"other-{subject.lower()}", f"{subject} (other book)", subject)


async def _safe_index(client, user_id, source, source_id, texts, subject, topic, batch_id) -> bool:
    try:
        await retrieval.index_texts(client, user_id, source, source_id, texts, subject, topic, batch_id)
        return True
    except AIError as exc:
        logger.error("Indexing %s %s failed: %s", source, source_id, exc.detail)
        return False


async def save_photo(
    client: NebiusClient,
    user_id: int,
    image_bytes: bytes,
    file_id: str,
    file_unique_id: str,
    caption: str = "",
    forced_kind: str | None = None,
    batch_id: str = "",
) -> Saved:
    """Read one photo, store it and everything on it, and describe what was stored."""
    batch_id = batch_id or new_batch_id()
    read = await router.read_page(client, image_bytes, caption, forced_kind)
    wants_answer = read.kind in ANSWER_KINDS or bool(caption.strip())

    subject = read.subject
    if subject == "General" and read.book:
        subject = syllabus.book_subject(read.book)
        read.subject = subject
    book_id = await _book_id(user_id, read) if read.book or read.kind in ("index", "pyq") else None
    chapter = (
        await db.find_chapter_for_page(user_id, book_id, read.page_no)
        if book_id and read.page_no
        else None
    )
    chapter_id = chapter["id"] if chapter else None
    topic = (chapter["title"] if chapter else "") or read.topic

    page_id = await db.add_page(user_id, {
        "book_id": book_id, "chapter_id": chapter_id, "page_no": read.page_no,
        "kind": read.kind, "subject": subject, "topic": topic, "text": read.text,
        "file_id": file_id, "file_unique_id": file_unique_id,
    }, batch_id)
    if page_id is None:
        return Saved(read.kind, None, chapter_id, topic, "Already saved this photo earlier.",
                     read, wants_answer, duplicate=True)

    indexed = await _safe_index(client, user_id, "page", page_id,
                                retrieval.chunk_text(read.text), subject, topic, batch_id)

    if read.kind == "cover":
        summary = _cover_summary(read)
    elif read.kind == "index":
        summary = await _save_index(client, user_id, book_id, read, batch_id)
    elif read.kind == "pyq":
        summary, topic = await _save_pyqs(client, user_id, book_id, chapter_id, page_id, read, topic, batch_id)
    elif read.kind == "content":
        summary = await _save_content(client, user_id, chapter_id, read, topic, batch_id)
    else:
        summary = ""
    if summary and not indexed:
        summary += " (Search index missed this one — it's saved, just not searchable.)"
    return Saved(read.kind, page_id, chapter_id, topic, summary, read, wants_answer)


def _cover_summary(read: PageRead) -> str:
    if read.book:
        return f"📘 {syllabus.book_title(read.book)}. Send the index pages next."
    return f"📘 Saved the cover (filed under {read.subject})."


async def _save_index(client, user_id, book_id, read: PageRead, batch_id) -> str:
    title = syllabus.book_title(read.book) if read.book else f"your {read.subject} book"
    if not read.chapters:
        return f"🗂 Saved the index page of {title}, but couldn't read any chapters on it."
    added = 0
    for chapter in read.chapters:
        chapter_id, created = await db.upsert_chapter(user_id, book_id, chapter, batch_id)
        if not created:
            continue
        added += 1
        number = f"Chapter {chapter['number']}: " if chapter.get("number") else ""
        topics = f" Topics: {', '.join(chapter['topics'])}." if chapter.get("topics") else ""
        await _safe_index(client, user_id, "chapter", chapter_id,
                          [f"{title} — {number}{chapter['title']}.{topics}"],
                          read.subject, chapter["title"], batch_id)
    names = ", ".join(c["title"] for c in read.chapters[:3])
    more = "…" if len(read.chapters) > 3 else ""
    updated = len(read.chapters) - added
    tail = f" ({updated} already known, updated)" if updated else ""
    return f"🗂 Saved {len(read.chapters)} chapters of {title}: {names}{more}{tail}"


async def _save_pyqs(client, user_id, book_id, chapter_id, page_id, read: PageRead, topic, batch_id):
    texts = [" ".join([q["question"], *q["options"], q["answer"]]).strip() for q in read.pyqs]
    try:
        vectors = await retrieval.embed(client, texts)
    except AIError as exc:
        logger.error("Embedding PYQs failed: %s", exc.detail)
        vectors = [None] * len(texts)

    saved, duplicates, topics = 0, 0, []
    for q, text, vector in zip(read.pyqs, texts, vectors):
        q_chapter, q_topic = chapter_id, topic
        if vector is not None:
            nearest = await db.search_chunks(user_id, vector, 1, ["pyq"])
            if nearest and nearest[0]["score"] >= retrieval.DUPLICATE_SCORE:
                duplicates += 1
                continue
            if q_chapter is None:
                match = await db.search_chunks(user_id, vector, 1, ["chapter"])
                if match and match[0]["score"] >= retrieval.CHAPTER_MATCH_SCORE:
                    q_chapter, q_topic = match[0]["source_id"], match[0]["topic"]
        q_topic = q_topic or read.topic or read.subject
        pyq_id = await db.add_pyq(user_id, {
            **q, "book_id": book_id, "chapter_id": q_chapter, "page_id": page_id,
            "subject": read.subject, "topic": q_topic,
        }, batch_id)
        if pyq_id is None:
            duplicates += 1
            continue
        saved += 1
        topics.append(q_topic)
        if vector is not None:
            await db.add_chunks(user_id, "pyq", pyq_id, [(text, vector)], read.subject, q_topic, batch_id)

    exam = " ".join(str(x) for x in (read.exam, read.year) if x)
    exam = f" ({exam})" if exam else ""
    top_topics = ", ".join(dict.fromkeys(topics)) or read.subject
    summary = f"📝 Saved {saved} PYQ{'s' if saved != 1 else ''}{exam} — {top_topics}."
    if duplicates:
        summary += f" {duplicates} already saved."
    return summary, (topics[0] if topics else topic)


async def _save_content(client, user_id, chapter_id, read: PageRead, topic, batch_id) -> str:
    label = topic or read.subject
    if not read.text:
        return f"📖 Saved a {label} page, but couldn't read its text."
    try:
        questions, _ = await mcq.generate_from_text(client, read.text, QUESTIONS_PER_PAGE, hint=label)
    except (AIError, mcq.MCQError) as exc:
        logger.error("MCQ generation failed: %s", getattr(exc, "detail", exc))
        questions = []
    added = 0
    if questions:
        added = await db.add_questions(
            user_id, questions, source=label, batch_id=batch_id, chapter_id=chapter_id
        )
    practice = f", {added} practice questions added" if added else ""
    return f"📖 {label} — saved{practice}."


# ------------------------------------------------------------ using the library


async def lookup_pyq(user_id: int, number: int, focus: dict) -> dict | None:
    return await db.find_pyq(user_id, number, focus.get("page_id"), focus.get("chapter_id"))


def render_pyq(pyq: dict) -> str:
    head = " ".join(str(x) for x in (pyq.get("exam"), pyq.get("year")) if x)
    number = f"Q{pyq['number']}" if pyq.get("number") is not None else "PYQ"
    lines = [f"{head} · {number}" if head else number, pyq["question"]]
    for letter, option in zip("abcde", pyq.get("options") or []):
        lines.append(f"({letter}) {option}")
    if pyq.get("answer"):
        lines.append(f"Printed answer: {pyq['answer']}")
    return "\n".join(lines)


async def grounded_prompt(
    client: NebiusClient, user_id: int, question: str, focus: dict
) -> tuple[str, list[dict]]:
    """Prefix the question with the closest material from the user's own saved pages."""
    query = f"{question}\n{focus.get('topic', '')}".strip()
    hits = await retrieval.search(client, user_id, query)
    if not hits:
        return question, []
    prompt = (
        "Material from the user's own books and saved PYQs. Use it when it is relevant, "
        "cite it as [n], and ignore it when it is not:\n\n"
        f"{retrieval.render_context(hits)}\n\nThe user's message:\n{question}"
    )
    return prompt, hits


def render_similar(hits: list[dict], limit: int = 3) -> str:
    pyqs = [h for h in hits if h["source"] == "pyq" and h["score"] >= 0.6][:limit]
    if not pyqs:
        return ""
    lines = ["", "", "Asked before:"]
    for hit in pyqs:
        snippet = hit["text"][:120] + ("…" if len(hit["text"]) > 120 else "")
        lines.append(f"• {snippet}")
    return "\n".join(lines)
