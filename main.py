"""Telegram entry point: TSLPRB (PC/SI) exam-prep bot with a built-in solver.

Two modes share one bot:
  * Solver    - send a question or a photo of one, get a structured answer.
  * Exam prep - photograph book pages to build a personal MCQ bank, then drill
                it with spaced repetition, scoring and a daily quiz.
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
from telegram import Update
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
import mcq
import quiz
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
from exam_prompts import SUBJECTS
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
ADD_MODE_KEY = "add_mode"

MAX_PROMPT_CHARS = 4000
MAX_IMAGE_DIMENSION = 1600
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MIN_JPEG_QUALITY = 55
ADD_MODE_TIMEOUT_SECONDS = 15 * 60
QUESTIONS_PER_PAGE = 8
DEFAULT_QUIZ_LENGTH = 10
MAX_QUIZ_LENGTH = 50
# Album photos arrive as separate updates a few hundred ms apart; wait this long
# after the last one before treating the album as complete.
ALBUM_SETTLE_SECONDS = 1.5
ALBUM_DEFAULT_PROMPT = "Solve or explain what is in these images."

# (chat_id, media_group_id) -> {"messages": [...], "last": monotonic time}
_pending_albums: dict[tuple[int, str], dict[str, Any]] = {}

GENERIC_ERROR = "Something went wrong. Please try again."
EMPTY_TEXT_REPLY = "Send me a question, or a photo of one. /help shows everything."
DOWNLOAD_ERROR = "I couldn't download that image from Telegram. Please try sending it again."
IMAGE_DECODE_ERROR = "I couldn't read that image. Try a clearer photo (JPEG or PNG)."

WELCOME = (
    "🎯 *TSLPRB Prep Bot*\n\n"
    "*Two things I do*\n\n"
    "1\\. *Solve* — send any question, or a photo of one, and I'll work it out\\.\n\n"
    "2\\. *Drill* — photograph a page from your books and I'll turn it into exam MCQs "
    "you can practise, with spaced repetition\\.\n\n"
    "*Start here*\n"
    "• /add — then photograph a page of Laxmikanth, Karim, R\\.S\\. Aggarwal, anything\n"
    "• /quiz — practise what you've added\n"
    "• /stats — see where you stand\n\n"
    "Questions come from *your* page only — I don't add facts from memory\\."
)

HELP_EXTRA = (
    "\n\n*All commands*\n"
    "• `/add [subject]` — next photos become questions\n"
    "• `/done` — stop adding\n"
    "• `/quiz [subject] [count]` — e\\.g\\. `/quiz polity 15`\n"
    "• `/stats` — accuracy overall and per subject\n"
    "• `/weak` — your worst topics\n"
    "• `/daily 6` — quiz every day at 6 AM \\(IST\\)\n"
    "• `/nodaily` — cancel it\n"
    "• `/bank` — how many questions you have\n"
    "• `/reset` — clear solver memory\n\n"
    "*Tips*\n"
    "• Good light and a flat page give better questions\n"
    "• One page at a time beats a whole spread\n"
    "• Wrong answers come back sooner, correct ones drift further out"
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


async def _send_answer(update: Update, answer: str) -> None:
    message = update.effective_message
    if message is None:
        return
    await _send_answer_to(message, answer)


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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await _reply_md(update.effective_message, WELCOME + HELP_EXTRA)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.chat_data is not None:
        context.chat_data.pop(HISTORY_KEY, None)
    if update.effective_message:
        await update.effective_message.reply_text("Solver memory cleared.")


# ------------------------------------------------------------- adding questions


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Put the chat into add mode: the next photos become question sources."""
    message = update.effective_message
    if message is None or context.chat_data is None:
        return
    hint = " ".join(context.args or "").strip()
    context.chat_data[ADD_MODE_KEY] = {"hint": hint, "until": time.time() + ADD_MODE_TIMEOUT_SECONDS}

    subject_line = f"Subject hint: {hint}\n\n" if hint else ""
    await message.reply_text(
        f"📷 Add mode on.\n\n{subject_line}"
        "Photograph a page from your book and send it. I'll read it and write exam MCQs "
        "from what's printed on that page — nothing from my own memory.\n\n"
        "Send as many pages as you like. /done when finished.\n"
        f"(Turns off by itself after {ADD_MODE_TIMEOUT_SECONDS // 60} minutes.)"
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.chat_data is not None:
        context.chat_data.pop(ADD_MODE_KEY, None)
    if update.effective_message:
        await update.effective_message.reply_text("Add mode off. /quiz when you're ready.")


def _add_mode(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    if context.chat_data is None:
        return None
    mode = context.chat_data.get(ADD_MODE_KEY)
    if not isinstance(mode, dict):
        return None
    if mode.get("until", 0) < time.time():
        context.chat_data.pop(ADD_MODE_KEY, None)
        return None
    return mode


async def _handle_page_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: dict) -> None:
    """Turn a photographed book page into stored MCQs."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    hint = (message.caption or "").strip() or mode.get("hint", "")

    async with typing(context, message.chat_id):
        image_bytes = await _download_photo(message, context)
        if image_bytes is None:
            return
        try:
            questions, note = await mcq.generate_from_image(
                _client(context), image_bytes, count=QUESTIONS_PER_PAGE, hint=hint
            )
        except (AIError, mcq.MCQError) as exc:
            detail = getattr(exc, "user_message", str(exc))
            logger.error("MCQ generation failed: %s", getattr(exc, "detail", exc))
            await message.reply_text(f"Couldn't make questions from that page. {detail}")
            return

        if not questions:
            reason = note or "I couldn't find examinable content on that page."
            await message.reply_text(f"No questions added. {reason}\n\nTry a clearer, flatter photo.")
            return

        added = await db.add_questions(user.id, questions, source=hint or "book page")

    skipped = len(questions) - added
    subjects = sorted({q["subject"] for q in questions})
    topics = sorted({q["topic"] for q in questions})

    lines = [f"✅ Added {added} question{'s' if added != 1 else ''}."]
    if skipped:
        lines.append(f"({skipped} were duplicates of ones you already have.)")
    lines.append("")
    lines.append(f"Subject: {', '.join(subjects)}")
    lines.append(f"Topics: {', '.join(topics[:6])}")
    if note:
        lines.append(f"\nNote: {note}")
    lines.append("\nSend the next page, or /quiz to start drilling.")
    await message.reply_text("\n".join(lines))


# --------------------------------------------------------------- solver handlers


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.chat is None:
        return

    user_text = (message.text or "").strip()
    if not user_text:
        await message.reply_text(EMPTY_TEXT_REPLY)
        return
    if len(user_text) > MAX_PROMPT_CHARS:
        user_text = user_text[:MAX_PROMPT_CHARS]
        logger.info("Truncated an over-long prompt from chat %s", message.chat_id)

    logger.info("Text question from chat %s (%d chars)", message.chat_id, len(user_text))
    try:
        async with typing(context, message.chat_id):
            answer = await _client(context).ask_text(user_text, history=_history(context))
    except AIError as exc:
        logger.error("AI error on text request: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return

    _remember(context, user_text, answer)
    await _send_answer(update, answer)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add mode turns pages into questions; otherwise solve what is in the photo."""
    message = update.effective_message
    if message is None or not message.photo:
        return

    mode = _add_mode(context)
    if mode is not None:
        await _handle_page_photo(update, context, mode)
        return

    if message.media_group_id:
        _queue_album_photo(message, context)
        return

    await _solve_photos(context, [message])


def _queue_album_photo(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hold an album photo until the rest of the album has arrived.

    Telegram delivers an album as separate messages sharing a media_group_id,
    with the caption on only one of them. Answering each on its own gives one
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
    """Wait for the album to go quiet, then answer every photo in one go."""
    while True:
        delay = _pending_albums[key]["last"] + ALBUM_SETTLE_SECONDS - time.monotonic()
        if delay <= 0:
            break
        await asyncio.sleep(delay)
    album = _pending_albums.pop(key)
    messages = sorted(album["messages"], key=lambda m: m.message_id)
    await _solve_photos(context, messages)


async def _solve_photos(context: ContextTypes.DEFAULT_TYPE, messages: list) -> None:
    """Answer one photo, or a whole album, with a single model call."""
    captioned = [m for m in messages if (m.caption or "").strip()]
    anchor = captioned[0] if captioned else messages[0]
    caption = (anchor.caption or "").strip()
    count = len(messages)

    prompt = caption or (IMAGE_DEFAULT_PROMPT if count == 1 else ALBUM_DEFAULT_PROMPT)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS]
    if count > 1:
        prompt = (
            f"The user sent these {count} photos together as one message. Read them as one "
            f"set and give one answer.\n\n{prompt}"
        )

    logger.info("Photo question from chat %s (%d image(s))", anchor.chat_id, count)
    try:
        async with typing(context, anchor.chat_id):
            downloaded = await asyncio.gather(*(_download_photo(m, context) for m in messages))
            images = [img for img in downloaded if img is not None]
            if not images:
                return
            answer = await _client(context).ask_image(
                prompt, images, "image/jpeg", history=_history(context)
            )
    except AIError as exc:
        logger.error("AI error on image request: %s", exc.detail)
        await anchor.reply_text(exc.user_message)
        return

    label = "[image]" if count == 1 else f"[{count} images]"
    _remember(context, f"{label} {caption or prompt}", answer)
    await _send_answer_to(anchor, answer)


# ------------------------------------------------------------------ quiz flow


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or context.chat_data is None:
        return

    subject: str | None = None
    count = DEFAULT_QUIZ_LENGTH
    for arg in context.args or []:
        if arg.isdigit():
            count = max(1, min(MAX_QUIZ_LENGTH, int(arg)))
        else:
            match = next((s for s in SUBJECTS if s.lower() == arg.lower()), None)
            if match:
                subject = match

    questions = await db.pick_quiz(user.id, count, subject)
    if not questions:
        where = f" in {subject}" if subject else ""
        await message.reply_text(
            f"No questions{where} yet.\n\n"
            "Use /add and photograph a page from your book to build your bank."
        )
        return

    session = quiz.start_session(context.chat_data, questions, subject)
    await _send_current_question(message, session)


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
        await query.answer("That quiz has ended. Send /quiz to start a new one.", show_alert=True)
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    await _reply_md(message, quiz.render_stats(await db.stats(user.id)))


async def weak_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    await _reply_md(message, quiz.render_weak(await db.weak_topics(user.id)))


async def bank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    rows = await db.count_questions(user.id)
    if not rows:
        await message.reply_text("Your question bank is empty. Use /add to fill it.")
        return
    total = sum(row["n"] for row in rows)
    lines = [f"📚 {total} questions in your bank", ""]
    lines.extend(f"{row['subject']:<14} {row['n']}" for row in rows)
    await message.reply_text("\n".join(lines))


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


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return

    args = context.args or []
    if not args or not args[0].isdigit() or not 0 <= int(args[0]) <= 23:
        await message.reply_text(
            "Give me an hour in 24h IST.\n\n"
            "  /daily 6      → 10 questions every day at 6 AM\n"
            "  /daily 21 20  → 20 questions every day at 9 PM"
        )
        return

    hour = int(args[0])
    count = max(1, min(MAX_QUIZ_LENGTH, int(args[1]))) if len(args) > 1 and args[1].isdigit() else DEFAULT_QUIZ_LENGTH

    await db.set_daily(user.id, message.chat_id, hour, count)
    _schedule_daily(context.application, user.id, message.chat_id, hour, count)
    await message.reply_text(
        f"⏰ Daily quiz set: {count} questions at {hour:02d}:00 IST.\n"
        "Cancel any time with /nodaily."
    )


async def nodaily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    await db.set_daily(user.id, message.chat_id, None)
    for job in context.application.job_queue.get_jobs_by_name(f"daily-{user.id}"):
        job.schedule_removal()
    await message.reply_text("Daily quiz cancelled.")


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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("quiz", quiz_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("weak", weak_command))
    application.add_handler(CommandHandler("bank", bank_command))
    application.add_handler(CommandHandler("daily", daily_command))
    application.add_handler(CommandHandler("nodaily", nodaily_command))
    application.add_handler(CallbackQueryHandler(on_answer, pattern=r"^q:"))
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
