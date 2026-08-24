"""Live progress for the Telegram channel (typing indicator + tool names).

Same approach as the attachment tests: a fake message object records what would
have gone out — no bot token, no network, no python-telegram-bot import.

Two properties are load-bearing here and both are about *bounds*:

* the typing refresher must not outlive the turn — a leaked task keeps a dead
  chat "typing" forever;
* the tool lines must not scale with the number of tool calls — the Bot API
  throttles a bot to roughly one message per second per chat, and a local model
  fires tool calls far faster than that.
"""

from __future__ import annotations

import asyncio

from selmakit.channels import telegram as tg
from selmakit.channels.telegram import TelegramReply
from selmakit.config import TelegramConfig


class _FakeSentMessage:
    """What ``reply_text`` returns: a message that can be edited in place."""

    def __init__(self, text: str) -> None:
        self.texts = [text]

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)

    @property
    def edits(self) -> int:
        return len(self.texts) - 1


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.sent: list[_FakeSentMessage] = []
        self.actions: list[str] = []

    async def reply_text(self, text: str) -> _FakeSentMessage:
        self.replies.append(text)
        sent = _FakeSentMessage(text)
        self.sent.append(sent)
        return sent

    async def reply_chat_action(self, action: str) -> None:
        self.actions.append(action)

    @property
    def outgoing(self) -> int:
        """Everything that costs a Bot API call — sends *and* edits."""
        return len(self.replies) + sum(s.edits for s in self.sent)


async def _settle() -> None:
    """Give freshly created/cancelled tasks a turn on the loop."""
    for _ in range(3):
        await asyncio.sleep(0)


# -- typing indicator ---------------------------------------------------------


def test_typing_starts_on_the_first_send_and_stops_when_done():
    async def turn():
        msg = _FakeMessage()
        reply = TelegramReply(msg)
        await reply.send_chunk("the answer")
        await _settle()
        started = list(msg.actions)
        task = reply._typing_task
        await reply.done()
        await _settle()
        return msg, started, task, reply

    msg, started, task, reply = asyncio.run(turn())

    assert started == ["typing"]
    assert task is not None and task.done()
    assert reply._typing_task is None
    assert msg.replies[-1] == "the answer"


def test_typing_stops_on_error_too():
    """The error path is the one that matters: a turn that blew up mid-way is
    exactly when a chat would otherwise be left typing forever."""

    async def turn():
        msg = _FakeMessage()
        reply = TelegramReply(msg)
        await reply.send_chunk("partial")
        await _settle()
        task = reply._typing_task
        await reply.send_error(RuntimeError("model died"))
        await _settle()
        return msg, task

    msg, task = asyncio.run(turn())

    assert task is not None and task.done()
    assert any("model died" in r for r in msg.replies)


def test_no_task_survives_the_turn():
    async def turn():
        reply = TelegramReply(_FakeMessage())
        await reply.send_chunk("x")
        await reply.send_tool("osm_features")
        await reply.done()
        await _settle()
        return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    assert asyncio.run(turn()) == set()


def test_the_indicator_is_refreshed_while_the_turn_runs(monkeypatch):
    """One action lasts ~5s; a geo turn lasts minutes."""
    monkeypatch.setattr(tg, "_TYPING_REFRESH_SECONDS", 0.01)

    async def turn():
        msg = _FakeMessage()
        reply = TelegramReply(msg)
        reply.start_typing()
        await asyncio.sleep(0.05)
        await reply.done()
        return msg

    assert len(asyncio.run(turn()).actions) >= 2


def test_a_failing_chat_action_never_breaks_the_reply():
    class _NoActions(_FakeMessage):
        async def reply_chat_action(self, action: str) -> None:
            raise RuntimeError("Telegram is down")

    async def turn():
        msg = _NoActions()
        reply = TelegramReply(msg)
        await reply.send_chunk("the answer")
        await _settle()
        await reply.done()
        return msg

    assert asyncio.run(turn()).replies == ["the answer"]


# -- tool names ---------------------------------------------------------------


def _run_tools(names: list[str], *, show_tools: bool = True) -> _FakeMessage:
    async def turn():
        msg = _FakeMessage()
        reply = TelegramReply(msg, show_tools=show_tools)
        for name in names:
            await reply.send_tool(name, args='{"bbox": [1, 2, 3, 4]}')
        await reply.send_chunk("done and dusted")
        await reply.done()
        await _settle()
        return msg

    return asyncio.run(turn())


def test_each_tool_call_shows_up_as_a_line():
    msg = _run_tools(["osm_features", "qgis_reproject"])

    assert msg.sent[0].texts[-1].splitlines() == ["🔧 osm_features", "🔧 qgis_reproject"]


def test_arguments_are_not_posted():
    """A single tool call's arguments can be a whole document."""
    msg = _run_tools(["osm_features"])

    assert "bbox" not in "".join(t for s in msg.sent for t in s.texts)


def test_show_tools_off_sends_nothing_for_tools():
    msg = _run_tools(["osm_features", "qgis_reproject"], show_tools=False)

    assert msg.replies == ["done and dusted"]


def test_a_burst_of_tool_calls_stays_bounded_and_still_answers():
    """Twenty tool calls in a run is normal for the geo agent. One message per
    call would be throttled by the Bot API and could stall the turn."""
    msg = _run_tools([f"tool_{i}" for i in range(20)])

    assert msg.replies[-1] == "done and dusted"
    assert len(msg.sent) == 2  # one progress message + the answer
    assert msg.outgoing <= 4
    # Nothing is lost by coalescing: the last write shows the final state.
    status = msg.sent[0].texts[-1]
    assert "🔧 tool_19" in status
    assert status.startswith("… 8 more")  # 20 calls, _TOOL_LINES shown


def test_show_tools_defaults_to_on():
    """The point of the feature is that a demo shows something by default."""
    assert TelegramConfig().show_tools is True
