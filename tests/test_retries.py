"""Retry budgets: that the output side has one at all, and that raising one
budget does not reset the other.

pydantic-ai resolves `retries={"tools": N, "output": M}` key by key and defaults
each to 1, so the partial `{"tools": 4}` selmakit used to pass left output
validation at a single retry for the whole run — shared by every validator.
A graded validator spends it on the heaviest flaw, and the second flaw's
ModelRetry becomes a note the model never acts on.

Driven through a real run: what matters is how often the validator is actually
invited to retry, not what the budget looks like in a private attribute. The
model is a `FunctionModel` that answers with plain text every time — `TestModel`
is no good here, because its answer to a retry prompt does not reach the
validator at all and the budget drains without a single call.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from selmakit.agent import Agent


def _answering_model() -> FunctionModel:
    """Answers every request, retry prompt included, with the same plain text."""

    def answer(messages, info):
        return ModelResponse(parts=[TextPart(content="answer")])

    return FunctionModel(answer)


def _counting_agent(tmp_path, **kwargs) -> tuple[Agent, list[int]]:
    """An agent whose validator never accepts, so the run spends the whole
    output budget and the call count *is* the budget plus the first attempt."""
    agent = Agent(model=_answering_model(), capabilities=[],
                  state_dir=tmp_path, **kwargs)
    calls: list[int] = []

    @agent.output_validator
    def always_retry(output: str) -> str:
        calls.append(1)
        raise ModelRetry("not good enough")

    return agent, calls


def _run(agent: Agent) -> None:
    async def go():
        with pytest.raises(UnexpectedModelBehavior):
            await agent._agent.run("hi", deps="s")

    asyncio.run(go())


def test_output_validation_gets_more_than_one_retry(tmp_path):
    """The fix. With pydantic-ai's default the validator is asked twice (first
    attempt + one retry); a run with two flaws needs the second retry to reach
    the milder one."""
    agent, calls = _counting_agent(tmp_path)

    _run(agent)

    assert len(calls) == 3, "first attempt plus two retries — the output budget is 2"


def test_output_budget_is_overridable(tmp_path):
    """The budget is a parameter, not a hard-wired number: the value that suits a
    geo-agent is not the one that suits a chatbot."""
    agent, calls = _counting_agent(tmp_path, retries={"output": 0})

    _run(agent)

    assert len(calls) == 1, "no retries requested, so only the first attempt"


def test_partial_override_keeps_the_other_budget(tmp_path):
    """The trap this whole change is about, one level up: passing a partial dict
    to pydantic-ai silently drops the unnamed budget back to 1. selmakit merges
    over its own defaults, so raising `output` must leave the tool budget at 4."""
    agent = Agent(model=TestModel(), capabilities=[], state_dir=tmp_path,
                  retries={"output": 5})

    assert agent._retries == {"tools": 4, "output": 5}
    # The inner agent is where it has to arrive; the attribute is private, but a
    # value that never reaches pydantic-ai is precisely the defect being pinned.
    assert agent._agent._max_tool_retries == 4
    assert agent._agent._max_output_retries == 5


def test_defaults_reach_the_inner_agent(tmp_path):
    agent = Agent(model=TestModel(), capabilities=[], state_dir=tmp_path)

    assert agent._agent._max_tool_retries == 4
    assert agent._agent._max_output_retries == 2
