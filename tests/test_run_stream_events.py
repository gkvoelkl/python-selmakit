"""Finalization and result forwarding in ``Agent.run_stream_events``.

Everything here drives a real ``selmakit.Agent`` over pydantic-ai's ``TestModel``
— no provider, no network, no state beyond a tmp state dir. The three behaviours
pinned here each cost a lost turn or an unreachable result when they regress, and
none of them is visible from the happy path a normal chat exercises.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.models.test import TestModel

from selmakit.agent import Agent


def _agent(tmp_path, *, validator=None) -> Agent:
    agent = Agent(
        model=TestModel(custom_output_text="RAW"),
        capabilities=[],
        state_dir=tmp_path,
    )
    if validator is not None:
        agent.output_validator(validator)
    return agent


def _note(output: str) -> str:
    return output + " [NOTE]"


def test_result_event_is_forwarded_to_the_consumer(tmp_path):
    """The run result must reach the caller, not just the finalizer.

    It is the only carrier of the validated output, so swallowing it puts the
    text the user was given out of reach of every stream consumer.
    """
    agent = _agent(tmp_path, validator=_note)

    async def go():
        seen = []
        async with agent.run_stream_events("hi", session_key="s") as (is_cmd, value):
            assert not is_cmd
            async for event in value:
                seen.append(event)
        return seen

    events = asyncio.run(go())
    results = [e for e in events if isinstance(e, AgentRunResultEvent)]
    assert len(results) == 1
    assert results[0].result.output == "RAW [NOTE]"


def test_validated_output_is_persisted_separately_from_history(tmp_path):
    """The validator's answer is in the meta key; the model's is in the history.

    A validator changes the run's output value without rewriting the
    ModelResponse, so the two genuinely differ and both are worth keeping.
    """
    agent = _agent(tmp_path, validator=_note)

    async def go():
        async with agent.run_stream_events("hi", session_key="s") as (_, value):
            async for _event in value:
                pass

    asyncio.run(go())

    assert agent.last_validated_output("s") == "RAW [NOTE]"
    texts = [
        part.content
        for message in agent._session_store.load("s")
        for part in getattr(message, "parts", [])
        if type(part).__name__ == "TextPart"
    ]
    assert texts == ["RAW"], "history should hold what the model said, unannotated"


def test_completed_turn_survives_a_raising_consumer(tmp_path):
    """A consumer that fails *after* the run finished must not cost the turn.

    The exception is thrown in at the `yield`, so without a `finally` the
    finalizer is skipped and a fully completed turn is dropped on the floor.
    """
    agent = _agent(tmp_path)

    async def go():
        try:
            async with agent.run_stream_events("hi", session_key="s") as (_, value):
                async for _event in value:
                    pass
                raise RuntimeError("post-processing blew up")
        except RuntimeError:
            pass

    asyncio.run(go())
    assert agent.message_count("s") > 0, "completed turn was lost"
    assert agent.last_validated_output("s") == "RAW"


def test_consumer_raising_mid_run_leaves_no_stale_state(tmp_path, caplog):
    """Failing on the first event aborts the run itself — there is no result.

    Nothing can be persisted here, and pretending otherwise would invent a turn
    that never completed. What must hold is that the finalizer is still *reached*
    (so `pending_approvals` cannot go stale) and that the loss is reported.
    """
    agent = _agent(tmp_path)

    async def go():
        try:
            async with agent.run_stream_events("hi", session_key="s") as (_, value):
                async for _event in value:
                    raise RuntimeError("consumer blew up")
        except RuntimeError:
            pass

    with caplog.at_level(logging.WARNING, logger="selmakit.agent"):
        asyncio.run(go())

    assert any("stream abandoned" in r.message for r in caplog.records)
    assert agent.pending_approvals("s") is None


def test_abandoned_stream_is_reported_not_swallowed(tmp_path, caplog):
    """Breaking out early leaves nothing to persist — but must not be silent."""
    agent = _agent(tmp_path)

    async def go():
        async with agent.run_stream_events("hi", session_key="s") as (_, value):
            async for _event in value:
                break

    with caplog.at_level(logging.WARNING, logger="selmakit.agent"):
        asyncio.run(go())

    assert any("stream abandoned" in r.message for r in caplog.records)
    assert agent.message_count("s") == 0
