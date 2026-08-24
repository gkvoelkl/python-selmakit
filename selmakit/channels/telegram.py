from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from selmakit.message import QueueItem

logger = logging.getLogger(__name__)
_MAX_CHARS = 4096


class TelegramReply:
    def __init__(self, msg: Any):
        self._msg = msg
        self._chunks: list[str] = []
        self._done_event = asyncio.Event()

    async def send_chunk(self, text: str) -> None:
        self._chunks.append(text)

    # Telegram is a plain-text channel with no side panel: the live-progress
    # parts of ReplyHandle are accepted and dropped. They must still exist —
    # the worker calls them unconditionally under /verbose, and a missing one
    # is an AttributeError that ends the turn as an error.
    async def send_tool(self, name: str, args: str | None = None) -> None:
        pass  # Telegram doesn't show tool status

    async def send_tool_result(
        self, name: str, result: str, duration: float | None = None, error: bool = False
    ) -> None:
        pass

    async def send_thinking(self, text: str) -> None:
        pass

    async def send_approval(self, pending: list) -> None:
        """Ask for approval in text — Telegram has no ✅/🚫 buttons wired.

        Appended to the chunks rather than sent on its own, so ``done()`` flushes
        it together with whatever the model said before it stopped.
        """
        names = ", ".join(str(p.get("tool_name", "?")) for p in pending) or "?"
        self._chunks.append(
            f"\n\n⏸ Waiting for approval: {names}\nReply /approve or /deny."
        )

    async def done(self) -> None:
        text = "".join(self._chunks).strip()
        if text:
            for i in range(0, len(text), _MAX_CHARS):
                await self._msg.reply_text(text[i:i + _MAX_CHARS])
        self._done_event.set()

    async def send_error(self, e: Exception) -> None:
        await self._msg.reply_text(f"Error: {e}")
        self._done_event.set()

    async def wait(self) -> None:
        await self._done_event.wait()


class TelegramChannel:
    """Telegram channel — enqueues messages and waits for the reply.

    ``allowed_chat_ids`` is the access list: only those chats can drive the
    agent. An empty list accepts every chat — see ``_log_access_policy``.
    """

    def __init__(
        self,
        token: str,
        queue: asyncio.Queue,
        allowed_chat_ids: Sequence[int] | None = None,
    ):
        self._token = token
        self._queue = queue
        self._allowed_chat_ids = set(allowed_chat_ids or ())

    @staticmethod
    def _session_key(update: Any) -> str:
        chat = update.effective_chat
        is_group = chat.type in ("group", "supergroup", "channel")
        clean_id = str(chat.id).replace("-100", "")
        prefix = "group:" if is_group else ""
        return f"telegram:{prefix}{clean_id}"

    def _log_access_policy(self) -> None:
        """State who may drive the agent, once, at channel start.

        An empty allow-list stays permissive for backwards compatibility, so the
        warning is the only thing standing between an unconfigured install and a
        stranger with a shell. It has to read like a problem, not like boilerplate.
        """
        if self._allowed_chat_ids:
            logger.info(
                "Telegram: restricted to %d allowed chat id(s).", len(self._allowed_chat_ids)
            )
            return
        logger.warning(
            "Telegram: channels.telegram.allowed_chat_ids is EMPTY — anyone who finds "
            "this bot can drive the agent, including its filesystem and command tools. "
            "Restrict it by listing your chat id there. If you do not know your id, put "
            "a placeholder (e.g. [0]) in the list and message the bot: the channel logs "
            "the id of every message it drops."
        )

    def _is_allowed(self, update: Any) -> bool:
        """Whether ``update``'s chat may drive the agent; logs the id when not.

        Checked on ``effective_chat.id`` rather than the user: in a private chat
        the two coincide, and in a group the chat id is the group — allowing a
        whole group is a legitimate setup. The rejection log names the id
        because that is the only practical way for an owner to learn their own.
        """
        if not self._allowed_chat_ids:
            return True  # unrestricted — warned about at start, see _log_access_policy()

        chat = update.effective_chat
        if chat is not None and chat.id in self._allowed_chat_ids:
            return True

        logger.warning(
            "Telegram: ignored message from chat %s (not in "
            "channels.telegram.allowed_chat_ids). Add that id to allow it.",
            "unknown" if chat is None else chat.id,
        )
        return False

    async def _handle(self, update: Any, context: Any) -> None:
        # Dropped messages are never answered: a reply would confirm the bot
        # exists to whoever went looking for it.
        if not self._is_allowed(update):
            return

        msg = update.effective_message
        if not msg or not msg.text:
            return
        session_key = self._session_key(update)
        user = update.effective_user
        text = msg.text.strip()
        prompt = text if text.startswith("/") else f"[{user.first_name}]: {text}"

        reply = TelegramReply(msg)
        await self._queue.put(QueueItem(session_key=session_key, prompt=prompt, reply=reply))
        await reply.wait()

    async def start(self) -> None:
        self._log_access_policy()
        try:
            from telegram.ext import ApplicationBuilder, MessageHandler, filters as tg_filters
        except ImportError:
            logger.error(
                "Telegram channel enabled but python-telegram-bot is not installed — skipping. "
                "Install it with: pip install 'selmakit[telegram]'"
            )
            return

        app = ApplicationBuilder().token(self._token).build()

        app.add_handler(MessageHandler(tg_filters.TEXT, self._handle))

        await app.initialize()
        await app.start()
        # `updater` is None only for a webhook-mode Application; ApplicationBuilder
        # above builds the polling one, so this is set.
        assert app.updater is not None
        await app.updater.start_polling()
        logger.info("Telegram channel active.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
