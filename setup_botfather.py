"""One-shot BotFather setup: pushes the command menu, description and about text.

Everything here is also doable by hand in @BotFather; this just does it over the
Bot API so it is repeatable. Run it once after creating the bot:

    python setup_botfather.py

Reads TELEGRAM_BOT_TOKEN from .env - the token is never printed.
"""

from __future__ import annotations

import sys

import httpx

from config import TELEGRAM_BOT_TOKEN

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

COMMANDS = [
    {"command": "add", "description": "Photograph a book page to make MCQs"},
    {"command": "quiz", "description": "Start a practice quiz"},
    {"command": "stats", "description": "Your accuracy by subject"},
    {"command": "weak", "description": "Your weakest topics"},
    {"command": "bank", "description": "How many questions you have"},
    {"command": "daily", "description": "Schedule a daily quiz"},
    {"command": "nodaily", "description": "Cancel the daily quiz"},
    {"command": "done", "description": "Stop add mode"},
    {"command": "help", "description": "How to use the bot"},
    {"command": "start", "description": "Welcome"},
]

DESCRIPTION = (
    "TSLPRB PC/SI prep. Photograph a page from your book and I turn it into exam MCQs, then drill you with spaced repetition. Also solves any question you send."
)

SHORT_DESCRIPTION = "TSLPRB PC/SI prep: turn your books into practice MCQs."


def call(method: str, payload: dict | None = None) -> dict:
    response = httpx.post(f"{API}/{method}", json=payload or {}, timeout=30.0)
    data = response.json()
    if not data.get("ok"):
        raise SystemExit(f"{method} failed: {data.get('description', response.text)}")
    return data["result"]


def main() -> int:
    me = call("getMe")
    print(f"Authenticated as @{me['username']} (id {me['id']}, name {me['first_name']!r})")

    call("setMyCommands", {"commands": COMMANDS})
    print(f"Command menu set ({len(COMMANDS)} commands)")

    call("setMyDescription", {"description": DESCRIPTION})
    print("Description set (shown on the empty chat screen)")

    call("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
    print("About text set (shown on the bot profile)")

    installed = call("getMyCommands")
    for command in installed:
        print(f"  /{command['command']} - {command['description']}")

    print("\nDone. Reopen the chat in Telegram to see the menu button.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
