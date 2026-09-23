"""Telegram entry point: TSLPRB (PC/SI) exam-prep bot with no commands.

Every photo is read, filed (book, chapter, PYQs, notes) and indexed for search;
every text message is routed by intent (study plan, question lookup, search,
quiz, stats, daily quiz, or a grounded answer).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, AsyncIterator

from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import library
import quiz
import ranking
import retrieval
import router
from ai_client import AIError, NebiusClient
from config import (
    HISTORY_TURNS,
    LOG_LEVEL,
    NEBIUS_API_KEY,
    NEBIUS_BASE_URL,
    NEBIUS_MODEL,
    NEBIUS_VISION_MODEL,
    PORT,
    TELEGRAM_BOT_TOKEN,
)
from formatter import escape_markdown_v2, prepare_for_telegram, unescape_markdown_v2
from prompts import IMAGE_DEFAULT_PROMPT

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("exam-bot")

AI_CLIENT_KEY = "ai_client"
HISTORY_KEY = "history"

MAX_PROMPT_CHARS = 4000
MAX_IMAGE_DIMENSION = 1600
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MIN_JPEG_QUALITY = 55
DEFAULT_QUIZ_LENGTH = 10
# Album photos arrive as separate updates a few hundred ms apart; wait this long
# after the last one before treating the album as complete.
ALBUM_SETTLE_SECONDS = 1.5

# (chat_id, media_group_id) -> {"messages": [...], "last": monotonic time}
_pending_albums: dict[tuple[int, str], dict[str, Any]] = {}

GENERIC_ERROR = "Something went wrong. Please try again."
EMPTY_TEXT_REPLY = "Send me a question, or a photo of your book pages."
DOWNLOAD_ERROR = "I couldn't download that image from Telegram. Please try sending it again."
IMAGE_DECODE_ERROR = "I couldn't read that image. Try a clearer photo (JPEG or PNG)."

WELCOME = (
    "🎯 *TSLPRB Prep Bot*\n\n"
    "No commands — just send photos and ask\\.\n\n"
    "📷 *Photos* of your index pages, PYQ pages, notes or a question — "
    "I file every one by book, chapter and topic\\.\n\n"
    "*Then ask things like*\n"
    "• what should I study?\n"
    "• help with Q14\n"
    "• PYQs on Ashoka\n"
    "• quiz me on polity\n"
    "• how am I doing?\n"
    "• daily quiz at 6am"
)


# --------------------------------------------------------------------- helpers


@contextlib.asynccontextmanager
async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> AsyncIterator[None]:
    """Keep the 'typing…' indicator alive for the duration of the block."""

    async def _loop() -> None:
        while True:
            try:
                await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramError:
                return
            await asyncio.sleep(4)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _client(context: ContextTypes.DEFAULT_TYPE) -> NebiusClient:
    return context.application.bot_data[AI_CLIENT_KEY]


def _history(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    if context.chat_data is None:
        return []
    history = context.chat_data.get(HISTORY_KEY)
    if not isinstance(history, list):
        history = []
        context.chat_data[HISTORY_KEY] = history
    return history


def _remember(context: ContextTypes.DEFAULT_TYPE, question: str, answer: str) -> None:
    if HISTORY_TURNS <= 0 or context.chat_data is None:
        return
    history = _history(context)
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    del history[: max(0, len(history) - HISTORY_TURNS * 2)]


async def _reply_md(message, text: str, **kwargs) -> None:
    """Send MarkdownV2, falling back to plain text if Telegram rejects it."""
    try:
        await message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True, **kwargs
        )
    except BadRequest as exc:
        logger.warning("MarkdownV2 rejected (%s); sending plain", exc)
        await message.reply_text(
            unescape_markdown_v2(text), disable_web_page_preview=True, **kwargs
        )


async def _send_answer_to(message, answer: str) -> None:
    chunks = prepare_for_telegram(answer)
    if not chunks:
        await message.reply_text(GENERIC_ERROR)
        return
    for chunk in chunks:
        await _reply_md(message, chunk)


def _compress_image(data: bytes) -> tuple[bytes, str]:
    """Normalise an image to a reasonably sized JPEG. Blocking - run in a thread."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        needs_resize = max(img.size) > MAX_IMAGE_DIMENSION
        if not needs_resize and len(data) <= MAX_IMAGE_BYTES and img.format == "JPEG":
            return data, "image/jpeg"
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if needs_resize:
            img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
        quality = 85
        while True:
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= MAX_IMAGE_BYTES or quality <= MIN_JPEG_QUALITY:
                return encoded, "image/jpeg"
            quality -= 10


async def _download_photo(message, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    """Download the largest photo size and normalise it, or reply with the error."""
    photo = message.photo[-1]
    try:
        telegram_file = await context.bot.get_file(photo.file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
    except TelegramError as exc:
        logger.error("Failed to download photo: %s", exc)
        await message.reply_text(DOWNLOAD_ERROR)
        return None
    try:
        image_bytes, _ = await asyncio.to_thread(_compress_image, raw)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.error("Failed to decode photo: %s", exc)
        await message.reply_text(IMAGE_DECODE_ERROR)
        return None
    return image_bytes


# ----------------------------------------------------------------- health probe


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers the platform's health checks and keep-alive pings."""

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"prep-bot alive")

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        """Silence per-request logs; a pinger every few minutes is just noise."""


def _start_health_server(port: int) -> None:
    """Bind a port in a daemon thread.

    Hosts like Render only keep a free web service alive if something is
    listening, and an external uptime pinger needs a URL to hit. The bot talks
    to Telegram outbound, so this server serves no other purpose.
    """
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    logger.info("Health endpoint listening on port %d", port)


# ---------------------------------------------------------------- basic commands


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await _reply_md(update.effective_message, WELCOME)


# ---------------------------------------------------------------- photo handling


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every photo is stored and indexed; questions on it get answered."""
    message = update.effective_message
    if message is None or not message.photo:
        return
    if message.media_group_id:
        _queue_album_photo(message, context)
        return
    await _process_photos(context, [message])


def _queue_album_photo(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hold an album photo until the rest of the album has arrived.

    Telegram delivers an album as separate messages sharing a media_group_id,
    with the caption on only one of them. Handling each on its own gives one
    reply per photo, most of them without the user's question.
    """
    key = (message.chat_id, message.media_group_id)
    album = _pending_albums.get(key)
    if album is None:
        album = _pending_albums[key] = {"messages": [], "last": 0.0}
        context.application.create_task(_flush_album(key, context))
    album["messages"].append(message)
    album["last"] = time.monotonic()


async def _flush_album(key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wait for the album to go quiet, then process every photo in it together."""
    while True:
        delay = _pending_albums[key]["last"] + ALBUM_SETTLE_SECONDS - time.monotonic()
        if delay <= 0:
            break
        await asyncio.sleep(delay)
    album = _pending_albums.pop(key)
    await _process_photos(context, sorted(album["messages"], key=lambda m: m.message_id))


async def _save_photo_message(context, message, user_id: int, caption: str, batch_id: str):
    image = await _download_photo(message, context)
    if image is None:
        return None
    photo = message.photo[-1]
    try:
        saved = await library.save_photo(
            _client(context), user_id, image, photo.file_id, photo.file_unique_id,
            caption=caption, batch_id=batch_id,
        )
    except AIError as exc:
        logger.error("Saving a photo failed: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return None
    return saved, image


async def _process_photos(context: ContextTypes.DEFAULT_TYPE, messages: list) -> None:
    """Save every photo (one batch), report what was filed, then answer if asked."""
    captioned = [m for m in messages if (m.caption or "").strip()]
    anchor = captioned[0] if captioned else messages[0]
    caption = (anchor.caption or "").strip()[:MAX_PROMPT_CHARS]
    user = anchor.from_user
    if user is None or context.chat_data is None:
        return

    batch_id = library.new_batch_id()
    logger.info("Saving %d photo(s) from chat %s", len(messages), anchor.chat_id)
    async with typing(context, anchor.chat_id):
        results = await asyncio.gather(
            *(_save_photo_message(context, m, user.id, caption, batch_id) for m in messages)
        )
    done = [r for r in results if r is not None]
    if not done:
        return
    for saved, _ in done:
        library.focus_from_saved(context.chat_data, saved)
    await _reply_saved(anchor, done, batch_id)

    if caption:
        focus = library.get_focus(context.chat_data)
        intent = router.quick_intent(caption) or await router.classify_text(
            _client(context), caption, library.focus_summary(focus)
        )
        if intent.name != "solve":
            await _TEXT_ROUTES[intent.name](anchor, context, user.id, caption, intent, focus)
            return
        to_answer = done
    else:
        to_answer = [(s, img) for s, img in done if s.needs_answer]
    if not to_answer:
        return

    question = caption or IMAGE_DEFAULT_PROMPT
    page_text = "\n\n".join(s.read.text for s, _ in to_answer if s.read.text)[:3000]
    if page_text:
        question += f"\n\nText read from the photo(s):\n{page_text}"
    await _grounded_answer(
        anchor, context, user.id, question,
        remember_as=f"[photo] {caption or 'solve this'}",
        images=[img for _, img in to_answer],
    )


def _undo_button(batch_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("↩️ Undo", callback_data=f"u:{batch_id}")


def _kind_keyboard(batch_id: str, page_id: int) -> InlineKeyboardMarkup:
    kinds = ("index", "pyq", "content", "question", "cover", "other")
    buttons = [
        InlineKeyboardButton(
            library.KIND_LABELS[k].capitalize(), callback_data=f"k:{batch_id}:{page_id}:{k}"
        )
        for k in kinds
    ]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:], [_undo_button(batch_id)]])


async def _reply_saved(anchor, done: list, batch_id: str) -> None:
    lines = [saved.summary for saved, _ in done if saved.summary]
    if not lines:
        return
    fresh = [saved for saved, _ in done if saved.page_id is not None]
    if not fresh:
        await anchor.reply_text("\n".join(lines))
        return
    single = fresh[0] if len(done) == 1 else None
    if single is not None and single.read.confidence < router.LOW_CONFIDENCE:
        lines.append(f"\nNot sure this is a {library.KIND_LABELS[single.kind]} page — what is it?")
        markup = _kind_keyboard(batch_id, single.page_id)
    elif single is not None:
        markup = InlineKeyboardMarkup([[
            _undo_button(batch_id),
            InlineKeyboardButton("🔁 Wrong type", callback_data=f"w:{batch_id}:{single.page_id}"),
        ]])
    else:
        markup = InlineKeyboardMarkup([[_undo_button(batch_id)]])
    await anchor.reply_text("\n".join(lines), reply_markup=markup)


async def _grounded_answer(
    message, context, user_id: int, question: str, remember_as: str, images: list | None = None
) -> None:
    """Answer with the user's own saved material retrieved into the prompt."""
    client = _client(context)
    focus = library.get_focus(context.chat_data)
    try:
        async with typing(context, message.chat_id):
            prompt, hits = await library.grounded_prompt(client, user_id, question, focus)
            if images:
                answer = await client.ask_image(prompt, images, "image/jpeg", history=_history(context))
            else:
                answer = await client.ask_text(prompt, history=_history(context))
    except AIError as exc:
        logger.error("AI error on answer: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return
    _remember(context, remember_as, answer)
    await _send_answer_to(message, answer + library.render_similar(hits, exclude=question))


async def on_library_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo a save, show the kind picker, or re-read a photo as a chosen kind."""
    query, user = update.callback_query, update.effective_user
    if query is None or user is None:
        return
    parts = (query.data or "").split(":")

    if parts[0] == "u" and len(parts) == 2:
        removed = await db.undo_batch(user.id, parts[1])
        await query.answer("Undone." if removed else "Nothing left to undo.")
        text = (query.message.text if query.message else "") or ""
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(f"{text}\n\n↩️ Undone.", reply_markup=None)
        return

    if parts[0] == "w" and len(parts) == 3 and parts[2].isdigit():
        await query.answer()
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(_kind_keyboard(parts[1], int(parts[2])))
        return

    if parts[0] == "k" and len(parts) == 4 and parts[2].isdigit() and parts[3] in router.PAGE_KINDS:
        await _reread_as(query, context, user.id, parts[1], int(parts[2]), parts[3])
        return

    await query.answer()


async def _reread_as(query, context, user_id: int, batch_id: str, page_id: int, kind: str) -> None:
    page = await db.get_page(user_id, page_id)
    if page is None:
        await query.answer("That photo was already removed.", show_alert=True)
        return
    await query.answer(f"Re-reading as {library.KIND_LABELS[kind]}…")
    try:
        telegram_file = await context.bot.get_file(page["file_id"])
        raw = bytes(await telegram_file.download_as_bytearray())
        image, _ = await asyncio.to_thread(_compress_image, raw)
    except (TelegramError, UnidentifiedImageError, OSError, ValueError) as exc:
        logger.error("Re-download failed: %s", exc)
        if query.message is not None:
            await query.message.reply_text(DOWNLOAD_ERROR)
        return

    await db.undo_batch(user_id, batch_id)
    new_batch = library.new_batch_id()
    try:
        saved = await library.save_photo(
            _client(context), user_id, image, page["file_id"], page["file_unique_id"],
            forced_kind=kind, batch_id=new_batch,
        )
    except AIError as exc:
        logger.error("Re-read failed: %s", exc.detail)
        if query.message is not None:
            await query.message.reply_text(exc.user_message)
        return
    library.focus_from_saved(context.chat_data, saved)
    text = saved.summary or f"Saved as a {library.KIND_LABELS[kind]}."
    with contextlib.suppress(TelegramError):
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup([[_undo_button(new_batch)]])
        )
    if saved.needs_answer and query.message is not None:
        await _grounded_answer(
            query.message, context, user_id,
            f"{IMAGE_DEFAULT_PROMPT}\n\nText read from the photo:\n{saved.read.text[:3000]}",
            remember_as="[photo] solve this", images=[image],
        )


# ----------------------------------------------------------------- text routing


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or context.chat_data is None:
        return
    user_text = (message.text or "").strip()
    if not user_text:
        await message.reply_text(EMPTY_TEXT_REPLY)
        return
    user_text = user_text[:MAX_PROMPT_CHARS]

    focus = library.get_focus(context.chat_data)
    intent = router.quick_intent(user_text)
    if intent is None:
        async with typing(context, message.chat_id):
            intent = await router.classify_text(_client(context), user_text, library.focus_summary(focus))
    logger.info("Text from chat %s routed to %s", message.chat_id, intent.name)
    await _TEXT_ROUTES.get(intent.name, _route_solve)(message, context, user.id, user_text, intent, focus)


async def _route_solve(message, context, user_id, text, intent, focus) -> None:
    await _grounded_answer(message, context, user_id, text, remember_as=text)


async def _route_study_plan(message, context, user_id, text, intent, focus) -> None:
    units = ranking.build_units(await db.pyq_counts(user_id), await db.topic_progress(user_id))
    await message.reply_text(ranking.render_plan(units))


async def _route_lookup(message, context, user_id, text, intent, focus) -> None:
    if intent.number is None:
        page = await db.get_page(user_id, focus["page_id"]) if focus.get("page_id") else None
        if page is None:
            await _route_solve(message, context, user_id, text, intent, focus)
            return
        question = f"{text}\n\nThe page the user means:\n{page['text']}"
    else:
        pyq = await library.lookup_pyq(user_id, intent.number, focus)
        if pyq is None:
            await message.reply_text(
                f"I don't have Q{intent.number} saved yet. Send a photo of that page and ask again."
            )
            return
        library.set_focus(context.chat_data, chapter_id=pyq.get("chapter_id"),
                          page_id=pyq.get("page_id"), topic=pyq.get("topic"))
        question = f"{library.render_pyq(pyq)}\n\n{text}"
    await _grounded_answer(message, context, user_id, question, remember_as=text)


async def _route_search(message, context, user_id, text, intent, focus) -> None:
    client = _client(context)
    async with typing(context, message.chat_id):
        hits = await retrieval.search(client, user_id, intent.query or text, k=8)
    if not hits:
        await message.reply_text("Nothing about that in what you've saved yet.")
        return
    prompt = (
        "Answer using ONLY the material below from the user's saved pages and PYQs. "
        "List any matching PYQs with their exam and year. Cite as [n].\n\n"
        f"{retrieval.render_context(hits)}\n\nThe user's request:\n{text}"
    )
    try:
        async with typing(context, message.chat_id):
            answer = await client.ask_text(prompt, history=_history(context))
    except AIError as exc:
        logger.error("AI error on search: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return
    _remember(context, text, answer)
    await _send_answer_to(message, answer)


async def _route_quiz(message, context, user_id, text, intent, focus) -> None:
    questions = await db.pick_quiz(user_id, DEFAULT_QUIZ_LENGTH, intent.subject)
    if not questions:
        where = f" in {intent.subject}" if intent.subject else ""
        await message.reply_text(
            f"No practice questions{where} yet. Send photos of your notes pages and I'll make some."
        )
        return
    session = quiz.start_session(context.chat_data, questions, intent.subject)
    await _send_current_question(message, session)


async def _route_stats(message, context, user_id, text, intent, focus) -> None:
    await _reply_md(message, quiz.render_stats(await db.stats(user_id)))
    weak = await db.weak_topics(user_id)
    if weak:
        await _reply_md(message, quiz.render_weak(weak))


async def _route_daily(message, context, user_id, text, intent, focus) -> None:
    if intent.hour is None:
        await message.reply_text("What time? For example: “daily quiz at 6am”.")
        return
    await db.set_daily(user_id, message.chat_id, intent.hour, DEFAULT_QUIZ_LENGTH)
    _schedule_daily(context.application, user_id, message.chat_id, intent.hour, DEFAULT_QUIZ_LENGTH)
    await message.reply_text(
        f"⏰ Daily quiz set: {DEFAULT_QUIZ_LENGTH} questions at {intent.hour:02d}:00 IST. "
        "Say “stop daily quiz” to cancel."
    )


async def _route_daily_off(message, context, user_id, text, intent, focus) -> None:
    await db.set_daily(user_id, message.chat_id, None)
    for job in context.application.job_queue.get_jobs_by_name(f"daily-{user_id}"):
        job.schedule_removal()
    await message.reply_text("Daily quiz cancelled.")


_TEXT_ROUTES = {
    "solve": _route_solve,
    "study_plan": _route_study_plan,
    "lookup": _route_lookup,
    "search": _route_search,
    "quiz": _route_quiz,
    "stats": _route_stats,
    "daily": _route_daily,
    "daily_off": _route_daily_off,
}


# ------------------------------------------------------------------ quiz flow


async def _send_current_question(message, session: dict) -> None:
    question = quiz.current_question(session)
    if question is None:
        return
    body = quiz.render_question(question, session["index"] + 1, len(session["questions"]))
    await _reply_md(
        message,
        body,
        reply_markup=quiz.build_keyboard(question["id"], session["token"]),
    )


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an A/B/C/D button press."""
    query = update.callback_query
    if query is None or context.chat_data is None:
        return

    parsed = quiz.parse_callback(query.data)
    if parsed is None:
        await query.answer()
        return
    question_id, chosen, token = parsed

    session = quiz.get_session(context.chat_data)
    if session is None or session["token"] != token:
        await query.answer("That quiz has ended. Say “quiz me” to start a new one.", show_alert=True)
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return

    question = quiz.current_question(session)
    if question is None or question["id"] != question_id:
        await query.answer("Already answered.")
        return

    is_correct = chosen == question["correct"]
    await query.answer("✅ Correct" if is_correct else "❌ Wrong")

    user = update.effective_user
    if user is not None:
        await db.record_answer(user.id, question_id, chosen, is_correct)

    session["index"] += 1
    session["correct"] += int(is_correct)
    session["answered"].append({
        "question_id": question_id,
        "is_correct": is_correct,
        "topic": question["topic"],
        "subject": question["subject"],
    })

    feedback = quiz.render_feedback(question, chosen, is_correct)
    try:
        await query.edit_message_text(
            feedback, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=None
        )
    except BadRequest as exc:
        logger.warning("Feedback edit rejected (%s); sending plain", exc)
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(unescape_markdown_v2(feedback), reply_markup=None)

    message = query.message
    if message is None:
        return

    if quiz.current_question(session) is not None:
        await _send_current_question(message, session)
    else:
        summary = quiz.render_summary(session)
        quiz.end_session(context.chat_data)
        await _reply_md(message, summary)


# ------------------------------------------------------------------ daily quiz


async def _daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire the scheduled quiz for one user."""
    data: dict[str, Any] = context.job.data or {}
    user_id, chat_id = data.get("user_id"), data.get("chat_id")
    count = data.get("count", DEFAULT_QUIZ_LENGTH)
    if user_id is None or chat_id is None:
        return

    questions = await db.pick_quiz(user_id, count)
    if not questions:
        return

    chat_data = context.application.chat_data.setdefault(chat_id, {})
    session = quiz.start_session(chat_data, questions, None)
    question = quiz.current_question(session)
    if question is None:
        return

    try:
        await context.bot.send_message(
            chat_id,
            escape_markdown_v2(f"⏰ Daily drill — {len(questions)} questions. Go."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await context.bot.send_message(
            chat_id,
            quiz.render_question(question, 1, len(questions)),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=quiz.build_keyboard(question["id"], session["token"]),
        )
    except TelegramError as exc:
        logger.error("Daily quiz send failed for chat %s: %s", chat_id, exc)


def _schedule_daily(application: Application, user_id: int, chat_id: int, hour: int, count: int) -> None:
    name = f"daily-{user_id}"
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    application.job_queue.run_daily(
        _daily_job,
        time=dt.time(hour=hour, minute=0, tzinfo=db.IST),
        name=name,
        data={"user_id": user_id, "chat_id": chat_id, "count": count},
    )


# ------------------------------------------------------------------- lifecycle


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(GENERIC_ERROR)


async def _post_init(application: Application) -> None:
    application.bot_data[AI_CLIENT_KEY] = NebiusClient(
        api_key=NEBIUS_API_KEY,
        base_url=NEBIUS_BASE_URL,
        model=NEBIUS_MODEL,
        vision_model=NEBIUS_VISION_MODEL,
    )
    logger.info(
        "Nebius client ready (model=%s, vision_model=%s, base_url=%s)",
        NEBIUS_MODEL, NEBIUS_VISION_MODEL, NEBIUS_BASE_URL,
    )

    await db.init()
    logger.info("Storage backend: %s", db.backend_name())

    restored = 0
    for row in await db.all_daily():
        _schedule_daily(
            application, row["user_id"], row["chat_id"], row["daily_hour"], row["daily_count"]
        )
        restored += 1
    logger.info("Restored %d daily quiz schedule(s)", restored)


async def _post_shutdown(application: Application) -> None:
    client: Any = application.bot_data.pop(AI_CLIENT_KEY, None)
    if isinstance(client, NebiusClient):
        await client.aclose()
    await db.shutdown()
    logger.info("Shutdown complete")


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(on_answer, pattern=r"^q:"))
    application.add_handler(CallbackQueryHandler(on_library_button, pattern=r"^[uwk]:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    logger.info("Starting exam-prep bot…")
    if PORT:
        _start_health_server(PORT)
    build_application().run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
