from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


@runtime_checkable
class ReplyHandle(Protocol):
    async def send_chunk(self, text: str) -> None: ...
    async def done(self) -> None: ...
    async def send_error(self, e: Exception) -> None: ...


class QueueItem(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_key: str
    prompt: str
    reply: ReplyHandle
