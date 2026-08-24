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
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, cast

from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.common_tools.web_fetch import web_fetch_tool
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.skills import Skills

from selmakit.agent import Agent
from selmakit.capabilities import (
    BootstrapCapability,
    McpCapability,
    RuntimeInfoCapability,
    SessionThinkingCapability,
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


def local_web_fetch() -> WebFetch:
    """``WebFetch`` whose local fallback may reach private/loopback addresses.

    pydantic-ai's local `web_fetch` refuses private and loopback IPs by default
    (SSRF protection). selmakit's own services live exactly there — Ollama on
    :11434, the gateway on :8000 — so skills that health-check them need the
    guard lifted. Keep this in mind for any deployment where the agent handles
    untrusted input: it can then reach services on the host and LAN.

    The fetcher is also wrapped so an unexpected exception degrades to a
    ``ModelRetry`` instead of aborting the whole turn. It already raises
    ``ModelRetry`` for HTTP and connection failures, but conversion errors escape:
    ``markdownify`` recurses once per DOM node, so a deeply nested page (the W3C
    WebGPU spec is one) raises ``RecursionError``, which propagates out of the
    tool, out of the run, and reaches the user as a bare "maximum recursion depth
    exceeded". One unreadable URL in a research batch should skip like a 404 does.
    """
    tool = web_fetch_tool(allow_local_urls=True)
    # `Tool.function` is a union that also covers the `(ctx, …)` and sync tool
    # shapes; this one is `async (url: str)`, so narrow it for the call below.
    fetch = cast(Callable[..., Awaitable[Any]], tool.function)

    async def guarded_web_fetch(url: str) -> Any:
        try:
            return await fetch(url=url)
        except ModelRetry:
            # The fetcher's own "skip this URL" signal — already the shape we want.
            raise
        except Exception as e:
            logger.warning("web_fetch failed | url=%s | %s: %s", url, type(e).__name__, e)
            raise ModelRetry(f"Failed to fetch {url}: {type(e).__name__}: {e}") from e

    return WebFetch(
        local=Tool(guarded_web_fetch, name=tool.name, description=tool.description)
    )


def build_skills_capability(workspace_dir: str) -> Any | None:
    """The harness ``Skills`` capability over ``<workspace>/skills/``, or None.

    Each ``<skill>/SKILL.md`` becomes a *deferred* capability: only its name and
    description sit in the prompt, and the model pulls the body in on demand via
    the ``load_capability`` tool. ``Skills`` scans at construction and raises on
    a missing directory, so an agent whose workspace has no skills folder gets
    no capability at all rather than a crash.
    """
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.is_dir() or not any(skills_dir.glob("*/SKILL.md")):
        return None
    return Skills(skills_dir)


def default_capabilities(ctx: GatewayContext) -> list[Any]:
    """The standard selmakit capability set, wired from ``ctx``.

    Mirror of the list the old top-level ``gateway.py`` constructed inline.
    """
    caps = [
        # Sandboxed to the state directory: absolute paths, `~` and `../`
        # escapes are rejected, symlinks resolved before authorization.
        # Rooted at `.selmakit` rather than the project because the harness
        # walkers (list_directory/search_files/find_files) skip every path with
        # a dot-prefixed component — from the project root the whole state
        # directory would silently list as empty.
        FileSystem(root_dir=ctx.state_dir),
        WebSearch(local="duckduckgo"),
        local_web_fetch(),
        BootstrapCapability(workspace_dir=ctx.workspace_dir),
        WorkspacePromptCapability(workspace_dir=ctx.workspace_dir),
        RuntimeInfoCapability(model_name=ctx.model_name),
        SessionThinkingCapability(
            session_store=ctx.session_store,
            default_thinking=ctx.config.model.thinking,
        ),
        CronCapability(store=ctx.cron_store),
    ]
    skills = build_skills_capability(ctx.workspace_dir)
    if skills is not None:
        caps.append(skills)
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
    harness is a core dependency (the default capability set already uses its
    FileSystem and Skills), so this import cannot fail on a supported install.

    When ``subagents.models`` configures a menu, ``delegate_task`` also takes a
    ``model`` argument (an enum of the menu keys) so the parent routes each task
    to the model that fits it; a sub-agent's own ``models`` list restricts which
    keys it accepts. With no menu the tool keeps exactly the shape it had before.
    """
    from pydantic_ai_harness.subagents import ModelOption, SubAgent, SubAgents

    from pydantic_ai import Agent as PydanticAgent
    from pydantic_ai.capabilities import WebSearch
    from pydantic_ai.settings import ModelSettings

    from selmakit.config import build_model

    def _worker_caps() -> list[Any]:
        # Fresh instances per sub-agent — the tools that make a delegate useful,
        # under the same state-directory sandbox as the parent.
        return [
            FileSystem(root_dir=ctx.state_dir),
            WebSearch(local="duckduckgo"),
            local_web_fetch(),
        ]

    built: dict[str, Any] = {}

    def _model_for(name: str) -> Any:
        # A "provider/model" string goes through build_model so it inherits the
        # main model's credentials/base_url; identical strings share one instance.
        if name not in built:
            built[name] = build_model(ctx.config.model.model_copy(update={"model": name}))
        return built[name]

    def _menu_settings(thinking: str | None) -> Any:
        # `thinking` is a free string in the config (as everywhere else in
        # selmakit); pydantic-ai narrows it to a Literal at the type level only.
        return ModelSettings(thinking=cast(Any, thinking)) if thinking and thinking != "off" else None

    menu = {
        key: ModelOption(
            model=_model_for(opt.model),
            description=opt.description or None,
            settings=_menu_settings(opt.thinking),
        )
        for key, opt in ctx.config.subagents.models.items()
    }

    entries = []
    for sa in ctx.config.subagents.agents:
        model = _model_for(sa.model) if sa.model else ctx.model
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
            models=sa.models,
        ))
    return SubAgents(agents=entries, models=menu, agent_folders=None)


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
                channels.append(TelegramChannel(
                    token=token,
                    queue=self.queue,
                    allowed_chat_ids=ch.telegram.allowed_chat_ids,
                    # The file tools are sandboxed to the state dir, so that is
                    # exactly the set of files the agent can have produced —
                    # and the only set it may attach.
                    attach_root=self.context.state_dir if ch.telegram.attach_files else None,
                    show_tools=ch.telegram.show_tools,
                ))
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
        # Opt-in: with no collector running, an always-on exporter retries every
        # refused connection and logs an error per turn.
        if self.config.tracing.enabled:
            tracing_setup(
                self.config.tracing.project_name,
                self.config.tracing.endpoint,
                capture_http=self.config.tracing.capture_http,
            )
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
