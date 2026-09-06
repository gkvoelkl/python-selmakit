"""Run-scoped validator helpers: the run cut, and the results a validator gets
to see.

The second half is the load-bearing one. `tool_returns` is what a result-gate
reads to decide whether a run is acceptable, and the harness `CodeMode`
capability moves tool results *out* of the message parts and into the metadata
of a single `run_code` return. A gate that misses them does not fail — it
passes everything, which is the failure a validator is built to prevent. These
pin the unpacking so switching CodeMode on cannot quietly disarm it.

No model and no run: `RunContext` is stubbed, since both helpers read nothing
but `run_id` and `messages`.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
)

from selmakit.validation import run_messages, tool_returns


class _Ctx:
    """Just enough RunContext for the two helpers."""

    def __init__(self, messages: list, run_id: str | None = None) -> None:
        self.messages = messages
        self.run_id = run_id


def _returns(*parts: Any) -> ModelRequest:
    return ModelRequest(parts=list(parts))


def _ret(name: str, content: Any, *, metadata: Any = None) -> ToolReturnPart:
    return ToolReturnPart(
        tool_name=name, content=content, tool_call_id=f"c-{name}", metadata=metadata
    )


def _code_mode_return(*nested: ToolReturnPart) -> ToolReturnPart:
    """A `run_code` result shaped the way CodeMode reports its nested calls:
    metadata['tool_returns'] keyed by tool_call_id (code_mode/_toolset.py)."""
    return _ret(
        "run_code",
        {"output": "done"},
        metadata={
            "code_mode": True,
            "tool_calls": {},
            "tool_returns": {p.tool_call_id: p for p in nested},
        },
    )


def test_run_messages_cuts_at_the_current_run():
    """A validator's ctx.messages is the whole conversation, not a run slice."""
    old = ModelResponse(parts=[TextPart(content="earlier")], run_id="r0")
    new = ModelResponse(parts=[TextPart(content="this run")], run_id="r1")

    assert run_messages(_Ctx([old, new], run_id="r1")) == [new]


def test_run_messages_without_a_run_falls_back_to_everything():
    """A synthetic RunContext is not backed by a run; dropping every message
    would be worse than returning the lot."""
    msgs = [ModelResponse(parts=[TextPart(content="x")], run_id="r0")]

    assert run_messages(_Ctx(msgs, run_id=None)) == msgs


def test_tool_returns_lists_plain_results_in_order():
    ctx = _Ctx([_returns(_ret("download", "80 objects"), _ret("render", "map.png"))])

    assert tool_returns(ctx) == [("download", "80 objects"), ("render", "map.png")]


def test_code_mode_nested_results_are_visible():
    """The defect: with CodeMode on, a gate reading only the top-level parts sees
    `run_code` and nothing of the calls that produced the answer — so a run that
    should have been retried passes instead."""
    ctx = _Ctx([_returns(_code_mode_return(_ret("download", "WARNING: bbox fallback")))])

    names = [name for name, _ in tool_returns(ctx)]
    assert "download" in names, "the nested call vanished — the gate has no basis left"
    assert ("download", "WARNING: bbox fallback") in tool_returns(ctx)


def test_nested_results_come_before_the_run_code_entry():
    """They ran inside the call, i.e. before it returned; order is what a gate
    scanning for 'the last thing that happened' relies on."""
    ctx = _Ctx([_returns(_code_mode_return(_ret("a", 1), _ret("b", 2)))])

    assert tool_returns(ctx) == [("a", 1), ("b", 2), ("run_code", {"output": "done"})]


def test_plain_metadata_is_not_treated_as_nesting():
    """Only the CodeMode marker unpacks; any other tool's metadata is left alone."""
    ctx = _Ctx([_returns(_ret("download", "ok", metadata={"tool_returns": "not mine"}))])

    assert tool_returns(ctx) == [("download", "ok")]


def test_unexpected_nested_shape_does_not_raise():
    """The metadata crosses a version boundary into pydantic-ai-harness. A shape
    change there must cost the nested results, not blow up inside a validator."""
    ctx = _Ctx([
        _returns(_ret("run_code", "r", metadata={"code_mode": True, "tool_returns": ["junk"]})),
        _returns(_ret("run_code", "r2", metadata={"code_mode": True})),
    ])

    assert tool_returns(ctx) == [("run_code", "r"), ("run_code", "r2")]
