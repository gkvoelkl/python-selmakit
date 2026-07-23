# selmakit — Architecture & Design

This document explains **how** and **why** `selmakit` is built. For installation, configuration, and usage, see [`README.md`](../README.md).

---

## Motivation

`selmakit` exists to answer a single question:

> *Can you rebuild a commercial multi-channel agent platform (workspace identity, persistent memory, skill routing, scheduled proactive turns, multi-channel delivery) yourself in Python with open-source components?*

The answer is **yes** — and the result is roughly 1000 lines of framework code.

[pydantic-ai](https://github.com/pydantic/pydantic-ai) handles the LLM loop with type safety: tool calling, streaming, message handling. It deliberately leaves out everything around that loop:

| Concern | Status in pydantic-ai |
|---|---|
| Scheduling / proactive turns | Absent |
| Session storage on disk | Serialization yes, storage no |
| Session concurrency / locking | Absent |
| Channel adapters (Telegram, WebChat) | Absent |
| Slash command routing | Absent |
| Long-term memory / RAG | Absent |
| HTTP gateway / SSE | Absent |
| Workspace file injection (SOUL.md, IDENTITY.md, …) | Absent |
| Bootstrap / first-run onboarding | Absent |

`selmakit` fills exactly these gaps as a thin layer around a real `pydantic_ai.Agent`.

---

## Design Principles

### 1. Capability-first

Anything that contributes to the LLM context — tools, instructions, model settings — lives in a `pydantic_ai.capabilities.AbstractCapability` subclass. The `Agent` constructor takes them as a single list:

```python
Agent(model=…, capabilities=[FilesystemCapability(…), WebSearch(…), …])
```

Adding a new context source = writing a new capability. No threading kwargs through layers, no monkey-patching system prompts.

### 2. selmakit.Agent is a *thin* wrapper

`selmakit.Agent` is not an `AbstractAgent` subclass. It composes `pydantic_ai.Agent` and adds only what *cannot* be a capability:

- Slash-command dispatch (streaming complications when handled via `wrap_run`)
- Session-key-driven load/save around `run_stream` (session_key is an *outside-pydantic-ai* concept)
- Heartbeat scheduler (runs outside the LLM loop entirely)
- `state_dir` / `workspace_dir` path convention

Everything else delegates to pydantic-ai or to capabilities.

### 3. One queue, one worker

Both channels (WebChat, Telegram) write into a shared `asyncio.Queue[QueueItem]` owned by the `Gateway`. A single `Gateway._worker()` coroutine dequeues and calls `agent.run_stream_events()`. This serialises agent turns per process — no concurrency issues with the SQLite memory index or session files.

### 4. Disk-first state

All runtime state lives under `.selmakit/`. Sessions, memory, workspace files, config. The Streamlit dashboard reads/writes the same files directly — no internal HTTP for config changes. The trade-off is acceptable because gateway and dashboard always run on the same machine.

### 5. Library-first runtime

The gateway and dashboard are not just scripts — they are importable library components. `selmakit.Gateway` wires an agent to its channels, worker, schedules and cron; `selmakit.dashboard.run()` renders the chat UI. A custom agent is two thin reference files (`gateway.py`, `dashboard.py`) that configure these components rather than reimplementing them:

```python
Gateway.from_config(extra_capabilities=[MyCapability(...)]).run()   # backend
run(title="…", image="…", input_placeholder="…")                    # frontend
```

Capabilities that need an internal object (session store, cron store, …) receive a `GatewayContext` via a `capabilities=lambda ctx: [...]` factory; `default_capabilities(ctx)` returns the standard set.

---

## Architecture Overview

```
                       ┌──────────────────────────────────────────┐
                       │       Gateway  (selmakit/gateway.py)     │
                       │                                          │
   WebChat ─────┐      │   asyncio.Queue[QueueItem] ─► _worker()──┼──► selmakit.Agent
                ├──────►                                          │         │
   Telegram ────┘      │                                          │         ▼
                       │                                          │   pydantic_ai.Agent
                       │   run_schedules()  ── heartbeat ──┐      │         │
                       │   cron_service.run() ── cron ─────┘      │   ┌─────┴──────┐
                       └──────────────────────────────────────────┘   │ capabilities│
                                                                      │ + toolsets  │
                                                                      └─────┬───────┘
                                                                            ▼
                                                                          LLM
```

`Gateway.serve()` runs the enabled channels, `_worker()`, `agent.run_schedules()` (heartbeat) and `cron_service.run()` together in one `asyncio.gather()`. Channels are opt-in: WebChat starts when `channels.webchat.enabled`; Telegram only when `channels.telegram.enabled` **and** `TELEGRAM_TOKEN` is set.

### Layers, top-down

0. **Gateway** (`selmakit.gateway.Gateway`)
   The composition root. `Gateway.from_config()` reads config and builds the model, session store, memory and cron store; the constructor resolves capabilities (defaults + `extra_capabilities`, or a `capabilities` list/factory), builds the agent, the enabled channels, the queue and the cron service. `run()`/`serve()` drives everything. The top-level `gateway.py` is just `Gateway.from_config().run()`.

1. **Channels** (`selmakit.channels.WebChatChannel`, `TelegramChannel`)
   Translate external protocols (SSE, Telegram updates) into `QueueItem(session_key, prompt, reply)` objects. Each is opt-in via the `channels` config section.

2. **Worker** (`Gateway._worker()`)
   Single async loop: `item = await queue.get()` → `agent.run_stream_events(item.prompt, session_key=item.session_key)` → stream events back via `item.reply`.

3. **selmakit.Agent** (`selmakit.agent.Agent`)
   Owns session_store, heartbeat, slash commands, auto-compaction. Wraps pydantic_ai.Agent.

4. **pydantic_ai.Agent** (constructed inside selmakit.Agent)
   Owns the actual LLM loop and the capability tree.

5. **Capabilities** (selmakit-shipped + pydantic-ai built-ins)
   Contribute toolsets, instructions, model settings. Composed by pydantic-ai into a `CombinedCapability`.

---

## The Capability System

A capability is a `@dataclass` subclass of `pydantic_ai.capabilities.AbstractCapability[DepsType]`. It can contribute up to three things to the agent's run-time behaviour:

### Configuration methods (evaluated at agent construction)

| Method | Returns | Purpose |
|---|---|---|
| `get_toolset()` | `AgentToolset \| None` | A `FunctionToolset` with tools (local execution) |
| `get_native_tools()` | `Sequence[AgentNativeTool]` | Provider-native tools (e.g. Anthropic's web search) |
| `get_instructions()` | `str` or `(RunContext) → str` | A fragment of the system prompt; callable = dynamic per run |
| `get_model_settings()` | `ModelSettings` or `(RunContext) → ModelSettings` | Per-run model settings (e.g. `thinking`) |
| `get_wrapper_toolset(toolset)` | `AbstractToolset \| None` | Intercept *all* tool calls (logging, validation) |

### Lifecycle hooks (evaluated per run)

| Phase | Hooks |
|---|---|
| Run boundary | `before_run` (observe-only), `wrap_run`, `after_run`, `on_run_error` |
| Model request | `before_model_request`, `wrap_model_request`, `after_model_request`, `on_model_request_error` |
| Tool validation | `before_tool_validate`, `wrap_tool_validate`, `after_tool_validate`, `on_tool_validate_error` |
| Tool execution | `before_tool_execute`, `wrap_tool_execute`, `after_tool_execute`, `on_tool_execute_error` |
| Output | `before_output_*`, `wrap_output_*`, `after_output_*`, `on_output_*_error` |
| Event stream | `wrap_run_event_stream` |

Composition rules: `before_*` fires in declared order, `after_*` fires in reverse, `wrap_*` nests middleware-style.

### State isolation

`for_run(ctx)` returns a fresh capability per run if the capability holds mutable state. Default returns `self`.

### Shipped capabilities in selmakit

| Capability | Methods used | What it contributes |
|---|---|---|
| `FilesystemCapability` | `get_toolset` | `read`/`write`/`edit`/`ls`/`grep`/`find` bound to a `cwd` |
| `SqliteMemory` | `get_toolset`, `get_instructions` | `memory_search`/`memory_write` + usage hint |
| `WorkspacePromptCapability` | dynamic `get_instructions` | Loads MD files from workspace each run |
| `SkillsPromptCapability` | dynamic `get_instructions` | `<available_skills>` XML block + selection rules |
| `RuntimeInfoCapability` | dynamic `get_instructions` | `host / os / model / date` one-liner |
| `BootstrapCapability` | dynamic `get_instructions` | Onboarding hint while `BOOTSTRAP.md` has content |
| `SessionThinkingCapability` | dynamic `get_model_settings` | Reads `"thinking"` from session meta via `ctx.deps` |
| `McpCapability` | `get_toolset` | One `MCPToolset` per configured MCP server (stdio/HTTP), merged into a `CombinedToolset`; optional `prefix`/`allow_tools`/`require_approval` (see [Tool approval](#tool-approval-deferred-tools)) |

Plus the pydantic-ai-shipped provider-adaptive tools:

| Capability | Notes |
|---|---|
| `WebSearch(local="duckduckgo")` | Native on supporting providers, DuckDuckGo fallback |
| `WebFetch(local=True)` | Native on supporting providers, markdownify fallback |

And one **optional** capability borrowed from [pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness) (the official capability library — complementary to selmakit, which is the surrounding *runtime*):

| Capability | Notes |
|---|---|
| `SubAgents` (`pydantic_ai_harness.subagents`) | `delegate_task(agent_name, task)` runs a named sub-agent in isolation. Built from the `subagents` config by `build_subagents_capability()` (lazy import), added to `default_capabilities` when enabled. Needs the `subagents` extra. This is the template for adopting further harness capabilities (`shell`, `planning`, richer `compaction`) instead of hand-rolling them. |

### Walking the capability tree

`agent._agent.root_capability.apply(visitor)` traverses the full composed tree, including pydantic-ai's internal built-ins (`ToolSearch`, `PendingMessageDrainCapability`).

`Agent.get_tools()` recurses through `agent._agent.toolsets` to enumerate every registered function tool — directly-passed, capability-provided, and wrapper-nested. Used by `/tools` and for status display.

---

## What stays in `selmakit.Agent` (and why not a capability)

The capability system is great for things that fit inside pydantic-ai's run lifecycle. Some selmakit features sit *outside* it and stay in the wrapper class:

### Slash commands (`/reset`, `/status`, `/think`, `/verbose`, `/mcp`, `/approve`, `/skill`, …)

Slash commands are intercepted **before** the LLM is called. `agent.run_stream()` checks if the prompt starts with `/`, dispatches to the handler, and yields a synthetic stream result. Two commands are special-cased even earlier in `_prepare_run`: `/skill` rewrites the prompt into an "Execute skill" turn, and `/approve`/`/deny` resume a deferred tool-approval run (see [Tool approval](#tool-approval-deferred-tools)) rather than reply.

In principle, `wrap_run` could host this. In practice, streaming (`run_stream`/`run_stream_events`) returns a context-manager / async generator structure that's awkward to substitute via `wrap_run`. The wrapper class is simpler and explicit.

### Session persistence (`JsonlStore`)

The `session_key` is an external identity (Telegram chat id, webchat session). pydantic-ai has no concept of it. Selmakit's wrapper loads the message history before each run and saves the result after — this is glue between the outside world and pydantic-ai's `message_history=` parameter. A capability would hide the coupling, not remove it.

### Auto-compaction trigger

The 50-message threshold check + `memory_flush()` + `compact_session()` sequence runs in `_prepare_run`. It's tied to the session_key (which messages to inspect, which file to compact). Could be a `before_run` capability *if* session_key were in `RunContext.deps`, but the resulting code would be split across two files for no real win.

### Heartbeat scheduler

Runs entirely outside the LLM loop. `ScheduleRunner` is an `asyncio` task that triggers a regular agent turn on a fixed interval. Not a capability — capabilities only fire during an active run.

### State directory convention

`state_dir` and the derived `workspace_dir` are selmakit conventions. pydantic-ai has no notion of them. The wrapper class owns these paths and exposes them to capabilities through constructor injection (each capability that needs `workspace_dir` takes it as a constructor parameter).

---

## MCP Servers & Tool Approval

### The MCP client

`McpCapability` (`capabilities.py`) attaches external [MCP](https://modelcontextprotocol.io) servers as tools. It reads the `mcp` section of `selmakit.json` — standard `mcpServers` fields plus selmakit extras — and, per enabled server, builds an `MCPToolset` over an explicit `StdioTransport` (`command`/`args`/`env`/`cwd`) or `StreamableHttpTransport` (`url`/`headers`). Modifiers are chained in order: `allow_tools` → `filtered()`, `require_approval` → `approval_required()`, `prefix` → `prefixed()`. All servers merge into one `CombinedToolset`. `${VAR}` in `env`/`headers` is expanded from the environment so secrets stay out of the JSON. The capability is added to `default_capabilities` only when `mcp.enabled` and at least one server is configured.

The underlying `fastmcp` client ships with pydantic-ai's mcp extra (no extra install). Connections are held open for the gateway's lifetime: `Gateway.serve()` wraps its `asyncio.gather` in `async with self.agent:`, and `Agent.__aenter__`/`__aexit__` delegate to the pydantic-ai agent context, which starts all `MCPToolset`s once. Entering is a no-op if already entered, so per-run streaming calls are unaffected; a standalone `agent.run(...)` outside the context still auto-connects per run.

Runtime management is via the `/mcp` command: list servers with their state, or `/mcp enable|disable <name>` to patch `selmakit.json`. Because toolsets are built once at startup, a toggle takes effect on the next gateway restart (the reply says so).

### Tool approval (deferred tools)

A server configured with `require_approval: true` has its tool calls gated behind human approval, built on pydantic-ai's deferred-tools API (2.9+). The `Agent` is constructed with `output_type=[str, DeferredToolRequests]`; when the model calls a gated tool the run **ends with a `DeferredToolRequests` output instead of executing it** (a normal turn's output stays a plain `str`).

`Agent._finalize_run` detects that output and records the pending calls in the session's `pending_approvals` meta (`[{tool_call_id, tool_name, args}]`); `Gateway._worker` then emits an `approval` SSE event. The decision comes back as an ordinary turn — `/approve` or `/deny` (the dashboard's ✅/🚫 buttons send exactly these). `_prepare_run` intercepts them and, via `_prepare_approval_resume`, resumes the deferred run with a `DeferredToolResults` (`True` to approve, `ToolDenied` to deny) and **no new user prompt** (`effective_prompt=None`). Approving executes the tool; denying tells the model it was rejected. A resume can defer again (chained approvals), re-setting `pending_approvals`.

**Unattended runs never wait for a human.** Heartbeat and cron call `run_stream(..., unattended=True)`; `_run_unattended_autodeny` runs non-streamed and auto-denies any gated call in a loop (capped by `_MAX_APPROVAL_ITERATIONS`), so a gated tool is hard-blocked rather than hanging the schedule.

---

## Message Flow (One User Turn)

1. **Channel receives a message** — `WebChatChannel.stream` (FastAPI handler) or `TelegramChannel._on_message` (PTB handler).
2. **`QueueItem` enqueued** — `await queue.put(QueueItem(session_key, prompt, reply))`. The `reply` handle abstracts how to stream output back to the originating channel.
3. **Worker dequeues** — `Gateway._worker()` calls `async with agent.run_stream_events(prompt, session_key=…) as (is_cmd, value): …`.
4. **Pre-run pipeline** (in `selmakit.Agent._prepare_run`):
   - If prompt is `/approve`/`/deny`: resume the deferred run with a `DeferredToolResults` (no new prompt) — see [Tool approval](#tool-approval-deferred-tools).
   - If prompt starts with `/skill <name>`: rewrite to `"Execute skill <name>."`
   - Else if prompt starts with `/`: dispatch to slash-command handler → return `(True, text)` and skip LLM call.
   - Stale-session check (`JsonlStore.is_fresh`) → clear session if expired.
   - Load message history from `.selmakit/sessions/<session_key>.json`.
   - If history > 50 messages: run `memory_flush()` + `compact_session()`.
   - Build kwargs: `message_history`, `deps=session_key` (+ `deferred_tool_results` on a resume, + per-run `model` on a live `/model` override).
5. **LLM run** — `pydantic_ai.Agent.run_stream_events(prompt, message_history=…, deps=…)`.
   - pydantic-ai assembles the system prompt by concatenating each capability's `get_instructions()` contribution.
   - `SessionThinkingCapability.get_model_settings()` runs; if session meta has `"thinking": "high"`, `thinking="high"` flows into the model request.
   - Tool calls flow as `FunctionToolCallEvent` → forwarded to the channel as SSE `tool` events. When the session's `verbose` flag is on, `Gateway._worker` also forwards args, `FunctionToolResultEvent` (as `tool_result`, with timing), and `ThinkingPart` deltas (as `thinking`).
   - Text deltas flow as `TextPartDelta` → SSE `chunk` events.
6. **Post-run** (`Agent._finalize_run`) — `result.all_messages()` saved to disk, session `touch()`ed, `last_system_prompt` cached. If the run ended in a `DeferredToolRequests` (a gated tool awaiting approval), the pending calls are recorded in the `pending_approvals` meta key and the worker emits an `approval` SSE event; otherwise `pending_approvals` is cleared.

The agent is entered once at gateway startup (`async with self.agent:` in `Gateway.serve()`), so `MCPToolset` connections are opened a single time and held for the gateway's lifetime rather than re-established per run.

---

## Migration Story: pydantic-ai 1.x → 2.0

selmakit was originally built against pydantic-ai 1.94.0. Migration to 2.0 (beta) reshaped the architecture (the project now tracks **2.16.x**, which added the deferred-tools API that [Tool approval](#tool-approval-deferred-tools) builds on):

| Before (1.x) | After (2.0) | Mechanism |
|---|---|---|
| `web_search` / `web_fetch` as plain function tools (selmakit-implemented) | `WebSearch(local="duckduckgo")` / `WebFetch(local=True)` | `NativeOrLocalTool` capability |
| `make_system_prompt(workspace, tools, …)` registered via `@agent.system_prompt(dynamic=True)` | Three capabilities: `WorkspacePromptCapability`, `SkillsPromptCapability`, `RuntimeInfoCapability` | dynamic `get_instructions()` |
| `BOOTSTRAP.md` prefix appended to user message string in `_prepare_run` | `BootstrapCapability` injects a hint into instructions | dynamic `get_instructions()` |
| `make_filesystem_tools(".")` spread into `tools=[…]` | `FilesystemCapability(cwd=".")` in `capabilities=[…]` | `get_toolset()` |
| `SqliteMemory` constructed with no workspace, then late-bound via `_attach()` | `SqliteMemory` is a real capability with `workspace_dir` as constructor field | `AbstractCapability` subclass |
| `supports_thinking` heuristic (URL sniffing for Ollama detection) | Removed; `SessionThinkingCapability` reads session meta | `get_model_settings()` callable |
| Ollama `extra_body={"options":{"think": False}}` workaround | Removed; pydantic-ai 2.0 handles provider-specific thinking knobs | — |

Roughly 150 lines of selmakit code were deleted; in exchange, capabilities make extension genuinely composable.

### Known incompatibility: Phoenix tracing

`arize-phoenix` pins `pydantic-ai-slim<2` (still true on the latest 17.x), so installing it in the same venv would crash on import under pydantic-ai 2.x. It is therefore deliberately **not** a Python dependency of selmakit — Phoenix runs as a standalone Docker container and selmakit talks to it purely over the OTLP endpoint. `selmakit/tracing.py` sets up the OTel SDK directly and skips instrumentation with a warning if the OTel exporter is missing; the gateway runs unaffected either way.

### Breaking changes worth knowing

- `OpenAIModel` was split into `OpenAIChatModel` (Chat Completions, used by Ollama) and `OpenAIResponsesModel` (OpenAI's new Responses API).
- `agent.toolsets` is a list of `_AgentFunctionToolset` + user-supplied toolsets + capability-provided `CombinedToolset`. The old private `_function_toolset` attribute is gone.
- `end_strategy="graceful"` is now the default — function tools requested alongside output tools execute instead of being skipped.

---

## Dashboard Communication

The Streamlit dashboard (`selmakit/dashboard/`, started via a thin `dashboard.py` that calls `selmakit.dashboard.run(...)`) communicates with the gateway in three ways. The stream and poll URLs are derived from `DashboardConfig.gateway_base_url`:

### Chat via HTTP/SSE

```
POST /webchat/stream  →  SSE  →  events: tool, chunk, error, done (+ approval; + tool_result, thinking when verbose)
```

Handled by `WebChatChannel`. The SSE event schema is a stable contract:

| Event | Payload | Meaning |
|---|---|---|
| `tool` | `{"name": "memory_search"}` (+ `"args"` when verbose) | Tool call started |
| `chunk` | `{"text": "…"}` | Text delta |
| `tool_result` | `{"name", "result", "duration", "error"}` | Tool returned (verbose only) |
| `thinking` | `{"text": "…"}` | Reasoning delta (verbose only) |
| `approval` | `{"pending": [{"tool_call_id", "tool_name", "args"}]}` | Gated tool call(s) awaiting `/approve`/`/deny` |
| `error` | `{"message": "…"}` | Run failed |
| `done` | `{}` | Stream complete |

The client-side httpx read timeout for this stream is `DashboardConfig.stream_timeout` (default `120.0` s). A turn that produces no SSE event for longer than this — e.g. a long-running QGIS/STAC job — raises a `ReadTimeout` in the dashboard only (the CLI and the gateway itself are unaffected). Raise `stream_timeout` or set it to `None` to disable the timeout for such workloads.

### Config via filesystem

The dashboard reads and writes `.selmakit/selmakit.json` directly. No HTTP endpoint. Changes are picked up by the gateway after the 120-second `load_config()` cache expires.

### Heartbeat alerts

```
GET /webchat/heartbeat/poll  →  {"alert": "..."} or {"alert": null}
```

The dashboard polls this endpoint to surface proactive turns from the heartbeat scheduler.

---

## State Directory Layout

```
.selmakit/
  selmakit.json          — config (cached 120s by load_config; incl. mcp servers)
  sessions/
    <session_key>.json   — message history (JSONL via TypeAdapter)
    <session_key>.meta.json — thinking, verbose, last_interaction_at, model_override, pending_approvals
  cron/
    jobs.json            — agent-managed cron jobs (CronStore)
  workspace/
    SOUL.md              — agent personality (free-form)
    IDENTITY.md          — agent identity (free-form)
    USER.md              — user context (free-form)
    HEARTBEAT.md         — instructions for proactive turns
    BOOTSTRAP.md         — first-run onboarding (clear content when done)
    memory/
      YYYY-MM-DD.md      — daily memory files (LLM-written)
    skills/
      <skill-name>/
        SKILL.md         — skill instructions
        <other files>    — skill resources
  memory.db              — SQLite FTS5 index (rebuilt from memory/*.md)
```

`load_workspace_files(workspace_dir)` reads MD files in fixed order: `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `TOOLS.md`, `MEMORY.md`, then today's + yesterday's daily memory, then `BOOTSTRAP.md` last. Empty files are skipped.

---

## Module Structure

```
selmakit/
  __init__.py           — public exports
  agent.py              — selmakit.Agent (thin wrapper around pydantic_ai.Agent)
  gateway.py            — Gateway composition root + GatewayContext + default_capabilities()
  capabilities.py       — Filesystem/Workspace/Skills/Runtime/Bootstrap/SessionThinking/Mcp capabilities
  commands.py           — slash-command handlers + CommandContext + SessionProxy
  config.py             — SelmaKitConfig (Pydantic) + load_config() with 120s cache
  cron.py               — agent-managed cron jobs (CronCapability/CronService/CronStore)
  memory.py             — MemoryIndex (FTS5 + vector) + SqliteMemory capability
  message.py            — QueueItem, ReplyHandle
  schedule.py           — ScheduleRunner, ScheduleConfig, interval parser
  session.py            — JsonlStore (load/save, freshness, meta)
  skills.py             — skill discovery + XML builder
  tools.py              — make_filesystem_tools() — consumed by FilesystemCapability
  tracing.py            — Phoenix OTel setup (degrades gracefully)
  workspace.py          — load_workspace_files() + detect_bootstrap()
  channels/
    __init__.py
    webchat.py          — WebChatChannel (FastAPI + SSE + heartbeat poll)
    telegram.py         — TelegramChannel (python-telegram-bot v22+)
  dashboard/
    __init__.py         — exports run() + DashboardConfig
    app.py              — reusable Streamlit app: run(title=, image=, input_placeholder=, …)
    config.py           — DashboardConfig (branding + gateway_base_url)

examples/
  weather_mcp.py        — self-contained reference MCP server (Open-Meteo) for the MCP client
gateway.py              — reference entry point: Gateway.from_config().run()
dashboard.py            — reference entry point: selmakit.dashboard.run(...)
setup.py                — initializes .selmakit/ structure on first run
start.sh                — boots Phoenix + gateway + dashboard
```

The framework code, including the `Gateway` runtime and the reusable dashboard, lives entirely under `selmakit/`. The top-level `gateway.py` and `dashboard.py` are thin reference entry points (a handful of lines each) — Selma is built with them.

---

## Where the abstractions end

selmakit is intentionally **not** trying to be a full agent platform. Things that stay project-specific:

- **System-prompt content** — SOUL.md, IDENTITY.md, USER.md are user-curated text. selmakit injects them; the user writes them.
- **Skills** — selmakit discovers them and emits the index. The skill MD files themselves are the user's responsibility.
- **Tool implementations** — selmakit ships filesystem and web tools. Domain-specific tools (calendar, mail, custom APIs) are added by the user as new capabilities or `tools=` entries.
- **Channel auth** — Telegram token, optional WebChat auth headers, OAuth flows for external services. Stays in `.env` / project code.

If you find yourself wanting to "configure" something that's really just code, write the code instead. selmakit prefers composable Python over declarative YAML.
