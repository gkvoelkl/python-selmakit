from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


@runtime_checkable
class ReplyHandle(Protocol):
    async def send_chunk(self, text: str) -> None: ...
    async def send_tool(self, name: str, args: str | None = None) -> None: ...
    async def send_tool_result(
        self, name: str, result: str, duration: float | None = None, error: bool = False
    ) -> None: ...
    async def send_thinking(self, text: str) -> None: ...
    async def send_approval(self, pending: list) -> None: ...
    # End-of-turn accounting under /verbose: token usage, wall-clock duration and
    # the model that actually answered (which a per-session override can change).
    async def send_metrics(self, metrics: dict) -> None: ...
    # An artefact — a rendered map, a PNG, an export — delivered as a file
    # rather than as a path in the answer text. Every channel implements it in
    # whatever its transport allows; see selmakit.attachments.
    async def send_file(self, path: str, caption: str | None = None) -> None: ...
    async def done(self) -> None: ...
    async def send_error(self, e: Exception) -> None: ...


class QueueItem(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_key: str
    prompt: str
    reply: ReplyHandle
