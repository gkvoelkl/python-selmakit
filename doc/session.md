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

**The rendered system prompt *is* stored here**, on every `ModelRequest` that
carries one. `save()` writes `all_messages()` verbatim, so each request repeats
the full assembled instructions block (workspace files, skills, runtime info —
easily several KB). Nothing strips it. Expect session files to grow accordingly
until auto-compaction replaces the history. The same string is *also*
cached in the metadata as [`last_system_prompt`](#last_system_prompt), which is
what `/systemprompt` reads — the meta copy is the supported accessor, not the
per-message ones.

Lifecycle:

- `load(session_key)` — returns `[]` when the file is missing.
- `save(session_key, messages)` — full overwrite with `all_messages()` from the run.
- `clear(session_key)` — deletes the `.json` only (history reset), leaving meta.

### Reading session files from outside the gateway

Trace readers, benchmarks and graders want the same files without constructing
a `JsonlStore` (which creates the directory and carries reset policy it has no
use for). Four module-level functions in `selmakit.session`, re-exported at the
package root, are the supported entry:

| Function | Returns |
|---|---|
| `load_session_messages(sessions_dir, session_key)` | The messages as **plain dicts**; `[]` when the file is missing or unreadable. |
| `load_session_meta(sessions_dir, session_key)` | The metadata dict; `{}` when missing or unreadable. |
| `session_file(sessions_dir, session_key)` | `Path` of the message file. |
| `session_meta_file(sessions_dir, session_key)` | `Path` of the metadata file. |

Dicts, not `ModelMessage` objects, on purpose: a reader wants a handful of
fields and should keep working when the message schema gains a part type. Use
`JsonlStore.load()` when you want the validated objects instead.

Opening `sessions/<key>.json` by hand works and will keep working until it does
not — the point of going through these is that a layout change then breaks the
*call*, which an import or a test catches, rather than the analysis built on it.

## Metadata file (`<session_key>.meta.json`)

A flat JSON object. Missing file → treated as `{}`. Known keys:

| Key | Type | Written by | Read by | Meaning |
|---|---|---|---|---|
| `last_interaction_at` | ISO-8601 UTC | `touch()` after each turn | `is_fresh()`, `list_sessions()` | Timestamp of the last turn; drives stale detection. |
| `thinking` | `off`/`low`/`medium`/`high` | `/think` command | `SessionThinkingCapability` | Per-session reasoning effort. Absent ⇒ falls back to the capability default. |
| `verbose` | bool | `/verbose on\|off` command | `Gateway._worker`, `/status` | When true, the webchat stream surfaces tool calls (`→ name(args)`), results (`← name: …`), tool errors, per-tool timing and reasoning deltas. Absent ⇒ off. |
| `pending_approvals` | list of `{tool_call_id, tool_name, args}` \| null | `Agent._finalize_run` (set when a turn defers, cleared otherwise) | `Gateway._worker` (emits `approval` event), `Agent._prepare_approval_resume`, `/status` | Gated MCP tool calls awaiting `/approve` or `/deny`. Present ⇒ the last turn ended in a `DeferredToolRequests`. See CLAUDE.md "Tool approval". |
| `last_system_prompt` | string | after each **user** turn | `/systemprompt`, `Agent.last_system_prompt()` | The instructions string as last actually sent to the model. |
| `last_validated_output` | string | `Agent._finalize_run`, when the turn ended in text | `Agent.last_validated_output()` | The final output **after** output validators — what the user was given, which the message history does not hold. See [below](#last_validated_output). |
| `model_override` | string | `/model <name>`, dashboard model selector | `Agent._resolve_run_model()`, `/model`, `/status` | Per-session live model switch. Consumed by the run loop: `_prepare_run` passes the built model as pydantic-ai's per-run `model=`. Interactive turns only — heartbeat/cron/compaction runs always use the base model. See CLAUDE.md "Live model switching". |
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

### `last_validated_output`

`<session_key>.json` holds **what the model said**. When an
[`@agent.output_validator`](../CLAUDE.md) rewrites or annotates the answer, that
is *not* what the user was given: a pydantic-ai output validator transforms the
run's output value and never writes back into the message history, so the
`ModelResponse` keeps the unvalidated text. This is by design upstream, and no
amount of reordering the save changes it — the two simply are different values.

So `_finalize_run` stores the validated output alongside the history:

- **The history** answers "what did the model produce?" — the right basis for
  replaying a run or debugging the model.
- **This key** answers "what did the user actually read?" — the right basis for
  a trace reader, a run log, or an LLM judge grading the delivered answer.

Read it with `agent.last_validated_output(session_key="default") -> str | None`.
It is `None` before the first LLM turn and for a turn that ended awaiting tool
approval (a `DeferredToolRequests` output is not text). With no validator
registered it simply equals the final text, so consumers need no special case.

Stream consumers can also take the value straight from the run: since
`run_stream_events` forwards `AgentRunResultEvent`, `event.result.output` carries
the validated output live, without waiting for the turn to be persisted.

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
