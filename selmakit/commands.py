from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from selmakit.agent import Agent
    from selmakit.config import SelmaKitConfig


class SessionProxy:
    """Provides session control inside a command handler."""

    def __init__(self, sessions_dir: str, session_key: str):
        self._dir = Path(sessions_dir)
        self._session_key = session_key

    def invalidate(self) -> None:
        """Delete the session history — next turn starts fresh."""
        for suffix in (".json", ".meta.json"):
            f = self._dir / f"{self._session_key}{suffix}"
            if f.exists():
                f.unlink()

    def set(self, key: str, value: Any) -> None:
        """Persist a key/value pair in the session metadata."""
        meta = self._load_meta()
        meta[key] = value
        self._save_meta(meta)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a key from the session metadata."""
        return self._load_meta().get(key, default)

    def _meta_path(self) -> Path:
        return self._dir / f"{self._session_key}.meta.json"

    def _load_meta(self) -> dict:
        p = self._meta_path()
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_meta(self, meta: dict) -> None:
        self._meta_path().write_text(json.dumps(meta, indent=2), encoding="utf-8")


class CommandContext(BaseModel):
    """Context passed to every @agent.command handler."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    args: str
    session_key: str
    session: SessionProxy
    agent: Any = None


def make_commands(config: "SelmaKitConfig", cron_store: Any = None) -> dict[str, Callable]:
    """Return the standard set of command handlers as a dict."""

    # ── Session ───────────────────────────────────────────────

    async def cmd_reset(ctx: CommandContext) -> str:
        """Reset the current session."""
        ctx.session.invalidate()
        return "Session reset."

    async def cmd_new(ctx: CommandContext) -> str:
        """Reset the current session (alias for /reset)."""
        ctx.session.invalidate()
        return "Session reset."

    async def cmd_compact(ctx: CommandContext) -> str:
        """Compact the session history and flush facts to memory."""
        agent: Agent = ctx.agent
        count = agent.message_count(ctx.session_key)
        if count <= 4:
            return f"Nothing to compact ({count} messages)."
        await agent.memory_flush(ctx.session_key)
        before, after = await agent.compact_session(ctx.session_key)
        return f"Session compacted: {before} → {after} messages."

    # ── Model & Thinking ──────────────────────────────────────

    async def cmd_model(ctx: CommandContext) -> str:
        """Show or set the active model for this session."""
        override = ctx.session.get("model_override")
        current = override or config.model.model
        if ctx.args:
            ctx.session.set("model_override", ctx.args.strip())
            return f"Model set to: {ctx.args.strip()}"
        return f"Current model: {current}"

    async def cmd_models(ctx: CommandContext) -> str:
        """List all models available at the configured endpoint."""
        url = f"{config.model.base_url}/models"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            if not models:
                return "No models found."
            return "Available models:\n" + "\n".join(f"• `{m}`" for m in sorted(models))
        except Exception as e:
            return f"Could not fetch models: {e}"

    async def cmd_think(ctx: CommandContext) -> str:
        """Show or set the thinking level (off / low / medium / high)."""
        if not ctx.args:
            level = ctx.session.get("thinking") or "off"
            return f"Current thinking level: `{level}`"
        level = ctx.args.strip().lower()
        if level not in {"off", "low", "medium", "high"}:
            return f"Invalid level `{level}`. Use: off, low, medium, high."
        ctx.session.set("thinking", level)
        return f"Thinking level set to: {level}"

    # ── Config & Status ───────────────────────────────────────

    async def cmd_config(ctx: CommandContext) -> str:
        """Show the current selmakit.json configuration."""
        return config.model_dump_json(indent=2)

    async def cmd_status(ctx: CommandContext) -> str:
        """Show runtime status: model, thinking level, session, message count."""
        agent: Agent = ctx.agent
        model_name = ctx.session.get("model_override") or config.model.model
        thinking = ctx.session.get("thinking") or "off"
        msg_count = agent.message_count(ctx.session_key)
        until_compact = agent.messages_until_compaction(ctx.session_key)

        lines = [
            "**selmakit Status**",
            "",
            f"`model`      {model_name}",
            f"`thinking`   {thinking}",
            "",
            f"`session`    {ctx.session_key}",
            f"`messages`   {msg_count}  (compaction in {until_compact} messages)",
        ]

        schedules = agent.get_schedules()
        if schedules:
            lines.append("")
            for s in schedules:
                if s["next_run_at"]:
                    lines.append(f"`heartbeat`  every {s['every']} — next {s['next_run_at'].strftime('%H:%M:%S')}")
                else:
                    lines.append(f"`heartbeat`  every {s['every']} — running now")

        return "\n".join(lines)

    async def cmd_systemprompt(ctx: CommandContext) -> str:
        """Show the system prompt as last sent to the model this session."""
        agent: Agent = ctx.agent
        prompt = agent.last_system_prompt(ctx.session_key)
        if not prompt:
            return "No system prompt recorded yet — send a message first."
        return prompt

    # ── Tools ─────────────────────────────────────────────────

    async def cmd_tools(ctx: CommandContext) -> str:
        """List all active tools registered with the agent."""
        agent: Agent = ctx.agent
        tools = agent.get_tools()
        if not tools:
            return "No tools registered."
        lines = []
        for name, desc in sorted(tools.items()):
            lines.append(f"• `{name}`" + (f" — {desc.splitlines()[0]}" if desc else ""))
        return "\n\n".join(lines)

    # ── Skills ────────────────────────────────────────────────

    async def cmd_skills(ctx: CommandContext) -> str:
        """List all available skills — run one with /skill <name> [args]."""
        from selmakit.skills import list_skills
        agent: Agent = ctx.agent
        skills = list_skills(str(agent.workspace_dir))
        if not skills:
            return "No skills found."
        lines = []
        for s in skills:
            lines.append(f"• `{s['name']}`" + (f" — {s['description'].splitlines()[0]}" if s["description"] else ""))
        return "\n\n".join(lines)

    # ── Commands ──────────────────────────────────────────────

    async def cmd_commands(ctx: CommandContext) -> str:
        """List all currently registered commands with their descriptions."""
        agent: Agent = ctx.agent
        cmds = agent.get_commands()
        if not cmds:
            return "No commands registered."
        lines = []
        for name, desc in sorted(cmds.items()):
            lines.append(f"• `{name}`" + (f" — {desc}" if desc else ""))
        return "\n\n".join(lines)

    # ── Cron ──────────────────────────────────────────────────

    async def cmd_cron(ctx: CommandContext) -> str:
        """List active cron jobs."""
        if cron_store is None:
            return "Cron is not configured."
        from selmakit.cron import _format_jobs
        jobs = cron_store.load()
        return _format_jobs(jobs)

    # ── Help ──────────────────────────────────────────────────

    async def cmd_help(ctx: CommandContext) -> str:
        """Show this help message."""
        return await cmd_commands(ctx)

    return {
        "/reset":    cmd_reset,
        "/new":      cmd_new,
        "/compact":  cmd_compact,
        "/model":    cmd_model,
        "/models":   cmd_models,
        "/think":    cmd_think,
        "/config":   cmd_config,
        "/status":   cmd_status,
        "/systemprompt": cmd_systemprompt,
        "/tools":    cmd_tools,
        "/skills":   cmd_skills,
        "/commands": cmd_commands,
        "/cron":     cmd_cron,
        "/help":     cmd_help,
    }
