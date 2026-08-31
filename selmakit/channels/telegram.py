from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from selmakit.attachments import (
    Attachment,
    DEFAULT_MAX_BYTES,
    IMAGE_SUFFIXES,
    find_attachments,
    pair_renderable_siblings,
)
from selmakit.message import QueueItem

logger = logging.getLogger(__name__)
_MAX_CHARS = 4096
# Photos have their own, lower ceiling in the bot API; a larger image still goes
# out, as a document.
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
# A "typing…" chat action expires after ~5s, so it has to be re-sent while the
# turn runs. 4s leaves headroom without hammering the API.
_TYPING_REFRESH_SECONDS = 4.0
# The Bot API throttles a bot to roughly one message *per chat per second*, and
# edits count against the same budget. A local model can fire twenty tool calls
# in a few seconds, so the progress line is written at most this often; the rest
# coalesce into the next write. See _write_tool_status().
_TOOL_EDIT_INTERVAL = 1.0
# How many tool names the progress message shows. Older ones scroll off with a
# count, so the message can never grow towards the 4096-char limit.
_TOOL_LINES = 12


class TelegramReply:
    """Buffers a turn's text and flushes it on ``done()``.

    While the turn runs the chat shows progress, because otherwise it shows
    nothing at all: a local model doing geo work can think for minutes, and a
    silent chat is indistinguishable from a dead one. Two signals:

    * a ``typing…`` chat action, kept alive from ``start_typing()`` until
      ``done()`` / ``send_error()``;
    * one **single** message listing the tools as they are called, edited in
      place (``show_tools``, on by default).

    With an ``attach_root`` set, ``done()`` also uploads the artefacts the answer
    names — but only files inside that root, see ``selmakit.attachments``.
    ``attach_root=None`` (the default) attaches nothing.
    """

    #: What this channel can actually *show*. An image renders inline in the
    #: chat; anything else is a document the reader opens in Telegram's own
    #: viewer, which runs no JavaScript and fetches nothing — an HTML map built
    #: from CDN scripts (folium's, for one) is a blank white page there.
    RENDERABLE_SUFFIXES = IMAGE_SUFFIXES

    #: Forms to look for beside an artefact this channel cannot show, best
    #: first. Only the channel may answer this, so it is stated here and passed
    #: into ``pair_renderable_siblings`` rather than assumed by the scanner.
    SUBSTITUTE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

    def __init__(
        self,
        msg: Any,
        attach_root: Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        show_tools: bool = True,
    ):
        self._msg = msg
        self._chunks: list[str] = []
        self._done_event = asyncio.Event()
        self._attach_root = attach_root
        self._max_bytes = max_bytes
        self._show_tools = show_tools
        self._typing_task: asyncio.Task | None = None
        self._tool_names: list[str] = []
        self._status_msg: Any = None
        self._last_status_write: float | None = None
        self._status_stale = False

    # -- progress: typing indicator ---------------------------------------

    def start_typing(self) -> None:
        """Show ``typing…`` until the turn ends. Idempotent, never blocks.

        The action itself expires after a few seconds, so a task refreshes it.
        That task *must* not outlive the turn — a chat left permanently typing
        by a leaked refresher is worse than no indicator at all — so every exit
        path (``done``, ``send_error``) stops it from a ``finally``.
        """
        if self._typing_task is None or self._typing_task.done():
            self._typing_task = asyncio.create_task(self._keep_typing())

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self._msg.reply_chat_action("typing")
            except Exception as e:
                # A chat action is cosmetic; it must never break the reply.
                logger.debug("Telegram: chat action failed: %s", e)
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    def _stop_typing(self) -> None:
        task, self._typing_task = self._typing_task, None
        if task is not None:
            task.cancel()

    # -- progress: tool names ---------------------------------------------

    async def send_tool(self, name: str, args: str | None = None) -> None:
        """Append ``name`` to the turn's progress message.

        Deliberately name-only: ``args`` can be a whole document and a single
        run calls twenty tools, so the useful signal — *which* tool is running —
        is also the only affordable one on a phone screen.
        """
        self.start_typing()  # safety net for a reply the channel did not start
        if not self._show_tools:
            return
        self._tool_names.append(name)
        self._status_stale = True
        now = asyncio.get_running_loop().time()
        if (
            self._last_status_write is not None
            and now - self._last_status_write < _TOOL_EDIT_INTERVAL
        ):
            return  # coalesced into the next write — see _TOOL_EDIT_INTERVAL
        await self._write_tool_status()

    async def _write_tool_status(self) -> None:
        """Send the progress message once, then edit that same message.

        One message per *turn* rather than per tool call: the chat stays
        readable and the turn cannot be throttled into stalling by its own
        progress reporting.
        """
        shown = self._tool_names[-_TOOL_LINES:]
        lines = [f"🔧 {n}" for n in shown]
        if len(self._tool_names) > len(shown):
            lines.insert(0, f"… {len(self._tool_names) - len(shown)} more")
        text = "\n".join(lines)
        try:
            if self._status_msg is None:
                self._status_msg = await self._msg.reply_text(text)
            else:
                await self._status_msg.edit_text(text)
        except Exception as e:
            # Progress is cosmetic too — a throttled edit must not cost the answer.
            logger.debug("Telegram: could not update tool status: %s", e)
        self._last_status_write = asyncio.get_running_loop().time()
        self._status_stale = False

    async def send_chunk(self, text: str) -> None:
        self.start_typing()  # safety net for a reply the channel did not start
        self._chunks.append(text)

    # Telegram is a plain-text channel with no side panel: the remaining
    # live-progress parts of ReplyHandle are accepted and dropped. They must
    # still exist — the worker calls them unconditionally under /verbose, and a
    # missing one is an AttributeError that ends the turn as an error.
    async def send_tool_result(
        self, name: str, result: str, duration: float | None = None, error: bool = False
    ) -> None:
        pass  # a result can be tens of kilobytes; the tool *name* is the signal

    async def send_thinking(self, text: str) -> None:
        pass

    async def send_metrics(self, metrics: dict) -> None:
        pass  # token accounting is a debugging view, not something a chat wants

    async def send_approval(self, pending: list) -> None:
        """Ask for approval in text — Telegram has no ✅/🚫 buttons wired.

        Appended to the chunks rather than sent on its own, so ``done()`` flushes
        it together with whatever the model said before it stopped.
        """
        names = ", ".join(str(p.get("tool_name", "?")) for p in pending) or "?"
        self._chunks.append(
            f"\n\n⏸ Waiting for approval: {names}\nReply /approve or /deny."
        )

    async def send_file(self, path: str, caption: str | None = None) -> None:
        """Upload a file to the chat.

        Images go out as a photo so they render **inline** — a map the user has
        to download first is barely better than the path they got before.
        Everything else, and any image past the photo ceiling, goes as a document.
        """
        file = Path(path)
        # Off-loop: the channels, the worker, the heartbeat and cron all share
        # one event loop, so a blocking call here stalls all of them.
        stat = await asyncio.to_thread(file.stat)
        if file.suffix.lower() in IMAGE_SUFFIXES and stat.st_size <= _MAX_PHOTO_BYTES:
            await self._msg.reply_photo(file, caption=caption)
        else:
            await self._msg.reply_document(file, caption=caption)

    async def _attach_named_files(self, text: str) -> None:
        """Upload the artefacts ``text`` names, once each, in order.

        ``text`` is the model's own answer, so ``find_attachments`` refuses
        anything that does not resolve inside ``attach_root`` — see
        ``selmakit.attachments`` for why that check is load-bearing.

        The answer names whichever form the agent happened to mention, and the
        agent does not know it is talking to Telegram. So an artefact this
        channel cannot render gets its sibling picture sent alongside it, per
        the table above — the model is never asked to pick a format.
        """
        if self._attach_root is None:
            return  # attaching is off — the default
        # The scan resolves and stats every candidate — off the shared loop.
        attachments = await asyncio.to_thread(self._collect, text)
        for attachment in attachments:
            if attachment.too_large:
                await self._msg.reply_text(
                    f"📎 {attachment.path.name} is too large to send "
                    f"({attachment.size / 1_000_000:.1f} MB, limit "
                    f"{self._max_bytes / 1_000_000:.0f} MB)."
                )
                continue
            try:
                await self.send_file(str(attachment.path))
            except Exception as e:
                # A failed upload costs an attachment, not the answer.
                logger.warning("Telegram: could not send %s: %s", attachment.path, e)

    def _collect(self, text: str) -> list[Attachment]:
        """Blocking half of ``_attach_named_files``: scan, then pair. Off-loop."""
        found = find_attachments(text, self._attach_root, max_bytes=self._max_bytes)
        return pair_renderable_siblings(
            found,
            self._attach_root,
            renderable=self.RENDERABLE_SUFFIXES,
            substitutes=self.SUBSTITUTE_SUFFIXES,
            max_bytes=self._max_bytes,
        )

    async def done(self) -> None:
        try:
            if self._status_stale:
                # The last tool calls were coalesced away; leave the progress
                # message showing what actually ran.
                await self._write_tool_status()
            text = "".join(self._chunks).strip()
            if text:
                for i in range(0, len(text), _MAX_CHARS):
                    await self._msg.reply_text(text[i:i + _MAX_CHARS])
                await self._attach_named_files(text)
        finally:
            self._stop_typing()
            self._done_event.set()

    async def send_error(self, e: Exception) -> None:
        try:
            await self._msg.reply_text(f"Error: {e}")
        finally:
            self._stop_typing()
            self._done_event.set()

    async def wait(self) -> None:
        await self._done_event.wait()


class TelegramChannel:
    """Telegram channel — enqueues messages and waits for the reply.

    ``allowed_chat_ids`` is the access list: only those chats can drive the
    agent. An empty list accepts every chat — see ``_log_access_policy``.

    ``attach_root`` switches on artefact delivery: after each answer the channel
    uploads the files that answer names, confined to that directory (the
    gateway passes the state dir, which is also the file tools' sandbox root).
    ``None`` — the default — attaches nothing.

    ``show_tools`` posts the names of the tools a turn calls, in one message
    edited in place — see ``TelegramReply``. On by default; turn it off for a
    deployment that wants the chat to carry answers only.
    """

    def __init__(
        self,
        token: str,
        queue: asyncio.Queue,
        allowed_chat_ids: Sequence[int] | None = None,
        attach_root: str | Path | None = None,
        show_tools: bool = True,
    ):
        self._token = token
        self._queue = queue
        self._allowed_chat_ids = set(allowed_chat_ids or ())
        self._attach_root = Path(attach_root) if attach_root is not None else None
        self._show_tools = show_tools

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

        reply = TelegramReply(
            msg, attach_root=self._attach_root, show_tools=self._show_tools
        )
        # Typing starts here, not at the first send: the wait for a queue slot
        # and the model's first token are exactly the silence being fixed.
        reply.start_typing()
        await self._queue.put(QueueItem(session_key=session_key, prompt=prompt, reply=reply))
        await reply.wait()

    @staticmethod
    def _silence_httpx_request_log() -> None:
        """Keep the bot token out of the log file.

        python-telegram-bot talks to the Bot API over httpx, which logs every
        request URL at INFO — and the token *is* a path segment of that URL. A
        gateway logging at INFO therefore writes the bot credential to disk in
        clear text, once per poll. WARNING keeps httpx's actual problems visible.
        """
        logging.getLogger("httpx").setLevel(logging.WARNING)

    async def start(self) -> None:
        self._log_access_policy()
        self._silence_httpx_request_log()
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
