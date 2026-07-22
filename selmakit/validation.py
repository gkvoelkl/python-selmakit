"""Run-scoped helpers for output validators.

An ``@agent.output_validator`` (see ``Agent.output_validator``) runs after the
final output and receives a :class:`~pydantic_ai.tools.RunContext`. Its
``messages`` list is the *whole* conversation — earlier runs of the same
session plus compaction-summarised history — with no ready-made "only this
run" slice. A result-gate that needs to inspect what *this* run produced
(e.g. the ``ToolReturnPart``s emitted since the run started) would otherwise
have to reconstruct the run boundary by scanning the message layout.

It doesn't have to. Every ``ModelRequest``/``ModelResponse`` carries a public
``run_id``, and ``RunContext.run_id`` holds the current run's id, so the run
cut is a one-line filter on a public field — the same basis pydantic-ai uses
for its own ``AgentRunResult.new_messages()``. These helpers wrap that so a
consumer never touches message-layout internals.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.tools import RunContext

__all__ = ["run_messages", "tool_returns"]


def run_messages(ctx: RunContext[Any]) -> list[ModelMessage]:
    """The messages produced since the start of the current run.

    The output-validation analogue of ``AgentRunResult.new_messages()``:
    ``ctx.messages`` filtered to the current run via the public ``run_id``
    field. On a synthetic ``RunContext`` not backed by a run (``run_id is
    None``) it returns the full list unchanged.
    """
    if ctx.run_id is None:
        return list(ctx.messages)
    return [m for m in ctx.messages if m.run_id == ctx.run_id]


def tool_returns(ctx: RunContext[Any]) -> list[tuple[str, Any]]:
    """``(tool_name, content)`` for each tool result produced this run.

    Convenience over :func:`run_messages`: walks the run's messages and pulls
    out every ``ToolReturnPart`` as an ``(tool_name, content)`` pair, in call
    order. Extracting concrete artefacts (e.g. file paths) from ``content``
    stays the consumer's job — tool results are application-specific.
    """
    return [
        (part.tool_name, part.content)
        for message in run_messages(ctx)
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
