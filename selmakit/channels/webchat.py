import asyncio
import json
import logging
import os

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from selmakit.message import QueueItem

_ANAM_TOKEN_URL = "https://api.anam.ai/v1/auth/session-token"

logger = logging.getLogger(__name__)


class _WebChatIn(BaseModel):
    user_id: str
    text: str
    user_name: str = "User"


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


class WebChatReply:
    def __init__(self, session_key: str):
        self._session_key = session_key
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send_chunk(self, text: str) -> None:
        await self._queue.put({"type": "chunk", "text": text})

    async def send_tool(self, name: str) -> None:
        await self._queue.put({"type": "tool", "name": name})

    async def done(self) -> None:
        await self._queue.put({"type": "done", "session_key": self._session_key})
        await self._queue.put(None)

    async def send_error(self, e: Exception) -> None:
        await self._queue.put({"type": "error", "message": str(e)})
        await self._queue.put(None)

    async def stream(self, timeout_s: float = 130.0):
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        _KEEPALIVE = 15.0
        while True:
            idle = loop.time() - last_activity
            remaining = timeout_s - idle
            if remaining <= 0:
                yield _sse({"type": "error", "message": "Stream timeout."})
                break
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=min(remaining, _KEEPALIVE)
                )
            except asyncio.TimeoutError:
                idle = loop.time() - last_activity
                if idle >= timeout_s:
                    yield _sse({"type": "error", "message": "Stream timeout."})
                    break
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            last_activity = loop.time()  # reset on every event
            yield _sse(item)


class WebChatChannel:
    """WebChat channel — enqueues messages, streams responses via SSE."""

    def __init__(
        self,
        queue: asyncio.Queue,
        alerts: asyncio.Queue,
        host: str = "0.0.0.0",
        port: int = 8000,
        timeout_seconds: int = 120,
        log_level: str = "info",
    ):
        self._queue = queue
        self._alerts = alerts
        self.host = host
        self.port = port
        self._timeout_seconds = timeout_seconds
        self._log_level = log_level
        self.app: FastAPI = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="selmakit WebChat")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

        @app.post("/webchat/stream")
        async def handle(incoming: _WebChatIn) -> StreamingResponse:
            reply = WebChatReply(session_key=incoming.user_id)
            await self._queue.put(QueueItem(
                session_key=incoming.user_id,
                prompt=incoming.text,
                reply=reply,
            ))
            return StreamingResponse(
                reply.stream(timeout_s=self._timeout_seconds + 10),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.get("/webchat/heartbeat/poll")
        async def poll_heartbeat():
            try:
                alert = self._alerts.get_nowait()
                return {"alert": alert}
            except asyncio.QueueEmpty:
                return {"alert": None}

        @app.get("/anam/session-token")
        async def anam_session_token():
            api_key   = os.environ.get("ANAM_API_KEY", "")
            avatar_id = os.environ.get("ANAM_AVATAR_ID", "")
            voice_id  = os.environ.get("ANAM_VOICE_ID", "")
            if not api_key:
                return JSONResponse({"error": "ANAM_API_KEY not set"}, status_code=503)
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    _ANAM_TOKEN_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"personaConfig": {
                        "name": "Selma",
                        "avatarId": avatar_id,
                        "voiceId": voice_id,
                        "llmId": "CUSTOMER_CLIENT_V1",
                    }},
                    timeout=10,
                )
            logger.info("Anam session-token | status=%d body=%s", resp.status_code, resp.text[:300])
            if not resp.is_success:
                return JSONResponse({"error": resp.text}, status_code=resp.status_code)
            data = resp.json()
            if "sessionToken" not in data:
                logger.error("Anam API response missing sessionToken field: %s", data)
                return JSONResponse({"error": f"Unexpected Anam response: {data}"}, status_code=502)
            return data

        return app

    async def start(self) -> None:
        import uvicorn
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level=self._log_level)
        server = uvicorn.Server(config)
        await server.serve()
