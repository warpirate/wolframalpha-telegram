"""Password gate: a new user sends the bot password, then a name, before anything else works.

Runs as a handler group ahead of every other handler. Registered users pass straight
through; everyone else only ever talks to the sign-up flow.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import time

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

import db
from config import ADMIN_USER_IDS, BOT_PASSWORD

logger = logging.getLogger(__name__)

MAX_TRIES = 5
LOCK_SECONDS = 15 * 60
MIN_NAME, MAX_NAME = 2, 40
# An album arrives as several updates at once; prompt only once for all of them.
REPROMPT_SECONDS = 5

SIGNUP_KEY = "signup"
REGISTERED_KEY = "registered"

ASK_PASSWORD = "🔒 This bot is private. Send the password to continue."
ASK_NAME = "✅ Password accepted. What's your name?"
BAD_NAME = f"Send just your name ({MIN_NAME}–{MAX_NAME} characters)."
LOCKED = "Too many wrong tries. Try again in {minutes} min."
WRONG = "❌ Wrong password. {left} tr{plural} left."
BUTTON_BLOCKED = "Send the password first."


def enabled() -> bool:
    return bool(BOT_PASSWORD)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def password_matches(given: str, expected: str = "") -> bool:
    expected = expected or BOT_PASSWORD
    return hmac.compare_digest(given.strip().encode(), expected.encode())


def clean_name(text: str) -> str | None:
    name = " ".join((text or "").split())
    if not MIN_NAME <= len(name) <= MAX_NAME or name.startswith("/"):
        return None
    return name


def locked_for(state: dict, now: float) -> int:
    """Seconds left on a lockout, 0 when not locked."""
    return max(0, int(state.get("locked_until", 0) - now))


def record_wrong(state: dict, now: float) -> int:
    """Count a wrong password. Returns tries left; 0 means the user is now locked out."""
    state["fails"] = state.get("fails", 0) + 1
    left = MAX_TRIES - state["fails"]
    if left <= 0:
        state["fails"] = 0
        state["locked_until"] = now + LOCK_SECONDS
        return 0
    return left


async def is_registered(user_id: int, user_data: dict | None) -> bool:
    if user_data is not None and user_data.get(REGISTERED_KEY):
        return True
    if await db.get_user(user_id) is None:
        return False
    if user_data is not None:
        user_data[REGISTERED_KEY] = True
    return True


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Let registered users through; walk everyone else through password then name."""
    if not enabled():
        return
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    if is_admin(user.id) or await is_registered(user.id, context.user_data):
        return

    if update.callback_query is not None:
        with contextlib.suppress(TelegramError):
            await update.callback_query.answer(BUTTON_BLOCKED, show_alert=True)
        raise ApplicationHandlerStop

    message = update.effective_message
    if message is None or context.user_data is None:
        raise ApplicationHandlerStop
    state = context.user_data.setdefault(SIGNUP_KEY, {})
    text = (message.text or "").strip()
    now = time.monotonic()

    if state.get("step") == "name" and text:
        name = clean_name(text)
        if name is None:
            await message.reply_text(BAD_NAME)
            raise ApplicationHandlerStop
        await db.add_user(user.id, name, user.username or "")
        context.user_data.pop(SIGNUP_KEY, None)
        context.user_data[REGISTERED_KEY] = True
        logger.info("Registered user %s as %r", user.id, name)
        await message.reply_text(f"Welcome, {name}! Send /start to see what I can do.")
        await _tell_admins(context, name, user.username, user.id)
        raise ApplicationHandlerStop

    wait = locked_for(state, now)
    if wait:
        await message.reply_text(LOCKED.format(minutes=wait // 60 + 1))
        raise ApplicationHandlerStop

    if state.get("step") == "password" and text and not text.startswith("/"):
        # The password should not sit in the chat history.
        with contextlib.suppress(TelegramError):
            await message.delete()
        if password_matches(text):
            state.clear()
            state["step"] = "name"
            await message.chat.send_message(ASK_NAME)
        else:
            logger.info("Wrong password from user %s", user.id)
            left = record_wrong(state, now)
            if left:
                reply = WRONG.format(left=left, plural="y" if left == 1 else "ies")
            else:
                reply = LOCKED.format(minutes=LOCK_SECONDS // 60)
            await message.chat.send_message(reply)
        raise ApplicationHandlerStop

    if state.get("step") != "name" and now - state.get("prompted_at", -REPROMPT_SECONDS) >= REPROMPT_SECONDS:
        state["step"] = "password"
        state["prompted_at"] = now
        await message.reply_text(ASK_PASSWORD)
    elif state.get("step") == "name":
        await message.reply_text(BAD_NAME)
    raise ApplicationHandlerStop


async def _tell_admins(context: ContextTypes.DEFAULT_TYPE, name: str, username: str | None, user_id: int) -> None:
    handle = f" @{username}" if username else ""
    for admin in ADMIN_USER_IDS:
        with contextlib.suppress(TelegramError):
            await context.bot.send_message(admin, f"👤 New user: {name}{handle} (id {user_id})")


def render_users(users: list[dict]) -> str:
    if not users:
        return "Nobody has registered yet."
    lines = [f"👥 {len(users)} registered user{'s' if len(users) != 1 else ''}:"]
    for number, row in enumerate(users, 1):
        handle = f" @{row['username']}" if row.get("username") else ""
        since = str(row.get("registered_at") or "")[:10]
        lines.append(f"{number}. {row['name']}{handle} — id {row['user_id']}, since {since}")
    return "\n".join(lines)
