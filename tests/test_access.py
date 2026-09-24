import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

import access


def run(coro):
    return asyncio.run(coro)


def test_password_match_ignores_surrounding_space():
    assert access.password_matches("  open sesame ", "open sesame")
    assert not access.password_matches("open", "open sesame")


def test_clean_name():
    assert access.clean_name("  Ravi   Kumar ") == "Ravi Kumar"
    assert access.clean_name("R") is None
    assert access.clean_name("/start") is None
    assert access.clean_name("x" * 41) is None


def test_lockout_after_max_tries():
    state: dict = {}
    for expected_left in range(access.MAX_TRIES - 1, 0, -1):
        assert access.record_wrong(state, 100.0) == expected_left
    assert access.record_wrong(state, 100.0) == 0
    assert access.locked_for(state, 100.0) == access.LOCK_SECONDS
    assert access.locked_for(state, 100.0 + access.LOCK_SECONDS) == 0


def test_user_rows(sqlite_db):
    assert run(sqlite_db.get_user(7)) is None
    run(sqlite_db.add_user(7, "Ravi", "ravi_k"))
    run(sqlite_db.add_user(7, "Ravi Kumar", "ravi_k"))  # re-register renames, no duplicate
    users = run(sqlite_db.list_users())
    assert [(u["user_id"], u["name"]) for u in users] == [(7, "Ravi Kumar")]
    assert "Ravi Kumar @ravi_k" in access.render_users(users)


class FakeChat:
    def __init__(self, sent):
        self.sent = sent

    async def send_message(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, text, sent):
        self.text, self.sent, self.deleted = text, sent, False
        self.chat = FakeChat(sent)

    async def reply_text(self, text):
        self.sent.append(text)

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _send(text, context, sent, user_id=42):
    message = FakeMessage(text, sent)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="ravi_k"),
        effective_message=message,
        callback_query=None,
    )
    try:
        run(access.gate(update, context))
    except ApplicationHandlerStop:
        return message, True
    return message, False


@pytest.fixture
def gated(sqlite_db, monkeypatch):
    monkeypatch.setattr(access, "BOT_PASSWORD", "open sesame")
    monkeypatch.setattr(access, "ADMIN_USER_IDS", frozenset({1}))
    return SimpleNamespace(user_data={}, bot=FakeBot())


def test_signup_flow(gated, sqlite_db):
    sent: list[str] = []

    _, stopped = _send("what should I study?", gated, sent)
    assert stopped and sent[-1] == access.ASK_PASSWORD

    message, stopped = _send("wrong", gated, sent)
    assert stopped and message.deleted and sent[-1].startswith("❌ Wrong password")

    message, stopped = _send("open sesame", gated, sent)
    assert stopped and message.deleted and sent[-1] == access.ASK_NAME

    _, stopped = _send("Ravi", gated, sent)
    assert stopped and sent[-1].startswith("Welcome, Ravi")
    assert run(sqlite_db.get_user(42))["name"] == "Ravi"
    assert gated.bot.messages == [(1, "👤 New user: Ravi @ravi_k (id 42)")]

    count = len(sent)
    _, stopped = _send("what should I study?", gated, sent)
    assert not stopped and len(sent) == count  # registered: passes through untouched


def test_admin_skips_gate(gated):
    _, stopped = _send("hi", gated, [], user_id=1)
    assert not stopped


def test_locked_out_user_cannot_guess(gated):
    sent: list[str] = []
    _send("hi", gated, sent)
    for _ in range(access.MAX_TRIES):
        _send("guess", gated, sent)
    _send("open sesame", gated, sent)
    assert sent[-1].startswith("Too many wrong tries")
    assert access.ASK_NAME not in sent


def test_gate_off_without_password(sqlite_db, monkeypatch):
    monkeypatch.setattr(access, "BOT_PASSWORD", "")
    _, stopped = _send("hi", SimpleNamespace(user_data={}, bot=FakeBot()), [])
    assert not stopped
