from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from selmakit.agent import Agent
    from selmakit.config import SelmaKitConfig


@dataclass
class RunPrompt:
    """A command handler returns this instead of text: selmakit runs
    `text` as a normal (streamed) user turn — the general form of the
    /skill rewrite-and-run pattern."""

    text: str


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
        """Show or set the active model for this session.

        Validates the target before setting it: the provider prefix must be one
        build_model() knows, and for ``ollama/…`` the model must actually be
        installed at the configured endpoint (checked live, skipped if the
        endpoint is unreachable so an offline check never blocks a switch).
        """
        override = ctx.session.get("model_override")
        current = override or config.model.model
        if not ctx.args:
            return f"Current model: {current}"

        target = ctx.args.strip()
        provider, _, model_name = target.partition("/")
        if not model_name:  # bare name → ollama, matching build_model()
            provider, model_name = "ollama", provider
        provider = provider.lower()

        known = {"ollama", "openai", "anthropic", "google", "gemini", "google-gla"}
        if provider not in known:
            return (
                f"Unknown provider `{provider}`. "
                "Use one of: ollama, openai, anthropic, google/gemini."
            )

        if provider == "ollama":
            url = f"{config.model.effective_base_url.rstrip('/')}/models"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    installed = [m["id"] for m in json.loads(resp.read()).get("data", [])]
            except Exception:
                installed = None  # endpoint unreachable → don't block the switch
            if installed is not None and model_name not in installed:
                avail = ", ".join(f"`{m}`" for m in sorted(installed)) or "(none installed)"
                return f"Ollama model `{model_name}` not found at the endpoint.\nInstalled: {avail}"

        ctx.session.set("model_override", target)
        return f"Model set to: {target}"

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

    async def cmd_verbose(ctx: CommandContext) -> str:
        """Show or toggle verbose mode (on / off).

        When on, the webchat stream surfaces tool calls (→ name(args)), their
        results (← name: …), tool errors, per-tool timing and — if /think is
        active — reasoning deltas, so you can see what the model is doing.
        """
        if not ctx.args:
            state = "on" if ctx.session.get("verbose") else "off"
            return f"Verbose mode is `{state}`."
        arg = ctx.args.strip().lower()
        if arg not in {"on", "off"}:
            return f"Invalid value `{arg}`. Use: on, off."
        ctx.session.set("verbose", arg == "on")
        return f"Verbose mode set to: {arg}"

    # ── Approval (deferred / gated tools) ─────────────────────
    # These are intercepted in Agent._prepare_run before dispatch (they resume a
    # deferred run); the handlers here only run when nothing is pending and exist
    # mainly so /help lists them.

    async def cmd_approve(ctx: CommandContext) -> str:
        """Approve the pending gated tool call(s) and continue (or use the ✅ button)."""
        return "Es steht derzeit nichts zur Freigabe aus."

    async def cmd_deny(ctx: CommandContext) -> str:
        """Deny the pending gated tool call(s) awaiting approval (or use the 🚫 button)."""
        return "Es steht derzeit nichts zur Freigabe aus."

    # ── Config & Status ───────────────────────────────────────

    async def cmd_config(ctx: CommandContext) -> str:
        """Show the current selmakit.json configuration."""
        return config.model_dump_json(indent=2)

    async def cmd_status(ctx: CommandContext) -> str:
        """Show runtime status: model, thinking level, session, message count."""
        agent: Agent = ctx.agent
        model_name = ctx.session.get("model_override") or config.model.model
        thinking = ctx.session.get("thinking") or "off"
        verbose = "on" if ctx.session.get("verbose") else "off"
        msg_count = agent.message_count(ctx.session_key)
        until_compact = agent.messages_until_compaction(ctx.session_key)

        lines = [
            "**selmakit Status**",
            "",
            f"`model`      {model_name}",
            f"`thinking`   {thinking}",
            f"`verbose`    {verbose}",
            "",
            f"`session`    {ctx.session_key}",
            f"`messages`   {msg_count}  (compaction in {until_compact} messages)",
        ]

        pending = agent.pending_approvals(ctx.session_key)
        if pending:
            names = ", ".join(p.get("tool_name", "tool") for p in pending)
            lines.append(f"`approval`   {len(pending)} pending — {names}  (/approve · /deny)")

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

    # ── MCP ───────────────────────────────────────────────────

    async def cmd_mcp(ctx: CommandContext) -> str:
        """List MCP servers, or toggle one: /mcp [enable|disable <name>].

        enable/disable patches selmakit.json and takes effect on the next gateway
        restart (MCP toolsets are built once at startup).
        """
        import json as _json
        agent: Agent = ctx.agent
        cfg_path = agent.workspace_dir.parent / "selmakit.json"

        def _load() -> dict:
            try:
                return _json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                return {}

        parts = ctx.args.strip().split()
        if parts:
            action = parts[0].lower()
            if action not in ("enable", "disable"):
                return "Usage: `/mcp [enable|disable <name>]`"
            if len(parts) < 2:
                return f"Usage: `/mcp {action} <server-name>`"
            name = parts[1]
            data = _load()
            servers = data.get("mcp", {}).get("servers", {})
            if name not in servers:
                known = ", ".join(f"`{n}`" for n in servers) or "—"
                return f"Unbekannter MCP-Server `{name}`. Bekannt: {known}."
            servers[name]["enabled"] = (action == "enable")
            cfg_path.write_text(_json.dumps(data, indent=4), encoding="utf-8")
            return f"MCP-Server `{name}` {'aktiviert' if action == 'enable' else 'deaktiviert'} — wird beim nächsten Gateway-Neustart wirksam."

        # No args → list servers (fresh from disk).
        mcp = _load().get("mcp", {})
        servers = mcp.get("servers", {})
        if not servers:
            return "Keine MCP-Server in selmakit.json konfiguriert."
        # What was actually loaded at startup (the captured config).
        startup_loaded = {n for n, s in config.mcp.servers.items() if s.enabled} if config.mcp.enabled else set()

        lines = [f"**MCP-Server** — global: {'enabled' if mcp.get('enabled') else 'disabled'}", ""]
        for name, s in servers.items():
            enabled = s.get("enabled", True)
            if s.get("command"):
                transport = " ".join([s["command"], *s.get("args", [])]).strip()
            elif s.get("url"):
                transport = s["url"]
            else:
                transport = "—"
            extras = []
            if s.get("prefix"):
                extras.append(f"prefix={s['prefix']}")
            if s.get("allow_tools") is not None:
                extras.append(f"allow={','.join(s['allow_tools'])}")
            if s.get("require_approval"):
                extras.append("approval=on")
            extra_s = ("  · " + " · ".join(extras)) if extras else ""
            lines.append(f"• `{name}` — {'✅ enabled' if enabled else '⛔ disabled'} | {transport}{extra_s}")
            if enabled and mcp.get("enabled"):
                lines.append(f"  ↳ {'geladen (läuft)' if name in startup_loaded else 'aktiviert — Neustart lädt den Server'}")
            elif not enabled and name in startup_loaded:
                lines.append("  ↳ noch geladen — Neustart entlädt den Server")

        lines += ["", "Ändern: `/mcp enable <name>` · `/mcp disable <name>` (wirkt nach Neustart)."]
        return "\n".join(lines)

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
        "/verbose":  cmd_verbose,
        "/approve":  cmd_approve,
        "/deny":     cmd_deny,
        "/config":   cmd_config,
        "/status":   cmd_status,
        "/systemprompt": cmd_systemprompt,
        "/tools":    cmd_tools,
        "/skills":   cmd_skills,
        "/commands": cmd_commands,
        "/cron":     cmd_cron,
        "/mcp":      cmd_mcp,
        "/help":     cmd_help,
    }
