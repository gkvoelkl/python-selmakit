# selmakit

![selmakit](images/selmakit.png)

**Question: Is it possible to rebuild OpenClaw with Pydantic-AI?**

[OpenClaw](https://openclaw.ai) is a open multi-channel agent platform — identity files, long-term memory, skill routing, scheduled proactive turns, and multiple messaging channels unified behind a single agent loop. This project is an attempt to answer: *can you build the same architecture yourself, in Python, with open-source tools?*

The answer is **yes**. `selmakit` is the result.

---

## What it is

`selmakit` is a minimal agent framework built on top of [pydantic-ai 2.16](https://github.com/pydantic/pydantic-ai). Pydantic-AI handles the LLM loop — tool calling, streaming, type safety. `selmakit` handles everything around it.

```
pydantic-ai  →  LLM loop
selmakit     →  channels, sessions, commands, memory, skills, scheduling
```

It runs a local Ollama model by default, but the same `model` config knob also drives hosted OpenAI, Anthropic (Claude), and Google (Gemini) models — see [Model providers](#model-providers). It serves a web chat UI via SSE, connects to Telegram, persists sessions, and routes skills — all wired up by a reusable `Gateway`, so your own agent is just a few lines.

---

## Features at a Glance

| Feature | Implementation |
|---|---|
| Multi-channel (WebChat + Telegram) | `WebChatChannel`, `TelegramChannel` |
| Session persistence + compaction | `JsonlStore` — JSONL per session, auto-compact at 50 messages |
| Long-term memory | `SqliteMemory` — FTS5 + optional vector search + temporal decay (now an `AbstractCapability`) |
| Slash commands | `@agent.command("/reset")` decorator |
| Output validators | `@agent.output_validator` decorator |
| Scheduled proactive turns | `@agent.schedule(every="30m")` decorator |
| Workspace identity files | `SOUL.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md` |
| Skills | `SKILL.md` files — discovered, XML-injected into system prompt |
| Filesystem tools | `FilesystemCapability(cwd=".")` — read/write/edit/ls/grep/find |
| Web search & fetch | `WebSearch(local="duckduckgo")`, `WebFetch(local=True)` — native on supporting providers, local fallback otherwise |
| External MCP servers | `McpCapability` — stdio/HTTP servers from `selmakit.json` (standard `mcpServers` shape), per-server `prefix`/`allow_tools`/`require_approval`; connections held open for the gateway's lifetime |
| Tool approval | Gated MCP tools defer instead of executing; approve/deny via `/approve` `/deny` or the dashboard's ✅/🚫 buttons; auto-denied in unattended (heartbeat/cron) runs |
| Sub-agent delegation | `SubAgents` (optional `subagents` extra, from [pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness)) — `delegate_task` hands self-contained work to named, isolated sub-agents |
| Dynamic prompt sections | `WorkspacePromptCapability`, `SkillsPromptCapability`, `RuntimeInfoCapability`, `BootstrapCapability` |
| Per-session thinking | `SessionThinkingCapability` — `/think high` writes to session meta, capability picks it up |
| Live model switching | Per-session `model_override` via `/model` or the dashboard selector — takes effect next turn, no restart |
| Verbose mode | `/verbose on` streams tool calls/results/timing and reasoning deltas into a collapsible dashboard panel |
| OpenTelemetry tracing | `pydantic_ai.Agent.instrument_all()` exporting OTLP/gRPC to a standalone Phoenix container (`arize-phoenix` is not a Python dependency — it pins pydantic-ai-slim<2) |
| Streamlit dashboard | `selmakit.dashboard.run(title=, image=, input_placeholder=)` — brandable SSE chat + heartbeat alerts |
| Reusable runtime | `Gateway.from_config(extra_capabilities=[...]).run()` — backend in one line |
| Config | `selmakit.json` with 120s cache |

---

## Architecture

```
gateway.py
  │
  ├── Agent (selmakit.Agent wraps pydantic_ai.Agent)
  │     ├── capabilities (everything LLM-facing):
  │     │     ├── FilesystemCapability      — read/write/edit/ls/grep/find
  │     │     ├── WebSearch / WebFetch      — native or local fallback
  │     │     ├── BootstrapCapability       — first-run hint while BOOTSTRAP.md exists
  │     │     ├── WorkspacePromptCapability — injects SOUL/IDENTITY/USER/… MD files
  │     │     ├── SkillsPromptCapability    — emits the <available_skills> block
  │     │     ├── RuntimeInfoCapability     — host/os/model/date line
  │     │     ├── SessionThinkingCapability — per-session thinking (reasoning effort) override
  │     │     └── SqliteMemory              — memory_search / memory_write
  │     ├── session_store: JsonlStore       — .selmakit/sessions/
  │     └── heartbeat: ScheduleRunner       — asyncio background task
  │
  ├── asyncio.Queue[QueueItem]  ← both channels write here
  │
  ├── worker()  ← reads queue, calls agent.run_stream_events()
  │
  ├── WebChatChannel  (FastAPI + SSE)
  └── TelegramChannel (python-telegram-bot)
```

### State directory

All runtime state lives under `.selmakit/` (configurable):

```
.selmakit/
  selmakit.json         — config
  sessions/             — one .json + .meta.json per session_key
  workspace/
    SOUL.md             — agent personality
    IDENTITY.md         — agent identity
    USER.md             — user context
    HEARTBEAT.md        — heartbeat instructions
    BOOTSTRAP.md        — first-run onboarding script (cleared after setup)
    memory/             — daily memory files (YYYY-MM-DD.md)
    skills/             — SKILL.md skill definitions
  memory.db             — SQLite FTS5 index
```

### Message flow

1. User sends a message via WebChat or Telegram
2. Channel creates a `QueueItem(session_key, prompt, reply)` and enqueues it
3. `worker()` dequeues and calls `agent.run_stream_events()`
4. Slash commands (`/reset`, `/status`, ...) are intercepted before the LLM
5. Tool call events (`FunctionToolCallEvent`) are forwarded as SSE `tool` events
6. Text delta events stream as SSE `chunk` events
7. Session is saved after each turn

---

## Quick Start

**Prerequisites:** [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com) running locally, a Telegram bot token (optional).

```bash
git clone https://github.com/gkvoelkl/python-selmakit
cd python-selmakit

uv sync

# Initialize directory structure, config, and workspace files
uv run python setup.py

cp .env.example .env
# Edit .env: set TELEGRAM_TOKEN (only needed for Telegram channel)

# Edit .selmakit/selmakit.json to set your model
# Edit .selmakit/workspace/IDENTITY.md and USER.md

# Start Phoenix tracing (Docker) + gateway + Streamlit dashboard
./start.sh        # Linux / macOS
start.bat         # Windows
```

Or run components individually:

```bash
uv run python gateway.py           # gateway only
uv run streamlit run dashboard.py  # dashboard only
docker run -d --rm -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest  # tracing UI at http://localhost:6006
```

---

## Build Your Own Agent in Two Files

You don't need to fork the repo to build your own agent. `selmakit` ships the whole runtime — the `Gateway` (backend) and the Streamlit `dashboard` (frontend) are library components. A custom agent is just two thin files plus a `.selmakit/selmakit.json`.

**`gateway.py`** — backend. Build from config and run, adding your own capabilities:

```python
from dotenv import load_dotenv
load_dotenv()

from selmakit import Gateway
from my_agent.capabilities import WeatherCapability

# Default capabilities + your own
Gateway.from_config(extra_capabilities=[WeatherCapability(api_key="...")]).run()
```

`Gateway.from_config()` reads `.selmakit/selmakit.json`, builds the model, session store, memory and cron store, wires the agent to its channels, worker, schedules and cron, and runs them all under one `asyncio.gather`. With no arguments it uses `default_capabilities()` — the standard set. Start it with `uv run gateway.py`.

**`dashboard.py`** — frontend. Brand it with your own title, image and prompt:

```python
from selmakit.dashboard import run

run(
    title="🌦️ Weather Agent",
    image="images/weather.png",
    input_placeholder="Ask me about the weather…",
)
```

Start it with `uv run streamlit run dashboard.py`. `DashboardConfig` also exposes `gateway_base_url` (the SSE stream + heartbeat-poll URLs are derived from it), `user_name`, `page_icon`, `show_settings`, and `stream_timeout` (httpx read timeout for the SSE stream, default `120.0` s; raise it or set `None` to disable for long-running turns that stay silent for a while, e.g. QGIS/STAC jobs).

### Customizing capabilities

| You want to… | Pass to `Gateway.from_config(...)` |
|---|---|
| Add a self-contained capability | `extra_capabilities=[MyCapability(...)]` |
| Add one that needs an internal object (session store, cron store, workspace dir, model name) | `capabilities=lambda ctx: [*default_capabilities(ctx), MyCapability(store=ctx.session_store)]` |
| Replace the default set entirely | `capabilities=[...]` (a plain list) |

The `ctx` passed to a `capabilities` callable is a `GatewayContext` exposing `config`, `model`, `state_dir`, `workspace_dir`, `model_name`, `session_store`, `memory`, and `cron_store`. See [Writing your own capability](#capabilities).

The root `gateway.py` and `dashboard.py` in this repo are exactly such reference files — Selma herself is built with them.

---

## Configuration

`.selmakit/selmakit.json`:

```json
{
  "model": {
    "model": "ollama/qwen3:8b",
    "base_url": "http://localhost:11434/v1",
    "timeout_seconds": 120
  },
  "memory": {
    "enabled": true,
    "vector_search": false,
    "embed_model": "nomic-embed-text",
    "temporal_decay": true,
    "temporal_decay_rate": 0.05
  },
  "session": {
    "reset": {
      "at_hour": 4,
      "idle_minutes": 120
    }
  },
  "channels": {
    "webchat": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 8000,
      "log_level": "info"
    },
    "telegram": {
      "enabled": false
    }
  },
  "heartbeat": {
    "enabled": true,
    "every": "30m",
    "active_hours": ["08:00", "22:00"],
    "timezone": "Europe/Berlin",
    "target": "last"
  },
  "mcp": {
    "enabled": true,
    "servers": {
      "weather": {
        "command": "uv",
        "args": ["run", "examples/weather_mcp.py"],
        "require_approval": true
      }
    }
  },
  "subagents": {
    "enabled": true,
    "agents": [
      {
        "name": "researcher",
        "description": "Researches facts on the web and summarizes with sources.",
        "system_prompt": "You are a research assistant. Use web tools, check multiple sources, answer concisely.",
        "max_calls": 8,
        "timeout_seconds": 120
      }
    ]
  }
}
```

The `subagents` section (optional — install the extra with `uv sync --extra subagents`) enables **task delegation** via the `SubAgents` capability from [pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness). Each entry becomes an isolated sub-agent (its own `system_prompt`, optional `model`, plus filesystem + web tools) that the main agent invokes by `name` through a single `delegate_task(agent_name, task)` tool; `max_calls`/`timeout_seconds` bound each delegation. Sub-agents never see the parent conversation. Added to the default capabilities when `subagents.enabled` and at least one agent is configured.

The `mcp` section attaches external [MCP](https://modelcontextprotocol.io) servers as tools (`McpCapability`, added to the default set when `mcp.enabled` and at least one server is configured). Each entry uses the standard `mcpServers` fields — stdio (`command`/`args`/`env`/`cwd`) or HTTP (`url`/`headers`) — plus selmakit extras: `enabled`, `prefix` (namespace the tool names), `allow_tools` (whitelist), and `require_approval` (gate every call behind human approval — the run defers and you resolve it with `/approve`/`/deny` or the dashboard buttons; unattended heartbeat/cron runs auto-deny). `${VAR}` in `env`/`headers` is expanded from the environment. `examples/weather_mcp.py` is a self-contained reference server (Open-Meteo, no API key). Manage servers at runtime with `/mcp`.

Thinking effort is per session, not per agent. Use `/think low|medium|high|off` in a chat — the value lands in `.meta.json` and `SessionThinkingCapability` reads it from there on each run. The value flows into pydantic-ai's unified `thinking` model setting; on providers without thinking support it is harmless (ignored).

### Model providers

The `model.model` string is a `"provider/model"` pair. `config.build_model()` — called by both `Gateway.from_config()` and `Agent.from_file()` — dispatches on the provider prefix, so you switch backends by editing one config value (no code change):

| `model.model` | Backend | Credentials / endpoint |
|---|---|---|
| `ollama/llama3.2` *(default)* | Local Ollama via its OpenAI-compatible endpoint | `model.base_url` (default `http://localhost:11434/v1`); no API key |
| `openai/gpt-5` | OpenAI | `OPENAI_API_KEY` (optional `OPENAI_BASE_URL`) |
| `anthropic/claude-sonnet-4-6` · `anthropic/claude-opus-4-8` | Anthropic (Claude) | `ANTHROPIC_API_KEY` |
| `google/gemini-2.5-pro` · `gemini/gemini-2.5-flash` | Google (Gemini) | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

A bare model string with no `provider/` prefix defaults to `ollama`. Only the `ollama` branch reads `model.base_url` — the hosted providers pick up their endpoints and keys from the environment (put them in `.env`). Ollama stays the primary, local-first path; the hosted providers are there when you want more capability or a cloud fallback. The `anthropic` and `google-genai` SDKs ship with the full `pydantic-ai` dependency, so no extra install is needed. An unknown provider raises a `ValueError` at startup.

---

## The Agent Class

`selmakit.Agent` wraps `pydantic_ai.Agent` and adds everything needed for a production agent loop. The pydantic-ai agent is never used directly — all interaction goes through `selmakit.Agent`.

### Construction

Everything LLM-facing lives in `capabilities=[...]`. Selmakit-specific concerns (session persistence, slash commands, heartbeat) stay as constructor kwargs.

```python
from pydantic_ai.capabilities import WebFetch, WebSearch
from selmakit import (
    Agent, JsonlStore, SqliteMemory,
    BootstrapCapability, FilesystemCapability,
    RuntimeInfoCapability, SessionThinkingCapability,
    SkillsPromptCapability, WorkspacePromptCapability,
)

state_dir = ".selmakit"
workspace_dir = f"{state_dir}/workspace"

session_store = JsonlStore(path=f"{state_dir}/sessions", at_hour=4, idle_minutes=120)

agent = Agent(
    model=model,
    state_dir=state_dir,
    session_store=session_store,
    memory=SqliteMemory(workspace_dir=workspace_dir, vector_search=False, temporal_decay=True),
    commands=make_commands(config),
    heartbeat=ScheduleConfig(every="30m", active_hours=("08:00", "22:00")),
    capabilities=[
        FilesystemCapability(cwd="."),
        WebSearch(local="duckduckgo"),
        WebFetch(local=True),
        BootstrapCapability(workspace_dir=workspace_dir),
        WorkspacePromptCapability(workspace_dir=workspace_dir),
        SkillsPromptCapability(workspace_dir=workspace_dir),
        RuntimeInfoCapability(model_name="ollama/qwen3:8b"),
        SessionThinkingCapability(session_store=session_store),
    ],
)
```

Or from `selmakit.json` in one call:

```python
agent = Agent.from_file(state_dir=".selmakit", capabilities=[WebSearch(local="duckduckgo")])
```

`from_file()` reads `selmakit.json`, builds the model, session store, and memory, and passes everything to the constructor.

---

### Capabilities

`selmakit` composes a set of `pydantic_ai.capabilities.AbstractCapability` subclasses that bundle tools, instructions, and model settings — its own, some shipped by pydantic-ai (`WebSearch`/`WebFetch`), and one optional from pydantic-ai-harness (`SubAgents`). Each one is independent — drop any of them or write your own without touching the rest of the system.

| Capability | Contribution | Lifecycle |
|---|---|---|
| `FilesystemCapability(cwd)` | `read`/`write`/`edit`/`ls`/`grep`/`find` toolset bound to `cwd` | `get_toolset()` |
| `WebSearch(local=...)` / `WebFetch(local=...)` | Native server-side on supporting providers, DuckDuckGo / markdownify fallback otherwise | `get_native_tools()` |
| `McpCapability(servers)` | One `MCPToolset` per configured MCP server (stdio/HTTP), merged into a `CombinedToolset`; optional `prefix`/`allow_tools`/`require_approval` | `get_toolset()` |
| `SubAgents(agents=...)` (harness) | `delegate_task` tool that runs a named sub-agent in isolation; from `pydantic-ai-harness` (optional `subagents` extra) | `get_toolset()` |
| `WorkspacePromptCapability(workspace_dir)` | Injects all `*.md` files from the workspace under `## Workspace Files` | dynamic `get_instructions()` |
| `SkillsPromptCapability(workspace_dir)` | Emits `<available_skills>` XML + selection rules | dynamic `get_instructions()` |
| `RuntimeInfoCapability(model_name)` | One-line `host / os / model / date` runtime info; date re-evaluated each run | dynamic `get_instructions()` |
| `BootstrapCapability(workspace_dir)` | Adds a bootstrap-pending hint while `BOOTSTRAP.md` has non-empty content; emptying or deleting the file silences it on the next turn | dynamic `get_instructions()` |
| `SessionThinkingCapability(session_store)` | Reads `"thinking"` meta key via `ctx.deps` (= session_key) and sets the unified `thinking` setting per run | `get_model_settings()` |
| `SqliteMemory(workspace_dir, …)` | `memory_search` / `memory_write` toolset + usage instructions | `get_toolset()` + `get_instructions()` |

Writing your own:

```python
from dataclasses import dataclass
from typing import Any
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

@dataclass
class GreetingCapability(AbstractCapability[Any]):
    name: str

    def get_instructions(self):
        n = self.name
        def _instructions(ctx: RunContext[Any]) -> str:
            return f"Greet the user as {n} on the first turn."
        return _instructions
```

Just add it to `capabilities=[...]`. pydantic-ai concatenates instructions, merges model settings, and combines toolsets in declared order. Use `agent._agent.root_capability.apply(visitor)` to walk the full capability tree.

---

### Slash Commands

`@agent.command` registers a handler that intercepts messages starting with `/` **before** the LLM is called. Handlers receive a `CommandContext` with `ctx.args`, `ctx.session_key`, and `ctx.session` (backed by `.meta.json`).

The simplest possible command — an `async` function that returns a string. The first docstring line becomes its description in `/help` and `/commands`:

```python
@agent.command("/echo")
async def cmd_echo(ctx: CommandContext) -> str:
    """Echo the arguments back."""
    return f"You said: {ctx.args}"
```

`ctx.args` holds everything after the command name (e.g. `/echo hi there` → `"hi there"`), and is an empty string when none are given:

```python
@agent.command("/hello")
async def cmd_hello(ctx: CommandContext) -> str:
    """Say hello."""
    return f"Hello, {ctx.args or 'world'}!"
```

The session proxy allows reading and writing persistent per-session state:

```python
@agent.command("/theme")
async def cmd_theme(ctx: CommandContext) -> str:
    """Get or set UI theme."""
    if ctx.args:
        ctx.session.set("theme", ctx.args.strip())
        return f"Theme set to: {ctx.args.strip()}"
    return f"Current theme: {ctx.session.get('theme', 'default')}"
```

Built-in commands (`/reset`, `/status`, `/compact`, `/model`, `/think`, etc.) are registered via `make_commands(config)` from `selmakit.commands`.

---

### Output Validators

`@agent.output_validator` is a thin passthrough to pydantic-ai's [`Agent.output_validator`](https://ai.pydantic.dev/output/#output-validator-functions) — a post-run hook that inspects the final output and may raise `pydantic_ai.ModelRetry` to force another turn. Use it to bolt a deterministic verification step onto the loop without reaching into the private inner agent (`agent._agent`).

```python
from pydantic_ai import ModelRetry

@agent.output_validator
async def gate(ctx, output: str) -> str:
    if not result_is_plausible(output):
        raise ModelRetry("Result failed the plausibility check — try again.")
    return output
```

Validators run on **final-output** validation, so they fire for `run`, `run_stream`, and `run_stream_events` alike (once per completed turn, not per streamed delta). Purely additive: with no validator registered, nothing changes.

**Gating on what *this* run produced.** A validator's `ctx.messages` is the whole conversation — earlier runs of the same session plus compaction-summarised history — not a run slice. To inspect only the artefacts the current turn produced, use the run-scoped helpers `run_messages(ctx)` and `tool_returns(ctx)` (importable from the package root). They filter `ctx.messages` by the public `run_id` field — the same basis as pydantic-ai's `AgentRunResult.new_messages()` — so no message-layout reconstruction is needed:

```python
from selmakit import run_messages, tool_returns

@agent.output_validator
async def gate(ctx, output: str) -> str:
    # (tool_name, content) for every tool result emitted this run, in call order
    for tool_name, content in tool_returns(ctx):
        if tool_name == "write_file" and not artefact_ok(content):
            raise ModelRetry(f"{tool_name} produced an invalid artefact — redo it.")
    return output
```

`run_messages(ctx)` returns the current run's `ModelMessage`s if you need the full parts. Extracting concrete values (file paths, etc.) from a tool result's `content` stays your job — tool results are application-specific, and selmakit imposes no `output_path` convention.

---

### Scheduled Turns

`@agent.schedule` registers a background `asyncio` task that fires on a fixed interval. The handler returns a prompt string; the agent runs a full turn with it. If the reply contains meaningful content (not just `HEARTBEAT_OK`), it is placed in `agent.alerts` for delivery.

```python
@agent.schedule(
    every="30m",
    active_hours=("08:00", "22:00"),
    timezone="Europe/Berlin",
    target="last",
)
async def check_tasks(ctx: ScheduleContext) -> str:
    return "Check open tasks from memory and report anything urgent. Reply HEARTBEAT_OK if nothing needs attention."
```

Interval syntax: `"30m"`, `"1h"`, `"90s"`. Set `every="0m"` to disable.

`agent.run_schedules()` starts all runners as concurrent `asyncio` tasks. In `gateway.py` it runs alongside channels in `asyncio.gather()`.

---

### Streaming

`run_stream` is an async context manager that yields a pydantic-ai stream result. Slash commands are transparently returned as a single-chunk result so the caller code is uniform:

```python
async with agent.run_stream(prompt, session_key="user:42") as result:
    async for chunk in result.stream_text(delta=True):
        print(chunk, end="", flush=True)
```

`run_stream_events` yields raw pydantic-ai events, enabling tool-call visibility in the UI:

```python
from pydantic_ai.messages import FunctionToolCallEvent, PartDeltaEvent, TextPartDelta

async with agent.run_stream_events(prompt, session_key="user:42") as (is_cmd, value):
    if is_cmd:
        print(value)   # slash command result — plain string
    else:
        async for event in value:
            if isinstance(event, FunctionToolCallEvent):
                print(f"[tool] {event.part.tool_name}")
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                print(event.delta.content_delta, end="", flush=True)
```

Both methods handle the full pre-run pipeline internally:
- Slash command routing (no LLM call)
- `/skill <name>` → converted to `"Execute skill <name>."` prompt
- Stale session detection and reset
- Auto-compaction when session exceeds 50 messages
- `deps=session_key` is passed through so capabilities (e.g. `SessionThinkingCapability`) can read per-session state

Bootstrap-prefix injection moved out of the wrapper: `BootstrapCapability` now emits the hint as an instruction while `BOOTSTRAP.md` exists.

---

### Session Introspection

```python
agent.message_count("user:42")           # number of messages in session
agent.messages_until_compaction("user:42")  # messages remaining before auto-compact
agent.get_tools()                         # {name: description}
agent.get_commands()                      # {name: first docstring line}
agent.get_schedules()                     # [{every, next_run_at}, ...]
```

---

### Manual Compaction

```python
# Flush key facts to memory/YYYY-MM-DD.md, then summarize and replace history
await agent.memory_flush("user:42")
before, after = await agent.compact_session("user:42")
# e.g. 52 → 4 messages
```

`/compact` calls both in sequence. Auto-compaction runs automatically inside `_prepare_run` when the session exceeds 50 messages.

---

## Slash Commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/status` | Model, thinking level, verbose, session, compaction countdown, pending approvals, next heartbeat |
| `/reset` / `/new` | Clear session history |
| `/compact` | Flush facts to memory and summarize session |
| `/model [name]` | Show or set the model (per-session override, applied live) |
| `/models` | List models available at the configured endpoint |
| `/think [off\|low\|medium\|high]` | Show or set thinking level |
| `/verbose [on\|off]` | Show or toggle verbose mode (tool activity + reasoning in the stream) |
| `/mcp [enable\|disable <name>]` | List MCP servers, or toggle one (applies on next restart) |
| `/approve` / `/deny` | Approve or deny a pending gated tool call |
| `/tools` | List registered tools |
| `/skills` | List available skills |
| `/skill <name> [args]` | Execute a skill |
| `/cron` | List active cron jobs |
| `/config` | Show current configuration |
| `/systemprompt` | Show the system prompt as last sent to the model this session |
| `/commands` | List all commands |

---

## Skills

Skills are `SKILL.md` files placed under `.selmakit/workspace/skills/<skill-name>/`.

At each turn the agent receives an XML index of all available skills in the system prompt and selects the most relevant one to read and follow. Skills are lazy-loaded — the LLM only reads a skill file when it decides to execute it.

Example skill frontmatter:

```markdown
---
name: my-skill
description: Does something specific
version: 1
---

# My Skill

Execute immediately when invoked. Do not ask for confirmation.

## Steps
1. ...
2. ...
```

---

## Memory

`SqliteMemory` is an `AbstractCapability` that contributes two tools and a usage hint:

- **`memory_search(query)`** — FTS5 full-text search with optional vector similarity and temporal decay scoring
- **`memory_write(content)`** — appends to today's `memory/YYYY-MM-DD.md` and re-indexes

Score formula (with temporal decay):
```
score = 0.7 × relevance + 0.3 × e^(−λ × age_days)
```

At auto-compaction (> 50 messages), the agent runs a silent `memory_flush()` turn to save important session facts before the history is summarized.

---

## Channels

Channels are opt-in via the `channels` config section. WebChat starts when `channels.webchat.enabled` is true; Telegram starts only when `channels.telegram.enabled` is true **and** `TELEGRAM_TOKEN` is set in the environment (a missing token logs a warning and skips Telegram — no crash). With no channels enabled the gateway still runs schedules and cron.

### WebChatChannel

FastAPI app with SSE streaming.

| Endpoint | Description |
|---|---|
| `POST /webchat/stream` | Send a message, receive SSE stream |
| `GET /webchat/heartbeat/poll` | Poll for pending proactive alerts |

SSE event types: `tool`, `chunk`, `error`, `done`.

### TelegramChannel

Wraps `python-telegram-bot` (v20+). Normalizes incoming messages into the shared queue. Group and supergroup chats get isolated sessions (`group:<id>`).

---

## Heartbeat (Proactive Turns)

The heartbeat scheduler runs a background `asyncio` task on a configurable interval. It calls the agent with `"heartbeat"` as the prompt. If the agent replies with something meaningful (not just `HEARTBEAT_OK`), the reply is queued as an alert and delivered via `GET /webchat/heartbeat/poll` or the next Telegram message.

The `active_hours` window prevents alerts outside working hours.

---

## Session Compaction

When a session exceeds 50 messages:

1. `memory_flush()` runs a silent agent turn to save key facts to `memory/YYYY-MM-DD.md`
2. `compact_session()` asks the agent to summarize the conversation history
3. The summary replaces the full history

This keeps context windows manageable without losing important information.

---

## Tracing

Phoenix (Arize) is used for OpenTelemetry tracing. Run it as a standalone container:

```bash
docker run -d --rm -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest
# UI: http://localhost:6006   OTLP/gRPC: localhost:4317
```

`selmakit/tracing.py` builds an OTel `TracerProvider` with an OTLP/gRPC exporter pointed at `localhost:4317` and calls `pydantic_ai.Agent.instrument_all(InstrumentationSettings(tracer_provider=…, include_content=True))`, which instruments all pydantic-ai spans — not just the HTTP layer. If the OTel SDK is missing, tracing is skipped and the gateway runs unaffected.

**Why a container?** `arize-phoenix` is **not** a Python dependency of selmakit: it pins `pydantic-ai-slim<2` (true even on the latest 17.x), so installing it in the same venv would crash on import under pydantic-ai 2.x. Running Phoenix in its own container keeps the venv clean while selmakit talks to it purely over the OTLP endpoint. Any OTLP collector on `:4317` works as a drop-in replacement.

---

## Project Structure

```
selmakit/
  agent.py          — selmakit.Agent (wraps pydantic_ai.Agent)
  gateway.py        — Gateway runtime + GatewayContext + default_capabilities()
  capabilities.py   — Filesystem/Workspace/Skills/Runtime/Bootstrap/SessionThinking/Mcp capabilities
  commands.py       — slash command handlers + CommandContext
  config.py         — SelmaKitConfig, load_config() with 120s cache
  cron.py           — agent-managed cron jobs (CronCapability/Service/Store)
  memory.py         — MemoryIndex + SqliteMemory capability (FTS5 + vector search)
  message.py        — QueueItem, ReplyHandle
  schedule.py       — ScheduleRunner, ScheduleConfig
  session.py        — JsonlStore
  skills.py         — skill discovery + XML builder
  tools.py          — make_filesystem_tools() (consumed by FilesystemCapability)
  tracing.py        — OTel setup: OTLP/gRPC export to Phoenix (degrades gracefully if OTel SDK missing)
  workspace.py      — workspace file loading + bootstrap detection
  channels/
    webchat.py      — WebChatChannel (FastAPI + SSE)
    telegram.py     — TelegramChannel
  dashboard/
    app.py          — reusable Streamlit app: run(title=, image=, input_placeholder=, …)
    config.py       — DashboardConfig

examples/
  weather_mcp.py    — self-contained reference MCP server (Open-Meteo) for the MCP client
gateway.py          — reference entry point: Gateway.from_config().run()
dashboard.py        — reference entry point: selmakit.dashboard.run(...)
setup.py            — initializes .selmakit/ structure, config, and workspace files
start.sh            — starts Phoenix (Docker) + gateway + dashboard (Linux / macOS)
start.bat           — starts Phoenix (Docker) + gateway + dashboard (Windows)
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `pydantic-ai[duckduckgo,web-fetch]>=2.16.0` | LLM loop, tool calling, streaming, capability framework; the `duckduckgo` and `web-fetch` extras pull in `ddgs` / `markdownify` for the local `WebSearch` / `WebFetch` fallbacks |
| `fastapi` + `uvicorn` | WebChat HTTP/SSE server |
| `python-telegram-bot` | Telegram channel |
| `httpx` | Async HTTP client |
| `streamlit` | Dashboard UI |
| `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` | OpenTelemetry tracing → OTLP/gRPC export (Phoenix runs as a standalone container, not a Python dep — see Tracing section) |
| `python-dotenv` | `.env` loading |
| `rich` | Colored terminal output in `setup.py` |

**Optional extras:**

| Extra | Package | Enables |
|---|---|---|
| `subagents` | `pydantic-ai-harness>=0.10.0` | Sub-agent delegation (`SubAgents` capability). Install with `uv sync --extra subagents`. |

---

## Conclusion

OpenClaw's core architecture — workspace identity, persistent memory, skill routing, scheduled proactive turns, multi-channel delivery — is entirely reproducible with pydantic-ai as the LLM engine. The result is ~800 lines of framework code and a `gateway.py` that fits on one screen.

What commercial platforms add on top: managed hosting, mobile apps, team collaboration, and billing. The agent logic itself is not magic.
