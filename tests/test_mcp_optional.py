"""An MCP server that fails to connect must cost its tools, not the gateway.

`Gateway.serve()` enters the agent context once, which connects every MCP
server; a server that exits on startup (the weather example under mcp 2.x did
exactly that) used to raise out of `serve()` and take every channel down. Here
one server dies on startup next to one that works: entering the agent must
succeed, a turn must run, and only the working server's tools may be offered.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from selmakit import Agent
from selmakit.capabilities import McpCapability
from selmakit.config import McpServerConfig

_GOOD_SERVER = """
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("good")


@mcp.tool()
def ping() -> str:
    return "pong"


mcp.run()
"""


def test_failed_server_is_skipped(tmp_path: Path) -> None:
    good = tmp_path / "good_mcp.py"
    good.write_text(_GOOD_SERVER)
    servers = {
        "broken": McpServerConfig(command=sys.executable, args=["-c", "raise SystemExit(1)"]),
        "good": McpServerConfig(command=sys.executable, args=[str(good)], prefix="good"),
    }
    seen: list[set[str]] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append({t.name for t in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        model=FunctionModel(respond),
        capabilities=[McpCapability(servers)],
        state_dir=tmp_path,
    )

    async def main() -> str:
        async with agent:
            result = await agent._agent.run("hi", deps="s")
            return result.output

    assert asyncio.run(main()) == "ok"
    assert seen and "good_ping" in seen[0]
    assert not any(name.startswith("broken") for name in seen[0])
