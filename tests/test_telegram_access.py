"""Access control for the Telegram channel (channels.telegram.allowed_chat_ids).

Everything here drives ``TelegramChannel._handle`` directly with a fake update —
no bot token, no network, no python-telegram-bot import (``start()`` is what
touches the library, and it is never called).
"""

from __future__ import annotations

import asyncio
import logging

from selmakit.channels.telegram import TelegramChannel
from selmakit.config import TelegramConfig


class _FakeMessage:
    def __init__(self, text: str = "hallo"):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class _FakeChat:
    def __init__(self, chat_id: int, chat_type: str = "private"):
        self.id = chat_id
        self.type = chat_type


class _FakeUser:
    def __init__(self, first_name: str = "Ada"):
        self.first_name = first_name


class _FakeUpdate:
    def __init__(self, chat_id: int, text: str = "hallo", chat_type: str = "private"):
        self.effective_chat = _FakeChat(chat_id, chat_type)
        self.effective_user = _FakeUser()
        self.effective_message = _FakeMessage(text)


class _AutoCompleteQueue(asyncio.Queue):
    """Stands in for ``Gateway._worker``.

    ``_handle`` blocks on ``reply.wait()`` until the worker has finished sending
    the turn, so a test that only enqueues would hang. Completing the reply on
    put is the smallest thing that lets the handler return; with no chunks
    buffered, ``done()`` sends nothing — so an assertion that the chat received
    no message still means what it says.
    """

    async def put(self, item) -> None:
        await super().put(item)
        await item.reply.done()


def test_allowed_chat_id_reaches_the_queue():
    queue = _AutoCompleteQueue()
    channel = TelegramChannel(token="unused", queue=queue, allowed_chat_ids=[42])

    asyncio.run(channel._handle(_FakeUpdate(42), None))

    assert queue.qsize() == 1
    item = queue.get_nowait()
    assert item.session_key == "telegram:42"
    assert item.prompt == "[Ada]: hallo"


def test_rejected_chat_id_is_dropped_and_never_answered(caplog):
    queue = _AutoCompleteQueue()
    channel = TelegramChannel(token="unused", queue=queue, allowed_chat_ids=[42])
    update = _FakeUpdate(999)

    with caplog.at_level(logging.INFO):
        asyncio.run(channel._handle(update, None))

    assert queue.qsize() == 0
    # Answering would confirm the bot exists to whoever probed it.
    assert update.effective_message.replies == []
    # The owner reads their own chat id out of this line.
    assert "999" in caplog.text
    assert "allowed_chat_ids" in caplog.text


def test_group_chat_id_is_allowed_as_a_whole():
    """The check is on the chat, not the user — allowing a group is legitimate."""
    queue = _AutoCompleteQueue()
    channel = TelegramChannel(token="unused", queue=queue, allowed_chat_ids=[-1001234])

    asyncio.run(channel._handle(_FakeUpdate(-1001234, chat_type="supergroup"), None))

    assert queue.qsize() == 1


def test_empty_allow_list_accepts_everyone():
    """Backwards compatibility: an existing deployment keeps working on upgrade."""
    queue = _AutoCompleteQueue()
    channel = TelegramChannel(token="unused", queue=queue)

    asyncio.run(channel._handle(_FakeUpdate(999), None))

    assert queue.qsize() == 1


def test_empty_allow_list_warns_at_start(caplog):
    channel = TelegramChannel(token="unused", queue=asyncio.Queue())

    with caplog.at_level(logging.INFO):
        channel._log_access_policy()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "allowed_chat_ids" in warnings[0].getMessage()


def test_configured_allow_list_does_not_warn_at_start(caplog):
    channel = TelegramChannel(token="unused", queue=asyncio.Queue(), allowed_chat_ids=[42])

    with caplog.at_level(logging.INFO):
        channel._log_access_policy()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_allowed_chat_ids_defaults_to_empty():
    assert TelegramConfig().allowed_chat_ids == []
