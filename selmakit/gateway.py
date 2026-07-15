"""
selmakit/gateway.py

The runtime that wires an agent to its channels, worker, schedules and cron
service — extracted from the former top-level ``gateway.py`` script so that
custom agents can be built in a few lines:

    from selmakit import Gateway
    Gateway.from_config().run()

To add your own capabilities, pass instances via ``extra_capabilities``; they
are appended to the default set:

    Gateway.from_config(extra_capabilities=[MyCapability(...)]).run()

For full control, pass ``capabilities=`` as a list, or as a callable
``(GatewayContext) -> list`` when a capability needs one of the internal
objects (session store, cron store, …).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from pydantic_ai.capabilities import WebFetch, WebSearch

from selmakit.agent import Agent
from selmakit.capabilities import (
    BootstrapCapability,
    FilesystemCapability,
    McpCapability,
    RuntimeInfoCapability,
    SessionThinkingCapability,
    SkillsPromptCapability,
    WorkspacePromptCapability,
)
from selmakit.channels.telegram import TelegramChannel
from selmakit.channels.webchat import WebChatChannel
from selmakit.commands import make_commands
from selmakit.config import SelmaKitConfig, build_model, load_config
from selmakit.cron import CronCapability, CronService, CronStore
from selmakit.memory import SqliteMemory
from selmakit.message import QueueItem
from selmakit.schedule import ScheduleConfig
from selmakit.session import JsonlStore
from selmakit.tracing import setup as tracing_setup

logger = logging.getLogger(__name__)


@dataclass
class GatewayContext:
    """The internal objects a capability factory may need to reference.

    Passed to ``default_capabilities()`` and to a ``capabilities=`` callable so
    capabilities that depend on the session store, cron store, workspace dir,
    etc. can be wired up without the caller rebuilding them.
    """

    config: SelmaKitConfig
    model: Any                       # a pydantic-ai model (see selmakit.config.build_model)
    state_dir: str
    workspace_dir: str
    model_name: str                  # full "provider/model" string, e.g. "ollama/llama3.2"
    session_store: JsonlStore
    memory: SqliteMemory | None
    cron_store: CronStore


def default_capabilities(ctx: GatewayContext) -> list[Any]:
    """The standard selmakit capability set, wired from ``ctx``.

    Mirror of the list the old top-level ``gateway.py`` constructed inline.
    """
    caps = [
        FilesystemCapability(cwd="."),
        WebSearch(local="duckduckgo"),
        WebFetch(local=True),
        BootstrapCapability(workspace_dir=ctx.workspace_dir),
        WorkspacePromptCapability(workspace_dir=ctx.workspace_dir),
        SkillsPromptCapability(workspace_dir=ctx.workspace_dir),
        RuntimeInfoCapability(model_name=ctx.model_name),
        SessionThinkingCapability(
            session_store=ctx.session_store,
            default_thinking=ctx.config.model.thinking,
        ),
        CronCapability(store=ctx.cron_store),
    ]
    if ctx.config.mcp.enabled and ctx.config.mcp.servers:
        caps.append(McpCapability(servers=ctx.config.mcp.servers))
    if ctx.config.subagents.enabled and ctx.config.subagents.agents:
        caps.append(build_subagents_capability(ctx))
    return caps


def build_subagents_capability(ctx: GatewayContext) -> Any:
    """Build the harness ``SubAgents`` capability from the ``subagents`` config.

    Each configured sub-agent becomes an isolated pydantic-ai agent (its own
    model + system prompt, plus filesystem/web tools so it can actually do work);
    the parent delegates to it by name via a single ``delegate_task`` tool. The
    harness is imported lazily so it is only required when subagents are enabled.
    """
    try:
        from pydantic_ai_harness.subagents import SubAgent, SubAgents
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "subagents needs the 'pydantic-ai-harness' package. Install the extra: "
            "`uv sync --extra subagents` (or disable subagents in selmakit.json)."
        ) from e

    from pydantic_ai import Agent as PydanticAgent
    from pydantic_ai.capabilities import WebFetch, WebSearch

    from selmakit.config import build_model

    def _worker_caps() -> list[Any]:
        # Fresh instances per sub-agent — the tools that make a delegate useful.
        return [
            FilesystemCapability(cwd="."),
            WebSearch(local="duckduckgo"),
            WebFetch(local=True),
        ]

    entries = []
    for sa in ctx.config.subagents.agents:
        model = build_model(ctx.config.model.model_copy(update={"model": sa.model})) if sa.model else ctx.model
        pai = PydanticAgent(
            model,
            deps_type=str,
            system_prompt=sa.system_prompt or (),
            capabilities=_worker_caps(),
        )
        entries.append(SubAgent(
            agent=pai,
            name=sa.name,
            description=sa.description,
            timeout_seconds=sa.timeout_seconds,
            max_calls=sa.max_calls,
        ))
    return SubAgents(agents=entries, agent_folders=None)


class Gateway:
    """Wires an :class:`~selmakit.Agent` to its channels, worker, schedules and
    cron service, and runs them all under a single ``asyncio.gather``.
    """

    def __init__(
        self,
        *,
        config: SelmaKitConfig,
        model: Any,
        state_dir: str,
        session_store: JsonlStore,
        memory: SqliteMemory | None,
        cron_store: CronStore,
        capabilities: Sequence[Any] | Callable[[GatewayContext], Sequence[Any]] | None = None,
        extra_capabilities: Sequence[Any] = (),
        tools: Sequence[Any] = (),
        commands: dict[str, Callable] | None = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.workspace_dir = f"{state_dir}/workspace"
        self.cron_store = cron_store

        self.context = GatewayContext(
            config=config,
            model=model,
            state_dir=state_dir,
            workspace_dir=self.workspace_dir,
            model_name=config.model.model,
            session_store=session_store,
            memory=memory,
            cron_store=cron_store,
        )

        caps = self._resolve_capabilities(capabilities, extra_capabilities)

        hb = config.heartbeat
        self.agent = Agent(
            model=model,
            state_dir=state_dir,
            session_store=session_store,
            memory=memory,
            model_config=config.model,
            capabilities=caps,
            tools=tools,
            commands=commands if commands is not None else make_commands(config, cron_store=cron_store),
            heartbeat=ScheduleConfig(
                every=hb.every,
                active_hours=hb.active_hours,
                timezone=hb.timezone,
                target=hb.target,
                isolated_session=hb.isolated_session,
            ) if hb.enabled else None,
        )

        self.cron_service = CronService(store=cron_store, agent=self.agent)
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self.channels = self._build_channels()

    # ----------------------------------------------------------------- build

    def _resolve_capabilities(
        self,
        capabilities: Sequence[Any] | Callable[[GatewayContext], Sequence[Any]] | None,
        extra_capabilities: Sequence[Any],
    ) -> list[Any]:
        if capabilities is None:
            caps = default_capabilities(self.context)
        elif callable(capabilities):
            caps = list(capabilities(self.context))
        else:
            caps = list(capabilities)
        return [*caps, *extra_capabilities]

    def _build_channels(self) -> list[Any]:
        """Build the enabled channels. Each channel is opt-in via config; Telegram
        additionally requires ``TELEGRAM_TOKEN`` in the environment."""
        ch = self.config.channels
        channels: list[Any] = []

        if ch.webchat.enabled:
            channels.append(WebChatChannel(
                queue=self.queue,
                alerts=self.agent.alerts,
                host=ch.webchat.host,
                port=ch.webchat.port,
                timeout_seconds=self.config.model.timeout_seconds,
                log_level=ch.webchat.log_level,
            ))
        else:
            logger.info("WebChat channel disabled (channels.webchat.enabled=false)")

        if ch.telegram.enabled:
            token = os.environ.get("TELEGRAM_TOKEN")
            if token:
                channels.append(TelegramChannel(token=token, queue=self.queue))
            else:
                logger.warning("Telegram channel enabled but TELEGRAM_TOKEN not set — skipping")
        else:
            logger.info("Telegram channel disabled (channels.telegram.enabled=false)")

        if not channels:
            logger.warning("No channels enabled — gateway will run schedules/cron only")
        return channels

    @classmethod
    def from_config(
        cls,
        state_dir: str = ".selmakit",
        config_name: str = "selmakit.json",
        *,
        capabilities: Sequence[Any] | Callable[[GatewayContext], Sequence[Any]] | None = None,
        extra_capabilities: Sequence[Any] = (),
        tools: Sequence[Any] = (),
        commands: dict[str, Callable] | None = None,
    ) -> "Gateway":
        """Build a Gateway from ``selmakit.json`` — reads and distributes config."""
        config = load_config(state_dir, config_name)
        cfg = config.model
        model = build_model(cfg)

        session_store = JsonlStore(
            path=f"{state_dir}/sessions",
            at_hour=config.session.reset.at_hour,
            idle_minutes=config.session.reset.idle_minutes,
        )

        memory = None
        if config.memory.enabled:
            memory = SqliteMemory(
                workspace_dir=f"{state_dir}/workspace",
                vector_search=config.memory.vector_search,
                embed_model=config.memory.embed_model,
                embed_base_url=cfg.base_url,
                temporal_decay=config.memory.temporal_decay,
                temporal_decay_rate=config.memory.temporal_decay_rate,
            )

        cron_store = CronStore(path=f"{state_dir}/cron/jobs.json")

        return cls(
            config=config,
            model=model,
            state_dir=state_dir,
            session_store=session_store,
            memory=memory,
            cron_store=cron_store,
            capabilities=capabilities,
            extra_capabilities=extra_capabilities,
            tools=tools,
            commands=commands,
        )

    # ---------------------------------------------------------------- worker

    async def _worker(self) -> None:
        from pydantic_ai.messages import (
            FunctionToolCallEvent, FunctionToolResultEvent, PartDeltaEvent, PartStartEvent,
            TextPart, TextPartDelta, ThinkingPart, ThinkingPartDelta,
        )
        while True:
            item: QueueItem = await self.queue.get()
            verbose = bool(self.context.session_store.get_meta(item.session_key, "verbose", False))
            call_started: dict[str, float] = {}  # tool_call_id -> monotonic start time
            try:
                async with self.agent.run_stream_events(item.prompt, session_key=item.session_key) as (is_cmd, value):
                    if is_cmd:
                        await item.reply.send_chunk(value)
                    else:
                        async for event in value:
                            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                # pydantic-ai 2.0 may deliver the first token(s) here
                                if event.part.content:
                                    await item.reply.send_chunk(event.part.content)
                            elif isinstance(event, FunctionToolCallEvent):
                                await self._forward_tool_call(item.reply, event.part, verbose, call_started)
                            elif isinstance(event, FunctionToolResultEvent):
                                if verbose:
                                    await self._forward_tool_result(item.reply, event.part, call_started)
                            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                                if event.delta.content_delta:
                                    await item.reply.send_chunk(event.delta.content_delta)
                            elif verbose and isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
                                if event.part.content:
                                    await item.reply.send_thinking(event.part.content)
                            elif verbose and isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
                                if event.delta.content_delta:
                                    await item.reply.send_thinking(event.delta.content_delta)
                # If the turn ended awaiting approval for gated (MCP) tool calls,
                # surface them so the user can /approve or /deny (buttons in the UI).
                pending = self.context.session_store.get_meta(item.session_key, "pending_approvals", None)
                if pending:
                    await item.reply.send_approval(pending)
                await item.reply.done()
            except Exception as e:
                await item.reply.send_error(e)
            finally:
                self.queue.task_done()

    # Longest tool result forwarded to the webchat verbose log; longer is truncated.
    _VERBOSE_RESULT_LIMIT = 800

    async def _forward_tool_call(self, reply, part, verbose: bool, call_started: dict) -> None:
        """Forward a tool call to the reply. In verbose mode include the args
        (→ name(args)) and record the start time for duration tracking."""
        if verbose:
            call_started[part.tool_call_id] = asyncio.get_running_loop().time()
            try:
                args = part.args_as_json_str()
            except Exception:
                args = str(part.args)
            await reply.send_tool(part.tool_name, args=args)
        else:
            await reply.send_tool(part.tool_name)

    async def _forward_tool_result(self, reply, part, call_started: dict) -> None:
        """Forward a tool result (← name: …) with duration and error flag."""
        started = call_started.pop(part.tool_call_id, None)
        duration = asyncio.get_running_loop().time() - started if started is not None else None
        is_error = getattr(part, "part_kind", None) == "retry-prompt"
        content = part.model_response() if is_error else part.content
        result = content if isinstance(content, str) else str(content)
        if len(result) > self._VERBOSE_RESULT_LIMIT:
            result = result[: self._VERBOSE_RESULT_LIMIT] + f"… ({len(result)} chars)"
        await reply.send_tool_result(part.tool_name, result, duration=duration, error=is_error)

    # ------------------------------------------------------------------- run

    async def serve(self) -> None:
        """Start tracing, logging, channels, worker, schedules and cron."""
        tracing_setup(endpoint="http://localhost:4317")
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.INFO,
        )
        # Enter the agent context once so MCP toolsets connect a single time and
        # stay open for the gateway's lifetime, instead of reconnecting per run.
        async with self.agent:
            await asyncio.gather(
                *[channel.start() for channel in self.channels],
                self._worker(),
                self.agent.run_schedules(),
                self.cron_service.run(),
            )

    def run(self) -> None:
        """Blocking entry point — runs the gateway until interrupted."""
        try:
            asyncio.run(self.serve())
        except KeyboardInterrupt:
            print("\nGateway shutting down...")
