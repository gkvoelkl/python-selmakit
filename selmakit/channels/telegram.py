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

    async def send_tool(self, name: str) -> None:
        pass  # Telegram doesn't show tool status

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
            logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
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
        await app.updater.start_polling()
        logger.info("Telegram channel active.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
