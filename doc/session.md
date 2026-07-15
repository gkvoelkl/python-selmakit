# Sessions & Metadata

How selmakit persists conversation state. Everything here lives under
`<state_dir>/sessions/` (default `.selmakit/sessions/`) and is owned by
`JsonlStore` (`selmakit/session.py`).

## The two files per session

Each session is identified by a `session_key` and stored as **two** sibling files:

| File | Contents | Written by |
|---|---|---|
| `<session_key>.json` | pydantic-ai message history (`list[ModelMessage]`) | `JsonlStore.save()` after every turn |
| `<session_key>.meta.json` | Small JSON dict of session metadata | `JsonlStore.set_meta()` / `touch()` |

They are split on purpose: the history is large and rewritten wholesale each
turn, while the metadata is a handful of tiny keys read cheaply (e.g. by
`is_fresh()` before a run, without deserializing the whole history).

### `session_key` — the external identity

pydantic-ai has no notion of a session; it only takes `message_history=` per
call. The `session_key` is selmakit's glue to the outside world:

- **WebChat / Telegram** — the channel supplies it (chat id, webchat session id).
- **Default** — `"default"` when a caller omits it.
- **Scheduled runs** — `schedule:<8-hex>` for isolated sessions, or the most
  recent user session / `schedule:main` when `isolated_session=False`
  (`selmakit/schedule.py`).

Any string is valid; it becomes the filename stem, so keep it filesystem-safe.

## Message history file (`<session_key>.json`)

Serialized with a pydantic `TypeAdapter(list[ModelMessage])` — i.e. the exact
pydantic-ai message objects (`ModelRequest` / `ModelResponse` and their parts).

**The rendered system prompt is *not* stored here.** pydantic-ai attaches the
assembled instructions only to the latest `ModelRequest` in memory, and selmakit
does not keep them in the persisted history — it would bloat every saved file
with a re-derivable block. See [`last_system_prompt`](#last_system_prompt) for
how the prompt is captured instead.

Lifecycle:

- `load(session_key)` — returns `[]` when the file is missing.
- `save(session_key, messages)` — full overwrite with `all_messages()` from the run.
- `clear(session_key)` — deletes the `.json` only (history reset), leaving meta.

## Metadata file (`<session_key>.meta.json`)

A flat JSON object. Missing file → treated as `{}`. Known keys:

| Key | Type | Written by | Read by | Meaning |
|---|---|---|---|---|
| `last_interaction_at` | ISO-8601 UTC | `touch()` after each turn | `is_fresh()`, `list_sessions()` | Timestamp of the last turn; drives stale detection. |
| `thinking` | `off`/`low`/`medium`/`high` | `/think` command | `SessionThinkingCapability` | Per-session reasoning effort. Absent ⇒ falls back to the capability default. |
| `verbose` | bool | `/verbose on\|off` command | `Gateway._worker`, `/status` | When true, the webchat stream surfaces tool calls (`→ name(args)`), results (`← name: …`), tool errors, per-tool timing and reasoning deltas. Absent ⇒ off. |
| `pending_approvals` | list of `{tool_call_id, tool_name, args}` \| null | `Agent._finalize_run` (set when a turn defers, cleared otherwise) | `Gateway._worker` (emits `approval` event), `Agent._prepare_approval_resume`, `/status` | Gated MCP tool calls awaiting `/approve` or `/deny`. Present ⇒ the last turn ended in a `DeferredToolRequests`. See CLAUDE.md "Tool approval". |
| `last_system_prompt` | string | after each **user** turn | `/systemprompt`, `Agent.last_system_prompt()` | The instructions string as last actually sent to the model. |
| `model_override` | string | `/model <name>` | `/model`, `/status` | Recorded/displayed per session. **Note:** not currently consumed by the run loop — model dispatch still uses the configured model. |
| `session_type` | `user`/`schedule` | schedule runner (sets `schedule` on isolated sessions) | `list_sessions()`, cron/schedule targeting | Distinguishes user chats from scheduled/heartbeat sessions. Defaults to `user` when absent. |

Metadata is a plain dict — custom slash commands may add their own keys via
`ctx.session.set(key, value)` (see [SessionProxy](#sessionproxy)).

### `last_system_prompt`

There is no static system prompt in selmakit — pydantic-ai assembles it each run
from the capabilities' `get_instructions()` fragments (workspace files, skills,
runtime info, …), so it changes as files on disk change. To make the *effective*
prompt inspectable without re-rendering it:

1. After each **user-facing** turn (`run_stream` / `run_stream_events`),
   `Agent._extract_instructions()` pulls the `instructions` string from the
   in-memory run result (the latest request that carries one).
2. It is cached via `set_meta(session_key, "last_system_prompt", …)`.

Retrieve it:

- **Programmatically:** `agent.last_system_prompt(session_key="default") -> str | None`
- **Interactively:** the `/systemprompt` slash command (a thin wrapper over the above).

Both return `None` / a "send a message first" hint before the session's first
LLM turn. **Only user turns update the cache** — heartbeat, cron, and compaction
runs bypass these entry points by design, so `/systemprompt` reflects the
interactive session, not an isolated background run.

## Stale detection & auto-reset (`is_fresh`)

Before each run, `_prepare_run()` calls `is_fresh(session_key)`; a stale session
is `clear()`-ed so the turn starts fresh. Two independent rules, configured on
the store (from `config.session.reset`, `selmakit/config.py`):

- **Daily reset — `at_hour`** (default `4`): stale if `last_interaction_at` is
  before today's `at_hour` local-time boundary. A conversation naturally resets
  once per day in the early morning.
- **Idle reset — `idle_minutes`** (default `None` = disabled): stale if more than
  `idle_minutes` have elapsed since the last interaction.

A session with no `last_interaction_at` (never used, or meta unparsable) is
always considered fresh.

## Compaction interplay

Independent of reset, histories over `_MAX_MESSAGES_BEFORE_COMPACT` (50) are
pre-compacted before the turn (`selmakit/agent.py`): `memory_flush()` writes key
facts to `memory/YYYY-MM-DD.md`, then `compact_session()` replaces the history
with a summary. This rewrites `<session_key>.json` but leaves the metadata
(including `last_system_prompt`) intact.

## Enumerating sessions

`list_sessions(session_type=None)` scans the directory, skips `*.meta.json`
files, and returns one dict per session sorted by `last_interaction_at`
(newest first):

```python
{"session_key", "session_type", "last_interaction_at", "thinking"}
```

Pass `session_type="user"` or `"schedule"` to filter — used by the cron and
schedule runners to target real user chats.

## API reference

### `JsonlStore` (`selmakit/session.py`)

```python
JsonlStore(path, max_tokens=50_000, compaction_strategy="none",
           at_hour=4, idle_minutes=None)
```

| Method | Purpose |
|---|---|
| `load(key) -> list[ModelMessage]` | Read history (`[]` if none). |
| `save(key, messages)` | Overwrite history. |
| `clear(key)` | Delete history file (keeps meta). |
| `get_meta(key, name, default=None)` | Read one meta key. |
| `set_meta(key, name, value)` | Write one meta key (merges). |
| `touch(key)` | Set `last_interaction_at = now (UTC)`. |
| `is_fresh(key) -> bool` | Apply the reset rules above. |
| `list_sessions(session_type=None)` | Enumerate sessions. |

`from_file()` builds the store with `at_hour`/`idle_minutes` from
`config.session.reset`; a bare `Agent()` uses the defaults.

### SessionProxy (`selmakit/commands.py`)

Handed to every slash-command handler as `ctx.session`. A thin per-session view
over the same `.meta.json`, plus session control:

| Method | Purpose |
|---|---|
| `invalidate()` | Delete **both** `.json` and `.meta.json` (full reset). |
| `get(key, default=None)` | Read a meta key. |
| `set(key, value)` | Write a meta key. |

Note the asymmetry: `JsonlStore.clear()` drops only history; `SessionProxy.invalidate()`
(used by `/reset` and `/new`) drops history *and* metadata.

## On disk

```
.selmakit/sessions/
  default.json            — message history
  default.meta.json       — { last_interaction_at, thinking, last_system_prompt, … }
  telegram:12345.json
  telegram:12345.meta.json
  schedule:a1b2c3d4.json
  schedule:a1b2c3d4.meta.json   — { session_type: "schedule", … }
```
