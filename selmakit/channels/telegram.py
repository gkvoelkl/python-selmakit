from __future__ import annotations

import asyncio
import logging
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
    """Telegram channel — enqueues messages and waits for the reply."""

    def __init__(self, token: str, queue: asyncio.Queue):
        self._token = token
        self._queue = queue

    @staticmethod
    def _session_key(update: Any) -> str:
        chat = update.effective_chat
        is_group = chat.type in ("group", "supergroup", "channel")
        clean_id = str(chat.id).replace("-100", "")
        prefix = "group:" if is_group else ""
        return f"telegram:{prefix}{clean_id}"

    async def start(self) -> None:
        try:
            from telegram.ext import ApplicationBuilder, MessageHandler, filters as tg_filters
        except ImportError:
            logger.error(
                "Telegram channel enabled but python-telegram-bot is not installed — skipping. "
                "Install it with: pip install 'selmakit[telegram]'"
            )
            return

        app = ApplicationBuilder().token(self._token).build()

        async def handle(update: Any, context: Any) -> None:
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

        app.add_handler(MessageHandler(tg_filters.TEXT, handle))

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
