"""build_model() provider dispatch for local OpenAI-compatible servers (issue #3).

The round-trip test fakes a llama.cpp server with an httpx2 MockTransport — no
network, no running server.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pydantic_ai.providers.openai as openai_providers
from pydantic_ai import Agent as PydanticAgent

from selmakit.config import ModelConfig, build_model

LLAMACPP_URL = "http://localhost:9931/v1"


def test_local_uses_base_url_without_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    model = build_model(ModelConfig(model="local/gemma4", base_url=LLAMACPP_URL))

    assert model.model_name == "gemma4"
    assert model.base_url.rstrip("/") == LLAMACPP_URL
    assert model.profile["openai_chat_thinking_field"] == "reasoning_content"
    assert model.profile["openai_chat_send_back_thinking_parts"] == "field"


def test_openai_ignores_base_url(monkeypatch):
    # The design choice behind `local/`: base_url is shared, and init writes the
    # Ollama default into every config, so `openai/` must not pick it up.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    model = build_model(ModelConfig(model="openai/gpt-5", base_url=LLAMACPP_URL))

    assert "api.openai.com" in model.base_url


def _chat_response(message: dict, finish_reason: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gemma4",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_local_sends_thinking_back_in_reasoning_content(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            # llama.cpp puts the thinking in `reasoning_content`.
            return httpx2.Response(200, json=_chat_response({
                "role": "assistant",
                "content": None,
                "reasoning_content": "I should ping first.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "ping", "arguments": "{}"},
                }],
            }, "tool_calls"))
        return httpx2.Response(200, json=_chat_response({"role": "assistant", "content": "done"}, "stop"))

    real_provider = openai_providers.OpenAIProvider
    monkeypatch.setattr(
        openai_providers,
        "OpenAIProvider",
        lambda **kw: real_provider(**kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))),
    )
    model = build_model(ModelConfig(model="local/gemma4", base_url=LLAMACPP_URL))

    agent = PydanticAgent(model)

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    result = asyncio.run(agent.run("ping it"))

    assert result.output == "done"
    assert len(requests) == 2
    assistant = next(m for m in requests[1]["messages"] if m["role"] == "assistant")
    # Sent back in the field it came from — not as <think> text in the content,
    # which is what made Gemma bury its tool calls in the reasoning.
    assert assistant.get("reasoning_content") == "I should ping first."
    assert "<think>" not in (assistant.get("content") or "")
