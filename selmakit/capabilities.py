"""
selmakit/capabilities.py

Prompt-shaped capabilities that contribute fragments to the agent's
instructions. Each is evaluated dynamically per run, so changes on disk
(workspace files, skills) are picked up without restart.
"""
from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic_ai import ModelSettings, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from selmakit.session import JsonlStore
from selmakit.skills import build_skills_xml
from selmakit.tools import make_filesystem_tools
from selmakit.workspace import detect_bootstrap, load_workspace_files

logger = logging.getLogger(__name__)


_BOOTSTRAP_INSTRUCTIONS = "\n".join([
    "## Bootstrap (pending)",
    "`BOOTSTRAP.md` is present in the workspace. Before responding to the user "
    "normally, read BOOTSTRAP.md and follow its instructions.",
    "Your first user-visible reply for a bootstrap-pending workspace must follow "
    "BOOTSTRAP.md, not a generic greeting.",
])


_SKILLS_USAGE = (
    "Before replying: scan the `<description>` entries in `<available_skills>`.\n"
    "- If exactly one skill clearly applies: use `read` to load its SKILL.md at "
    "`<location>`, then follow it.\n"
    "- If multiple could apply: choose the most specific one, then read and follow it.\n"
    "- If none clearly apply: do not read any SKILL.md.\n"
    "Never read more than one skill per turn. Only read after selecting.\n"
    "When a skill references a relative path, resolve it against the skill's "
    "directory (parent of SKILL.md)."
)


@dataclass
class BootstrapCapability(AbstractCapability[Any]):
    """Inject a bootstrap-pending hint into instructions while
    ``BOOTSTRAP.md`` exists in the workspace.

    Once the bootstrap process removes BOOTSTRAP.md (typically as the LLM's
    first action), the hint disappears on the next turn — no restart needed.
    """

    workspace_dir: str

    def get_instructions(self):
        wd = self.workspace_dir

        def _instructions(ctx: RunContext[Any]) -> str:
            if detect_bootstrap(wd):
                return _BOOTSTRAP_INSTRUCTIONS
            return ""

        return _instructions


@dataclass
class FilesystemCapability(AbstractCapability[Any]):
    """Provides file-system tools (read, write, edit, ls, grep, find) bound
    to a working directory.

    All path arguments are resolved relative to ``cwd``. Tools are built
    once at construction; restart the agent if you change ``cwd``.
    """

    cwd: str = "."

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=list(make_filesystem_tools(self.cwd)))


@dataclass
class WorkspacePromptCapability(AbstractCapability[Any]):
    """Inject workspace markdown files (SOUL.md, IDENTITY.md, USER.md, …) into
    the agent's instructions as a ``## Workspace Files`` section.

    Files are re-read each run, so edits on disk apply on the next turn.
    """

    workspace_dir: str

    def get_instructions(self):
        wd = self.workspace_dir

        def _instructions(ctx: RunContext[Any]) -> str:
            files = load_workspace_files(wd)
            if not files:
                return ""
            parts = ["## Workspace Files (injected)"]
            for f in files:
                parts += ["", f"### {f.name}", f.content.strip()]
            return "\n".join(parts)

        return _instructions


@dataclass
class SkillsPromptCapability(AbstractCapability[Any]):
    """Inject the ``<available_skills>`` XML block plus selection rules into
    the agent's instructions.

    Each subdirectory of ``<workspace_dir>/skills/`` containing a SKILL.md
    becomes a skill entry. Re-scanned each run.
    """

    workspace_dir: str

    def get_instructions(self):
        wd = self.workspace_dir

        def _instructions(ctx: RunContext[Any]) -> str:
            xml = build_skills_xml(wd)
            if not xml:
                return ""
            return "## Skills\n" + _SKILLS_USAGE + "\n\n" + xml

        return _instructions


@dataclass
class SessionThinkingCapability(AbstractCapability[str]):
    """Per-session ``reasoning_effort`` override sourced from the session store.

    Reads the ``"thinking"`` meta key (set by the ``/think`` slash command)
    from ``session_store`` using the agent's ``deps`` as the session key.
    Falls back to ``default_thinking`` when the session has no explicit setting.

    Requires the agent to run with ``deps_type=str`` and ``deps=session_key``
    on each call.
    """

    session_store: JsonlStore
    default_thinking: str | None = None

    def get_model_settings(self):
        store = self.session_store
        default = self.default_thinking

        def _settings(ctx: RunContext[str]) -> ModelSettings:
            session_key = ctx.deps
            thinking = store.get_meta(session_key, "thinking") or default
            if thinking and thinking != "off":
                return ModelSettings(reasoning_effort=thinking)
            return ModelSettings()

        return _settings


@dataclass
class RuntimeInfoCapability(AbstractCapability[Any]):
    """Inject a one-line runtime info (host, os, arch, model, shell, date)
    into the agent's instructions.

    Date is re-evaluated each run, so long-lived sessions see the new date
    after midnight without restart.
    """

    model_name: str = ""

    def get_instructions(self):
        model = self.model_name

        def _instructions(ctx: RunContext[Any]) -> str:
            pairs = [
                ("host", platform.node()),
                ("os", f"{platform.system()} {platform.release()}"),
                ("arch", platform.machine()),
                ("model", model),
                ("shell", os.environ.get("SHELL", "")),
                ("date", date.today().isoformat()),
            ]
            line = " | ".join(f"{k}={v}" for k, v in pairs if v)
            return f"## Runtime\nRuntime: {line}"

        return _instructions


@dataclass
class HeartbeatCapability(AbstractCapability[Any]):
    """Per-run capability for structured heartbeat outcome signaling.

    Inject a fresh instance into each heartbeat agent run. After the run,
    read `should_alert` and `alert_text` instead of scanning raw text for
    HEARTBEAT_OK. Falls back gracefully when the model skips the tool call.
    """

    _notify: bool | None = field(default=None, init=False, repr=False)
    _text: str = field(default="", init=False, repr=False)

    def get_toolset(self) -> AgentToolset[Any] | None:
        cap = self

        async def heartbeat_respond(notify: bool, text: str = "") -> str:
            """Signal heartbeat outcome. Call once after completing all checks.

            Args:
                notify: True = deliver text as alert, False = stay silent.
                text: Concise alert message shown to the user (1–2 sentences).
                      Required when notify=True, ignored when notify=False.
            """
            cap._notify = notify
            cap._text = text
            return "Recorded."

        return FunctionToolset([heartbeat_respond])

    def get_instructions(self):
        return (
            "## Heartbeat Protocol\n"
            "After completing all heartbeat tasks call `heartbeat_respond` exactly once:\n"
            "- `heartbeat_respond(notify=True, text='...')` — something needs user attention\n"
            "- `heartbeat_respond(notify=False)` — nothing to report, all clear\n"
            "Keep notification text to 1–2 sentences."
        )

    @property
    def was_called(self) -> bool:
        return self._notify is not None

    @property
    def should_alert(self) -> bool:
        return self._notify is True and bool(self._text)

    @property
    def alert_text(self) -> str:
        return self._text


def _expand_env(mapping: dict[str, str]) -> dict[str, str]:
    """Expand ``${VAR}`` / ``$VAR`` in mapping values from the environment so
    secrets live in the environment, not in selmakit.json."""
    return {k: os.path.expandvars(v) for k, v in mapping.items()}


@dataclass
class McpCapability(AbstractCapability[Any]):
    """Attach external MCP servers as tools.

    Servers come from the ``mcp`` section of selmakit.json (the standard
    ``mcpServers`` fields, so existing server configs port over unchanged). Each
    server becomes an ``MCPToolset`` over an explicit stdio or HTTP transport;
    optional per-server ``prefix`` namespaces its tool names and ``allow_tools``
    whitelists them. All servers are merged into one ``CombinedToolset``.

    The toolset is built once at construction. pydantic-ai opens/closes the
    underlying connection around each run; keeping it open across runs (entering
    the agent context once at startup) is a later optimization.
    """

    servers: dict[str, Any]  # name -> McpServerConfig
    _toolset: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        from pydantic_ai.mcp import (
            MCPToolset, StdioTransport, StreamableHttpTransport,
        )
        from pydantic_ai.toolsets import CombinedToolset

        try:
            from fastmcp import Client
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "MCP support needs the 'fastmcp' package (ships with pydantic-ai's "
                "mcp extra). Install it or disable mcp in selmakit.json."
            ) from e

        toolsets: list[Any] = []
        for name, s in self.servers.items():
            if not getattr(s, "enabled", True):
                continue

            if s.command:
                transport = StdioTransport(
                    command=s.command, args=list(s.args),
                    env=_expand_env(s.env) or None, cwd=s.cwd,
                )
            elif s.url:
                transport = StreamableHttpTransport(
                    url=s.url, headers=_expand_env(s.headers) or None,
                )
            else:
                logger.warning("MCP server %r has neither command nor url — skipping", name)
                continue

            ts: Any = MCPToolset(Client(transport))
            if s.allow_tools is not None:
                allow = set(s.allow_tools)
                ts = ts.filtered(lambda ctx, td, _a=allow: td.name in _a)
            if getattr(s, "require_approval", False):
                # Gate every call: the run returns a DeferredToolRequests instead
                # of executing. Resolved via /approve /deny (see Agent) or auto-
                # denied in unattended (heartbeat/cron) runs.
                ts = ts.approval_required()
            if s.prefix:
                ts = ts.prefixed(s.prefix)
            toolsets.append(ts)

        self._toolset = CombinedToolset(toolsets) if toolsets else None

    def get_toolset(self) -> AgentToolset[Any] | None:
        return self._toolset
