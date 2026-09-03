"""Usage limits: that they are set at all, that they survive a resume, and that
hitting one does not cost the turn.

Before `limits` existed, `usage_limits` never reached pydantic-ai and every run
was silently capped at its default of 50 requests — enough for a chat, not for an
agent whose turn spends 18 tool calls. These pin the wiring (which is invisible
until a long turn dies) and the recovery (which is what makes a dead turn still
readable in the session file).

No provider and no network: `TestModel` for the wiring, and a `FunctionModel`
that only ever calls a tool for the runaway loop.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import DeferredToolRequests
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from selmakit.agent import Agent
from selmakit.config import LimitsConfig


def _looping_model() -> FunctionModel:
    """A model that answers every request with the same tool call, forever."""

    async def stream(messages, info):
        yield {0: DeltaToolCall(name="ping", json_args="{}")}

    return FunctionModel(stream_function=stream)


def _agent(tmp_path, *, model=None, limits=LimitsConfig()) -> Agent:
    agent = Agent(
        model=model or TestModel(custom_output_text="RAW"),
        capabilities=[],
        state_dir=tmp_path,
        limits=limits,
    )

    @agent._agent.tool_plain
    def ping() -> str:
        return "pong"

    return agent


def test_configured_limit_reaches_the_run(tmp_path):
    """The whole point: without this, `usage_limits` never arrives at pydantic-ai."""
    agent = _agent(tmp_path, limits=LimitsConfig(request_limit=7, total_tokens_limit=1234))

    _cmd, _prompt, kwargs = asyncio.run(agent._prepare_run("hi", "s"))

    limits = kwargs["usage_limits"]
    assert limits.request_limit == 7
    assert limits.total_tokens_limit == 1234


def test_no_limits_configured_passes_nothing(tmp_path):
    """A hand-built Agent without `limits` must behave exactly as before —
    pydantic-ai's own defaults, not a selmakit-invented budget."""
    agent = Agent(model=TestModel(), capabilities=[], state_dir=tmp_path)

    _cmd, _prompt, kwargs = asyncio.run(agent._prepare_run("hi", "s"))

    assert "usage_limits" not in kwargs


def test_limit_survives_an_approval_resume(tmp_path):
    """A /approve resume builds its own kwargs — the limit has to be set there too,
    or a resumed run restarts against the default budget."""
    agent = _agent(tmp_path, limits=LimitsConfig(request_limit=7))
    agent._session_store.set_meta(
        "s", "pending_approvals", [{"tool_call_id": "c1", "tool_name": "t", "args": "{}"}]
    )

    _cmd, prompt, kwargs = asyncio.run(agent._prepare_approval_resume("/approve", "s"))

    assert prompt is None, "a resume carries no new user prompt"
    assert kwargs["usage_limits"].request_limit == 7


def test_unattended_resume_keeps_the_limit(tmp_path):
    """The trap: `_run_unattended_autodeny` rebuilds kwargs through a whitelist,
    so a key missing from that tuple is dropped silently on every auto-deny
    resume — and the resumed run continues against pydantic-ai's default.

    Driven through the real method with a stubbed inner ``run``: the first call
    defers (as an approval-gated tool does), the second is the resume, and it is
    the second one's kwargs that matter.
    """
    agent = _agent(tmp_path, limits=LimitsConfig(request_limit=7))
    seen: list[dict] = []

    class _Result:
        def __init__(self, output):
            self.output = output

        def all_messages(self):
            return []

    async def fake_run(*args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return _Result(DeferredToolRequests(
                approvals=[ToolCallPart(tool_name="gated", args={}, tool_call_id="c1")]
            ))
        return _Result("done")

    agent._agent.run = fake_run  # type: ignore[method-assign]
    _cmd, prompt, kwargs = asyncio.run(agent._prepare_run("go", "s"))
    text = asyncio.run(agent._run_unattended_autodeny(prompt, "s", kwargs))

    assert text == "done"
    assert len(seen) == 2, "the gated call should have driven one auto-deny resume"
    assert seen[1]["usage_limits"].request_limit == 7, (
        "the resume fell back to pydantic-ai's default budget"
    )
    assert "message_history" in seen[1], "the resume supplies its own history"


def test_exceeding_the_limit_saves_the_turn(tmp_path, caplog):
    """The defect that costs the measurement: the run aborts, the exception is
    real, but the tool calls it already made must still be in the session file."""
    agent = _agent(tmp_path, model=_looping_model(), limits=LimitsConfig(request_limit=2))

    async def go():
        async with agent.run_stream_events("go", session_key="s") as (_is_cmd, value):
            async for _event in value:
                pass

    with caplog.at_level(logging.WARNING, logger="selmakit.agent"):
        try:
            asyncio.run(go())
        except UsageLimitExceeded:
            pass
        else:
            raise AssertionError("the run should still fail — only the loss is fixed")

    messages = agent._session_store.load("s")
    assert len(messages) > 1, "the aborted turn was lost"
    assert any(
        type(part).__name__ == "ToolCallPart"
        for message in messages
        for part in getattr(message, "parts", [])
    ), "the work the turn actually did should be in the history"
    assert any("usage limit" in r.message for r in caplog.records)
    assert agent.pending_approvals("s") is None


def test_limit_abort_does_not_fake_a_validated_output(tmp_path):
    """There is no answer, so nothing may be cached as one — a judge reading
    `last_validated_output` must see the turn produced none."""
    agent = _agent(tmp_path, model=_looping_model(), limits=LimitsConfig(request_limit=2))

    async def go():
        async with agent.run_stream_events("go", session_key="s") as (_is_cmd, value):
            async for _event in value:
                pass

    try:
        asyncio.run(go())
    except UsageLimitExceeded:
        pass

    assert agent.last_validated_output("s") is None
