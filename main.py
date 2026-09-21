"""Telegram entry point: a Wolfram-Alpha-style bot for text and image questions."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from typing import Any, AsyncIterator

from PIL import Image, UnidentifiedImageError
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_client import AIError, NebiusClient
from config import (
    HISTORY_TURNS,
    LOG_LEVEL,
    NEBIUS_API_KEY,
    NEBIUS_BASE_URL,
    NEBIUS_MODEL,
    NEBIUS_VISION_MODEL,
    TELEGRAM_BOT_TOKEN,
)
from formatter import prepare_for_telegram, unescape_markdown_v2
from prompts import IMAGE_DEFAULT_PROMPT

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("wolfram-bot")

AI_CLIENT_KEY = "ai_client"
HISTORY_KEY = "history"

MAX_PROMPT_CHARS = 4000
MAX_IMAGE_DIMENSION = 1600
MAX_IMAGE_BYTES = 3 * 1024 * 1024  # re-encode anything bigger before upload
MIN_JPEG_QUALITY = 55

GENERIC_ERROR = "Something went wrong. Please try again."
EMPTY_TEXT_REPLY = "Send me a question, an equation, or a photo of a problem and I'll solve it."
DOWNLOAD_ERROR = "I couldn't download that image from Telegram. Please try sending it again."
IMAGE_DECODE_ERROR = "I couldn't read that image. Try a clearer photo (JPEG or PNG)."

WELCOME = (
    "🔬 *Wolfram\\-style Solver*\n\n"
    "Send me a question as *text* or a *photo* and I'll return a structured answer:\n"
    "📥 Input · ✅ Result · 📊 Details · 💡 Notes\n\n"
    "*Try:*\n"
    "• `integrate x^2 sin(x) dx`\n"
    "• `solve 3x^2 - 5x + 2 = 0`\n"
    "• `convert 120 km/h to m/s`\n"
    "• `half life of carbon-14`\n"
    "• 📷 a photo of a handwritten equation, a circuit, or a textbook problem\n\n"
    "Use /help for tips\\."
)

HELP_EXTRA = (
    "\n\n*Tips*\n"
    "• Add a caption to a photo to ask something specific about it\n"
    "• One problem per message gives the most accurate answer\n"
    "• State units and assumptions if they matter\n"
    "• /reset clears the short conversation memory"
)


# --------------------------------------------------------------------- helpers


@contextlib.asynccontextmanager
async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> AsyncIterator[None]:
    """Keep the 'typing…' indicator alive for the duration of the block."""

    async def _loop() -> None:
        while True:
            try:
                await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramError:  # transient; the indicator is cosmetic
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


async def _send_answer(update: Update, answer: str) -> None:
    """Format and deliver an answer, falling back to plain text on parse errors."""
    message = update.effective_message
    if message is None:
        return

    chunks = prepare_for_telegram(answer)
    if not chunks:
        await message.reply_text(GENERIC_ERROR)
        return

    for chunk in chunks:
        try:
            await message.reply_text(
                chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            logger.warning("MarkdownV2 rejected by Telegram (%s); sending plain text", exc)
            await message.reply_text(
                unescape_markdown_v2(chunk),
                disable_web_page_preview=True,
            )


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


# -------------------------------------------------------------------- handlers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN_V2)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(WELCOME + HELP_EXTRA, parse_mode=ParseMode.MARKDOWN_V2)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.chat_data is not None:
        context.chat_data.pop(HISTORY_KEY, None)
    message = update.effective_message
    if message:
        await message.reply_text("Conversation memory cleared.")


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
    message = update.effective_message
    if message is None or not message.photo:
        return

    caption = (message.caption or "").strip() or IMAGE_DEFAULT_PROMPT
    if len(caption) > MAX_PROMPT_CHARS:
        caption = caption[:MAX_PROMPT_CHARS]

    photo = message.photo[-1]  # highest resolution variant
    logger.info(
        "Photo question from chat %s (%dx%d, caption: %s)",
        message.chat_id,
        photo.width,
        photo.height,
        "yes" if message.caption else "no",
    )

    try:
        async with typing(context, message.chat_id):
            try:
                telegram_file = await context.bot.get_file(photo.file_id)
                raw = bytes(await telegram_file.download_as_bytearray())
            except TelegramError as exc:
                logger.error("Failed to download photo: %s", exc)
                await message.reply_text(DOWNLOAD_ERROR)
                return

            try:
                image_bytes, mime_type = await asyncio.to_thread(_compress_image, raw)
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                logger.error("Failed to decode photo: %s", exc)
                await message.reply_text(IMAGE_DECODE_ERROR)
                return

            answer = await _client(context).ask_image(caption, image_bytes, mime_type)
    except AIError as exc:
        logger.error("AI error on image request: %s", exc.detail)
        await message.reply_text(exc.user_message)
        return

    _remember(context, f"[image] {caption}", answer)
    await _send_answer(update, answer)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(GENERIC_ERROR)


# ------------------------------------------------------------------- lifecycle


async def _post_init(application: Application) -> None:
    application.bot_data[AI_CLIENT_KEY] = NebiusClient(
        api_key=NEBIUS_API_KEY,
        base_url=NEBIUS_BASE_URL,
        model=NEBIUS_MODEL,
        vision_model=NEBIUS_VISION_MODEL,
    )
    logger.info(
        "Nebius client ready (model=%s, vision_model=%s, base_url=%s)",
        NEBIUS_MODEL,
        NEBIUS_VISION_MODEL,
        NEBIUS_BASE_URL,
    )


async def _post_shutdown(application: Application) -> None:
    client: Any = application.bot_data.pop(AI_CLIENT_KEY, None)
    if isinstance(client, NebiusClient):
        await client.aclose()
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    logger.info("Starting bot…")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
