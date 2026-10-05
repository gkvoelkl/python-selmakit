"""End-to-end turns through the *default* capability set, harness included.

Every other test builds an `Agent` with a hand-picked capability list, so none
of them notices when the harness capabilities stop working in the composition
the gateway actually ships. That is not hypothetical: harness 0.52 moved
`FileSystem`, `Skills` and `SubAgents` onto the run's workspace, and on the
upgrade mypy, ruff and every test here stayed green while the first real turn
died with "`Skills` needs a workspace". These tests run that first real turn.

Built through `Gateway.from_config` on a state dir made by `selmakit init`, so
the config, the workspace files and the capability wiring are the real ones;
only the model is replaced. It is a `FunctionModel` that works through a fixed
list of tool calls and then answers — `TestModel` is no good here, it calls no
tool of its own accord and rejects the native web tools outright. Outcomes are
read back from the persisted session, i.e. what the tools really returned.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import selmakit.config
import selmakit.gateway
from selmakit import load_session_messages
from selmakit.gateway import Gateway
from selmakit.init import init

SUB_AGENT = "scout"


def _scripted_model(plan: list[tuple[str, dict[str, Any]]]) -> FunctionModel:
    """Parent: make the calls in `plan`, one per request, then answer.
    Sub-agent (recognised by having no `delegate_task`): list `workspace`, then
    answer with what it saw — so the delegation result proves its tool ran."""

    def respond(messages, info: AgentInfo) -> ModelResponse:
        is_sub_agent = all(t.name != "delegate_task" for t in info.function_tools)
        # Tool results of this run only: everything after the last user prompt.
        start = max(i for i, m in enumerate(messages)
                    if any(isinstance(p, UserPromptPart) for p in m.parts))
        results = [p for m in messages[start:] for p in m.parts
                   if isinstance(p, (ToolReturnPart, RetryPromptPart))]
        steps = [("list_directory", {"path": "workspace"})] if is_sub_agent else plan
        if len(results) < len(steps):
            name, args = steps[len(results)]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        seen = str(results[-1].content) if is_sub_agent and results else ""
        return ModelResponse(parts=[TextPart("done " + seen)])

    async def stream(messages, info: AgentInfo):
        part = respond(messages, info).parts[0]
        if isinstance(part, TextPart):
            yield part.content
        else:
            yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}

    return FunctionModel(respond, stream_function=stream)


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)          # init() drops .env.example into the cwd
    state = tmp_path / "state"
    init(str(state))
    skill = state / "workspace" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: A demo skill.\n---\nSay hi.\n")
    config = json.loads((state / "selmakit.json").read_text())
    config["subagents"]["enabled"] = True
    config["subagents"]["agents"] = [{"name": SUB_AGENT, "description": "Looks around.",
                                      "system_prompt": "You look around."}]
    (state / "selmakit.json").write_text(json.dumps(config))
    return state


def _gateway(state: Path, plan: list[tuple[str, dict[str, Any]]], monkeypatch) -> Gateway:
    model = _scripted_model(plan)
    # Both names: from_config uses the gateway module's import, the sub-agent
    # builder imports from selmakit.config at call time.
    monkeypatch.setattr(selmakit.gateway, "build_model", lambda cfg: model)
    monkeypatch.setattr(selmakit.config, "build_model", lambda cfg: model)
    return Gateway.from_config(str(state))


def _turn(gw: Gateway, prompt: str = "go", key: str = "e2e") -> None:
    async def go():
        async with gw.agent.run_stream_events(prompt, session_key=key) as (is_command, events):
            assert not is_command
            async for _ in events:
                pass

    asyncio.run(go())


def _tool_outcomes(state: Path, key: str = "e2e") -> list[tuple[str, str, str]]:
    """(part_kind, tool_name, content) for every tool return / retry prompt."""
    return [
        (part["part_kind"], part.get("tool_name") or "", str(part["content"]))
        for message in load_session_messages(state / "sessions", key)
        for part in message["parts"]
        if part["part_kind"] in ("tool-return", "retry-prompt")
    ]


def test_file_tools_work_through_the_workspace(state_dir, monkeypatch):
    gw = _gateway(state_dir, [("list_directory", {"path": "."}),
                              ("read_file", {"path": "workspace/SOUL.md"})], monkeypatch)
    _turn(gw)
    outcomes = _tool_outcomes(state_dir)
    assert [kind for kind, _, _ in outcomes] == ["tool-return", "tool-return"]
    listing, soul = outcomes[0][2], outcomes[1][2]
    # Paths are relative to the state dir: the workspace's working directory.
    assert "selmakit.json" in listing and "workspace/" in listing
    assert "SOUL.md" in soul


def test_file_tools_stay_inside_the_state_dir(state_dir, monkeypatch):
    secret = state_dir.parent / "secret.txt"
    secret.write_text("TOP-SECRET")
    gw = _gateway(state_dir, [("read_file", {"path": "../secret.txt"}),
                              ("read_file", {"path": str(secret)})], monkeypatch)
    _turn(gw)
    outcomes = _tool_outcomes(state_dir)
    assert [kind for kind, _, _ in outcomes] == ["retry-prompt", "retry-prompt"]
    assert all("TOP-SECRET" not in content for _, _, content in outcomes)


def test_skill_loads_and_a_new_one_is_live_without_restart(state_dir, monkeypatch):
    gw = _gateway(state_dir, [("load_capability", {"id": "demo"})], monkeypatch)
    _turn(gw)
    assert _tool_outcomes(state_dir)[-1][:2] == ("tool-return", "load_capability")
    assert "Say hi." in _tool_outcomes(state_dir)[-1][2]

    late = state_dir / "workspace" / "skills" / "late"
    late.mkdir()
    (late / "SKILL.md").write_text("---\nname: late\ndescription: Added while running.\n---\nbody\n")
    _turn(gw)                            # same gateway, no restart
    assert "late" in (gw.agent.last_system_prompt("e2e") or "")


def test_delegate_runs_in_the_parent_workspace(state_dir, monkeypatch):
    gw = _gateway(state_dir, [("delegate_task", {"agent_name": SUB_AGENT,
                                                 "task": "List the workspace."})], monkeypatch)
    _turn(gw)
    kind, tool, content = _tool_outcomes(state_dir)[-1]
    assert (kind, tool) == ("tool-return", "delegate_task")
    # The sub-agent's own list_directory reached the parent's state dir.
    assert "SOUL.md" in content


def test_missing_skills_dir_does_not_fail_turns(state_dir, monkeypatch):
    # Harness Skills raises on a missing directory at the start of every run,
    # so build_skills_capability must leave the capability out entirely.
    shutil.rmtree(state_dir / "workspace" / "skills")
    gw = _gateway(state_dir, [("list_directory", {"path": "."})], monkeypatch)
    _turn(gw)
    assert _tool_outcomes(state_dir)[-1][0] == "tool-return"


def test_relative_state_dir_with_skills(state_dir, monkeypatch):
    # The default `state_dir=".selmakit"` is relative (#2). selmakit's own
    # existence check resolves the skills path against the process cwd, the
    # harness against the workspace's working directory (= the state dir) —
    # a relative path handed over as-is means two different directories, and
    # every run failed at its start. The fixture has chdir'ed to the parent.
    assert Path.cwd() == state_dir.parent
    gw = _gateway(Path(state_dir.name), [("load_capability", {"id": "demo"})], monkeypatch)
    _turn(gw)
    kind, tool, content = _tool_outcomes(state_dir)[-1]
    assert (kind, tool) == ("tool-return", "load_capability")
    assert "Say hi." in content
