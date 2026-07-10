from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic_ai import Agent as _PydanticAgent

from selmakit.commands import CommandContext, RunPrompt, SessionProxy
from selmakit.schedule import ScheduleConfig, ScheduleContext, ScheduleRunner
from selmakit.session import JsonlStore

logger = logging.getLogger(__name__)

_MAX_MESSAGES_BEFORE_COMPACT = 50


class _CommandResult:
    """Minimal stream-result adapter for slash-command responses."""
    is_complete = True

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream_text(self, *, delta: bool = False):
        yield self._text

    def all_messages(self) -> list:
        return []


def _format_history_for_compaction(messages: list) -> str:
    lines: list[str] = []
    for m in messages:
        role = "User" if "Request" in type(m).__name__ else "Assistant"
        parts_text: list[str] = []
        for part in getattr(m, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str) and content.strip():
                parts_text.append(content)
        if parts_text:
            lines.append(f"{role}: {''.join(parts_text)}")
    return "\n\n".join(lines)


class Agent:
    """
    selmakit Agent — wraps pydantic-ai Agent and adds selmakit features.

    state_dir is the root directory for all selmakit state:
      <state_dir>/sessions/   — session files (JsonlStore)
      <state_dir>/workspace/  — workspace files loaded into system prompt
      <state_dir>/memory/     — memory index
    """

    def __init__(
        self,
        model: Any = None,
        *,
        system_prompt: str | None = None,
        tools: Sequence[Any] = (),
        capabilities: Sequence[Any] = (),
        commands: dict[str, Callable] | None = None,
        state_dir: str = ".selmakit",
        session_store: JsonlStore | None = None,
        memory: Any = None,
        heartbeat: ScheduleConfig | None = None,
        model_config: Any = None,
    ):
        self._state_dir = Path(state_dir)
        # Base model config, used to build per-run override models (live /model
        # switching). None → no override support; runs always use `model`.
        self._model_config = model_config
        self._override_models: dict[tuple, Any] = {}
        self._workspace_dir = self._state_dir / "workspace"
        self._commands: dict[str, Callable] = {k.lower(): v for k, v in (commands or {}).items()}
        self._memory = memory
        self._schedule_runners: list[ScheduleRunner] = []
        self._alerts: asyncio.Queue = asyncio.Queue()

        if heartbeat is not None:
            from selmakit.schedule import build_heartbeat_prompt

            async def _heartbeat_handler(ctx: ScheduleContext) -> str:
                return build_heartbeat_prompt(ctx.workspace_dir)

            self._schedule_runners.append(
                ScheduleRunner(handler=_heartbeat_handler, cfg=heartbeat, agent=self, alerts=self._alerts)
            )

        all_tools = list(tools)

        all_caps = list(capabilities)
        if memory is not None:
            all_caps.append(memory)

        self._agent = _PydanticAgent(
            model=model,
            deps_type=str,
            system_prompt=system_prompt or (),
            tools=all_tools,
            capabilities=all_caps,
        )
        self._session_store = session_store or JsonlStore(
            path=str(self._state_dir / "sessions"),
        )

    @classmethod
    def from_file(
        cls,
        state_dir: str = ".selmakit",
        config_name: str = "selmakit.json",
        **kwargs,
    ) -> "Agent":
        """Create an Agent from selmakit.json — reads and distributes config internally."""
        from selmakit.config import build_model, load_config
        from selmakit.memory import SqliteMemory

        config = load_config(state_dir, config_name)
        cfg = config.model
        model = build_model(cfg)

        memory = None
        if config.memory.enabled:
            memory = SqliteMemory(
                workspace_dir=str(Path(state_dir) / "workspace"),
                vector_search=config.memory.vector_search,
                embed_model=config.memory.embed_model,
                embed_base_url=cfg.effective_base_url,
                temporal_decay=config.memory.temporal_decay,
                temporal_decay_rate=config.memory.temporal_decay_rate,
            )

        session_store = JsonlStore(
            path=str(Path(state_dir) / "sessions"),
            at_hour=config.session.reset.at_hour,
            idle_minutes=config.session.reset.idle_minutes,
        )

        return cls(
            model=model,
            state_dir=state_dir,
            session_store=session_store,
            memory=memory,
            model_config=cfg,
            **kwargs,
        )

    # --------------------------------------------------------------- properties

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    @property
    def alerts(self) -> asyncio.Queue:
        return self._alerts

    def message_count(self, session_key: str) -> int:
        return len(self._session_store.load(session_key))

    def last_system_prompt(self, session_key: str = "default") -> str | None:
        """The system prompt as last actually sent to the model for this session.

        The rendered instructions are stripped from the persisted message history
        to save space, so they are cached separately in the session metadata after
        each turn. Returns None before the first LLM turn of the session.
        """
        if not self._session_store:
            return None
        return self._session_store.get_meta(session_key, "last_system_prompt")

    @staticmethod
    def _extract_instructions(messages: list) -> str | None:
        """Pull the rendered instructions string from the latest request that has one."""
        for m in reversed(messages):
            instr = getattr(m, "instructions", None)
            if instr:
                return instr
        return None

    # ------------------------------------------------------------------ tools

    def get_tools(self) -> dict[str, str | None]:
        """Return all function tools as {name: description}, recursively across
        directly-registered tools and capability-provided toolsets."""
        result: dict[str, str | None] = {}

        def walk(ts: Any) -> None:
            tools = getattr(ts, "tools", None)
            if isinstance(tools, dict):
                for name, tool in tools.items():
                    result[name] = getattr(tool, "description", None)
            wrapped = getattr(ts, "wrapped", None)
            if wrapped is not None:
                walk(wrapped)
            inner = getattr(ts, "toolsets", None)
            if isinstance(inner, (list, tuple)):
                for x in inner:
                    walk(x)

        for ts in self._agent.toolsets:
            walk(ts)
        return result

    def get_schedules(self) -> list[dict]:
        """Return [{every, next_run_at}, ...] for all registered schedule runners."""
        return [
            {
                "every": r._cfg.every,
                "next_run_at": r.next_run_at,
            }
            for r in self._schedule_runners
        ]

    def messages_until_compaction(self, session_key: str) -> int:
        """Return how many more messages until auto-compaction triggers."""
        count = self.message_count(session_key)
        return max(0, _MAX_MESSAGES_BEFORE_COMPACT - count)

    def get_commands(self) -> dict[str, str | None]:
        """Return registered commands as {name: description}."""
        return {
            name: (fn.__doc__.strip().splitlines()[0] if fn.__doc__ else None)
            for name, fn in self._commands.items()
        }

    # -------------------------------------------------------- slash commands

    def command(self, path: str) -> Callable:
        """Decorator that registers a slash-command handler."""
        def decorator(fn: Callable) -> Callable:
            self._commands[path.lower()] = fn
            return fn
        return decorator

    async def _dispatch_command(self, text: str, session_key: str) -> str | RunPrompt | None:
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower()
        handler = self._commands.get(cmd)
        if handler is None:
            return None
        args = parts[1] if len(parts) > 1 else ""
        ctx = CommandContext(
            args=args,
            session_key=session_key,
            session=SessionProxy(
                sessions_dir=str(self._state_dir / "sessions"),
                session_key=session_key,
            ),
            agent=self,
        )
        return await handler(ctx)

    # ---------------------------------------------------------------- scheduling

    def schedule(
        self,
        *,
        every: str,
        active_hours: tuple[str, str] | None = None,
        timezone: str = "UTC",
        target: str = "last",
        isolated_session: bool = True,
        ack_max_chars: int = 300,
    ) -> Callable:
        """Decorator that registers a scheduled handler."""
        cfg = ScheduleConfig(
            every=every,
            active_hours=active_hours,
            timezone=timezone,
            target=target,
            isolated_session=isolated_session,
            ack_max_chars=ack_max_chars,
        )

        def decorator(fn: Callable) -> Callable:
            runner = ScheduleRunner(handler=fn, cfg=cfg, agent=self, alerts=self._alerts)
            self._schedule_runners.append(runner)
            return fn

        return decorator

    async def run_schedules(self) -> None:
        """Start all registered schedule runners."""
        tasks = [asyncio.create_task(r.run()) for r in self._schedule_runners]
        if not tasks:
            return
        await asyncio.gather(*tasks)

    def run(self) -> None:
        """Start all registered schedules."""
        asyncio.run(self.run_schedules())

    # ---------------------------------------- compaction + memory flush

    async def memory_flush(self, session_key: str) -> None:
        """Silent agent turn that saves important session context to memory before compaction."""
        if self._memory is None:
            return
        today = date.today().isoformat()
        prompt = (
            "[Memory Flush — silent turn]\n"
            f"Save important context from this session to memory/{today}.md "
            "using the memory_write tool. Include key decisions, facts, and todos. "
            "Skip trivial messages. If nothing important needs saving, call memory_write "
            "with a short note. Do not reply with anything visible to the user."
        )
        try:
            history = self._session_store.load(session_key)
            await self._agent.run(prompt, message_history=history, deps=session_key)
        except Exception as e:
            logger.warning("memory_flush failed | session=%s error=%s", session_key, e)

    async def compact_session(self, session_key: str) -> tuple[int, int]:
        """Summarize the session history and replace it with the summary.

        Returns (messages_before, messages_after).
        """
        if not self._session_store:
            return 0, 0
        from selmakit.schedule import filter_heartbeat_messages

        messages = self._session_store.load(session_key)
        messages = filter_heartbeat_messages(messages)
        before = len(messages)
        if before <= 4:
            return before, before

        history_text = _format_history_for_compaction(messages)
        compaction_prompt = (
            "Summarize the following conversation concisely as a context handoff. "
            "Preserve all important facts, decisions, open tasks, and user preferences. "
            "Write in the third person.\n\n"
            f"{history_text}"
        )
        try:
            result = await self._agent.run(compaction_prompt, message_history=[], deps=session_key)
            self._session_store.save(session_key, result.all_messages())
            after = len(result.all_messages())
            logger.info("compact_session | session=%s %d→%d messages", session_key, before, after)
            return before, after
        except Exception as e:
            logger.warning("compact_session failed | session=%s error=%s", session_key, e)
            return before, before

    # ----------------------------------------------------------------- stream

    def _current_model_config(self):
        """The model config as currently on disk (fresh read, cache-bypassing).

        Lets a live model/key change made in selmakit.json (e.g. via the
        dashboard selector) take effect on the next turn without a restart.
        Falls back to the base config captured at construction.
        """
        from selmakit.config import ModelConfig

        path = self._state_dir / "selmakit.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return ModelConfig(**data.get("model", {}))
            except Exception:
                pass
        return self._model_config or ModelConfig()

    def _resolve_run_model(self, session_key: str):
        """Build a per-run model when the session carries a live ``model_override``.

        Returns a pydantic-ai model to pass as ``run(model=…)``, or ``None`` to
        use the agent's default model. The override (set by ``/model`` or the
        dashboard selector) is a ``provider/model`` string; credentials and
        base_url are read fresh from selmakit.json so a just-saved key applies
        without a restart. Built models are cached per (model, key, base_url).
        """
        if not self._session_store:
            return None
        override = self._session_store.get_meta(session_key, "model_override")
        base = self._model_config
        if not override or (base is not None and override == base.model):
            return None

        cfg = self._current_model_config().model_copy(update={"model": override})
        cache_key = (override, cfg.api_key, cfg.effective_base_url)
        model = self._override_models.get(cache_key)
        if model is None:
            from selmakit.config import build_model
            model = build_model(cfg)
            self._override_models[cache_key] = model
        return model

    async def _prepare_run(self, prompt: str, session_key: str) -> tuple[str | None, str, dict]:
        """Shared pre-processing for all run methods.

        Returns (command_text, effective_prompt, kwargs).
        command_text is non-None when a slash command was handled — caller should
        return that text directly without calling the LLM.
        """
        if prompt.lower().startswith("/skill "):
            from selmakit.skills import get_skill_path
            parts = prompt[7:].strip().split(None, 1)
            skill_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            skill_path = get_skill_path(str(self._workspace_dir), skill_name)
            if skill_path is None:
                return f"Skill `{skill_name}` not found.", "", {}
            prompt = (
                f"Execute skill {skill_name}."
                if not args else
                f"Execute skill {skill_name}: {args}"
            )
        elif prompt.startswith("/"):
            result = await self._dispatch_command(prompt, session_key)
            if isinstance(result, RunPrompt):
                prompt = result.text          # rewrite-and-run like /skill
            elif result is not None:
                return result, "", {}         # plain text: short-circuit
            else:                             # unknown slash command
                cmd = prompt.strip().split(None, 1)[0]
                return (
                    f"Unknown command `{cmd}`. Type `/help` to see available commands.",
                    "",
                    {},
                )

        effective_prompt = prompt

        if self._session_store:
            if not self._session_store.is_fresh(session_key):
                logger.info("Session stale, resetting | session=%s", session_key)
                self._session_store.clear(session_key)

        history = self._session_store.load(session_key) if self._session_store else []

        if self._session_store and len(history) > _MAX_MESSAGES_BEFORE_COMPACT:
            logger.info("Pre-compacting long session | session=%s messages=%d", session_key, len(history))
            await self.memory_flush(session_key)
            await self.compact_session(session_key)
            history = self._session_store.load(session_key)

        kwargs: dict[str, Any] = {"message_history": history, "deps": session_key}

        override_model = self._resolve_run_model(session_key)
        if override_model is not None:
            kwargs["model"] = override_model

        return None, effective_prompt, kwargs

    @asynccontextmanager
    async def run_stream(
        self,
        prompt: str,
        *,
        session_key: str = "default",
        event_handler: Any = None,
        extra_capabilities: Sequence[Any] | None = None,
    ):
        cmd_text, effective_prompt, kwargs = await self._prepare_run(prompt, session_key)
        if cmd_text is not None:
            yield _CommandResult(cmd_text)
            return

        if event_handler is not None:
            kwargs["event_stream_handler"] = event_handler
        if extra_capabilities:
            kwargs["capabilities"] = list(extra_capabilities)

        async with self._agent.run_stream(effective_prompt, **kwargs) as result:
            yield result

        if self._session_store and result.is_complete:
            messages = result.all_messages()
            self._session_store.save(session_key, messages)
            self._session_store.touch(session_key)
            instr = self._extract_instructions(messages)
            if instr:
                self._session_store.set_meta(session_key, "last_system_prompt", instr)

    @asynccontextmanager
    async def run_stream_events(self, prompt: str, *, session_key: str = "default"):
        """Yields a (is_command, value) tuple:
          - (True,  text_str)     — slash command result
          - (False, async_gen)    — pydantic-ai AgentStreamEvents
        """
        from pydantic_ai import AgentRunResultEvent

        cmd_text, effective_prompt, kwargs = await self._prepare_run(prompt, session_key)
        if cmd_text is not None:
            yield True, cmd_text
            return

        final_result = None

        async def _event_gen():
            nonlocal final_result
            async with self._agent.run_stream_events(effective_prompt, **kwargs) as stream:
                async for event in stream:
                    if isinstance(event, AgentRunResultEvent):
                        final_result = event.result
                    else:
                        yield event

        yield False, _event_gen()

        if self._session_store and final_result is not None:
            messages = final_result.all_messages()
            self._session_store.save(session_key, messages)
            self._session_store.touch(session_key)
            instr = self._extract_instructions(messages)
            if instr:
                self._session_store.set_meta(session_key, "last_system_prompt", instr)
