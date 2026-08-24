# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.31] — 2026-08-24

### Added

- **Telegram shows that the agent is working, and which tools it is using.** A turn
  used to be completely silent: a geo request on a local model takes three to ten
  minutes, and in that time the chat showed nothing — a watcher could not tell a
  working agent from a broken one. Now the channel keeps a `typing…` indicator alive
  for the whole turn (started when the message is enqueued, refreshed every 4 s
  because the action expires after ~5, stopped in `done()` **and** `send_error()`),
  and posts the tool names as they are called:

  ```
  🔧 osm_features
  🔧 qgis_reproject
  ```

  Configurable via `channels.telegram.show_tools` (default `true`); the typing
  indicator is unconditional.

  **The tool output is bounded, not one message per call.** The Bot API throttles a
  bot to roughly one message per second per chat and counts edits against the same
  budget, so a twenty-tool run posting a line each would spend the turn throttled.
  The channel sends one progress message per turn and edits it in place at most once
  a second; calls in between coalesce into the next edit, and the last one is always
  flushed before the answer. The message keeps the newest 12 lines plus a `… n more`
  counter so it cannot grow towards the 4096-char limit. Arguments and results are
  never posted — a tool result can be tens of kilobytes, and the signal worth paying
  for on a phone screen is *which* tool ran, so `send_tool_result` and `send_thinking`
  stay no-ops.

  Tests (`tests/test_telegram_progress.py`, fake telegram objects, no token or
  network) pin the bounds that matter: the refresher is cancelled on both exit paths
  and no task survives the turn, and twenty tool calls in a row still deliver the
  answer within a fixed number of API calls.

## [0.1.30] — 2026-08-24

### Added

- **Channels can hand back a file.** `send_file(path, caption=None)` is part of the
  `ReplyHandle` protocol, so an artefact — a rendered map, a PNG, an export — is a
  first-class thing a channel delivers instead of a path buried in the answer text.
  Telegram uploads it (photo for `.png/.jpg/.jpeg/.gif/.webp` so it renders inline,
  document otherwise); WebChat emits a `file` SSE event carrying the local path,
  which the dashboard renders inline (images) or as a download button. *A custom
  channel with its own reply object now has to implement `send_file` — the protocol
  is `runtime_checkable` and `QueueItem` validates against it.*

- **The Telegram channel attaches the artefacts an answer names**
  (`channels.telegram.attach_files`, default `false`). After the answer text is
  sent, the channel re-reads it and uploads the files it mentions — no agent or
  prompt change, so agents that already report paths start delivering files. An
  agent asked for a map used to answer with `/Users/…/map.html`, which in Telegram
  is nothing you can open.

  **The scanned text is model output, not user input**, so the confinement in the
  new `selmakit/attachments.py` is the load-bearing part: a path is uploaded only
  when it resolves — `..` collapsed and symlinks followed — inside the state
  directory, which is also the sandbox root of the agent's own file tools.
  A hallucinated or coaxed `/etc/passwd` is refused and logged. Relative paths
  resolve against that root, matching how the agent addresses its own files
  (`workspace/map.html`). A file that doesn't exist is skipped quietly, each
  distinct path is sent once in the order it appears, files over 20 MB get one
  line saying so instead of a stalled upload, and a failed upload costs the
  attachment, not the answer.

  Note what the root includes: everything under `.selmakit/`, `selmakit.json` and
  any API key in it among it. Switch this on for chats you would hand those files to.

### Security

- **The bot token no longer lands in the log file.** python-telegram-bot calls the
  Bot API through httpx, which logs every request URL at INFO — and the token is a
  path segment of that URL, so a gateway logging at INFO wrote the credential to
  disk in clear text once per poll. `TelegramChannel.start()` now raises the httpx
  logger to WARNING.

## [0.1.28] — 2026-08-24

### Security

- **The Telegram channel has an access list.** `channels.telegram.allowed_chat_ids`
  is a list of chat ids allowed to talk to the bot; a message from any other chat
  is dropped before it reaches the queue. Until now anyone who found the bot could
  drive the agent — and a SelmaKit agent has filesystem tools and, depending on the
  deployment, runs local commands, so an unrestricted bot handed a stranger those
  tools. The check is on `effective_chat.id`, not the user: in a private chat the
  two coincide, and in a group the chat id is the group, so allowing a whole group
  stays a supported setup.

  **The default is an empty list, which still accepts everyone** — that is the
  pre-existing behaviour, and tightening it here would lock every running
  deployment out of its own bot on upgrade. While the list is empty the channel
  logs a warning at start naming what is exposed and how to restrict it.

  A rejected message is never answered — a reply confirms the bot exists to
  whoever probed it — but it is logged with the rejecting chat id, which is the
  only practical way for an owner to learn their own:

  ```
  Telegram: ignored message from chat 123456789 (not in
  channels.telegram.allowed_chat_ids). Add that id to allow it.
  ```

  If you do not know your id yet, put a placeholder (e.g. `[0]`) in the list and
  message the bot — that line names the id that was dropped.

### Added

- **A test suite exists**, for the first time: `tests/`, run with
  `uv run pytest tests/ -q` (`pytest` joins `mypy` and `ruff` in the `dev`
  dependency group, so a plain `uv sync` installs it; dependency groups are not
  part of the wheel). It currently covers only the Telegram access list — allowed
  id reaches the queue, rejected id does not and is never answered, empty list
  stays permissive, and the start-up warning fires. No async plugin: the tests
  drive coroutines through `asyncio.run`, and a fake queue completes each item's
  reply so the handler's `await reply.wait()` returns.

### Changed

- **Minimum `pydantic-ai` raised to 2.33.0 and `pydantic-ai-harness` to 0.24.0.**
  No source changes were needed — the harness filesystem walkers behave as before
  (the dot-prefixed-component skip and `max_list_results` cap both survive, only
  their line numbers moved), and the `anthropic` 1.0 major bump that comes with
  pydantic-ai is absorbed entirely by pydantic-ai's `AnthropicModel`, so
  `build_model()`'s provider dispatch is unaffected.
- `TelegramChannel`'s message handler moved from a closure inside `start()` to a
  `_handle` method registered on the `MessageHandler`. Same behaviour; it is what
  makes the access list testable without a bot token or a network.
- The README described the default `FileSystem` capability as
  `FileSystem(root_dir=".")` "sandboxed to the project directory". It has been
  rooted at the state dir (`.selmakit/`) since the harness migration — corrected,
  since the sentence describes a security boundary.

## [0.1.27] — 2026-08-18

### Fixed

- **One unreadable page no longer aborts the whole turn.** `WebFetch`'s local
  fetcher raises `ModelRetry` for HTTP and connection failures, but conversion
  errors escape it: `markdownify` recurses once per DOM node, so a deeply nested
  page (the W3C WebGPU spec is one) raises `RecursionError`, which propagated out
  of the tool, out of the run, and reached the user as a bare "maximum recursion
  depth exceeded". `local_web_fetch()` now wraps the fetcher so any unexpected
  exception degrades to a `ModelRetry` naming the URL — an unreadable URL in a
  research batch skips like a 404 does — and logs a warning.
- **A tool-name fumble no longer kills the turn.** Skills are deferred
  capabilities, so their names sit in the prompt catalog next to the real tools
  and smaller local models call e.g. `web-research` directly instead of
  `load_capability(id="web-research")`. Each unknown name is a `ModelRetry`
  charged to that name, and pydantic-ai's default budget of 1 ended the run on
  the second fumble. The agent is now built with `retries={"tools": 4}`, leaving
  room for the model to fall back to the real tools.
- **`TelegramReply` implemented only half of `ReplyHandle`**, and the worker
  calls the rest unconditionally: `/verbose on` made every Telegram turn fail
  (`send_tool(name, args=…)` → `TypeError`, `send_tool_result`/`send_thinking` →
  `AttributeError`), and a turn ending on an approval-gated MCP tool hit a
  missing `send_approval` and surfaced as an error instead of the request. The
  live-progress parts are now accepted and dropped — Telegram has no side panel
  — while `send_approval` appends a text notice naming the gated tools and
  asking for `/approve` or `/deny`, since the ✅/🚫 buttons are dashboard-only.
- **`/model` and `/models` stalled the entire gateway.** Both validated against
  the Ollama endpoint with a blocking `urllib.request.urlopen(timeout=5)` from
  an `async def`. Command handlers are awaited on the gateway's event loop, and
  `serve()` runs the channels, the worker, the heartbeat and cron in one
  `asyncio.gather()` — so an unreachable or hung endpoint froze all of them for
  the full five seconds, not just the turn that ran the command. Now `httpx`
  (already a core dependency): measured against a socket that accepts and never
  answers, the loop keeps running (49 ticks over the 5 s timeout, previously 0).
- `truncated_by` in `tools.py` was computed and never read — the truncation
  notice never said whether the line or the byte budget hit first. Removed;
  the notice's `Use offset=N to continue` covers both cases identically.
- `build_subagents_capability()` re-imported `WebFetch` locally, shadowing the
  module-level import without using it.

### Changed

- **`mypy` and `ruff` are now a `dev` dependency group**, so a plain `uv sync`
  installs both and `uv run mypy selmakit/` / `uv run ruff check selmakit/` work
  in a fresh checkout. Both report no issues; getting there fixed the
  `TelegramReply`, `/model` and `truncated_by` bugs above plus an MCP
  `transport` annotation and a `DeferredToolResults(approvals=…)` dict-invariance
  error.
  `[tool.ruff.lint]` pins a narrower set than ruff's default (`E4,E7,E9,F` +
  `ASYNC,PLW,FURB`). Out of the box ruff reports ~125 findings here and nearly
  all are this codebase working as designed — `BLE001` flags the
  degrade-with-a-warning pattern the whole extras architecture rests on, and
  `UP006` is cosmetic given `from __future__ import annotations`. The three
  groups that are kept are the ones that actually caught bugs.
- `subprocess.run` in the `rg` helpers now passes `check=False` explicitly. Not
  cosmetic for the search call: `rg` exits 1 on "no matches", which is a normal
  empty result.
- **Minimum `pydantic-ai` raised to 2.31.1 and `pydantic-ai-harness` to 0.22.0.**
  The harness release rewrote the filesystem walkers that back the default
  capability set's `FileSystem`: `list_directory`, `search_files` and
  `find_files` now resolve each entry's symlink before authorizing it, so one
  pointing out of the sandbox root or dangling is dropped rather than listed,
  and `list_directory` gained a `max_list_results` cap (default 1000, left at
  the default — reachable in a long-lived install, where `sessions/` holds two
  files per session). The hard skip of dot-prefixed path components survives,
  so rooting `FileSystem` at the state dir rather than the project still holds.
  On the pydantic-ai side both fixes are provider-specific and one is reachable
  from here: Gemini models that reject `thinking_level='MINIMAL'` now fall back
  to `'LOW'`. `/think` only accepts `off`/`low`/`medium`/`high`, but
  `model.thinking` in `selmakit.json` is a free string that reaches the model
  unchecked.

## [0.1.26] — 2026-08-16

### Added

- **Transcript view in the dashboard.** A sidebar switch (`Ansicht: Chat |
  Transcript`) toggles the chat bubbles for a flat, one-row-per-message trace
  showing the assembled system prompt, injected context, and each tool call
  paired with its result. It reads the persisted session file, so it surfaces
  what the SSE stream never carried; a live turn still streams, then rebuilds
  from disk when it completes — the in-flight turn is drawn as transcript rows
  in the same grid, so it no longer appears as loose left-aligned text that
  jumps into place when the turn ends. Rows whose collapsed form hides something render
  as a `<details>` — click to expand the full text in place, with newlines and
  indentation preserved and its own scroll area. Rendered through `st.html`
  rather than `st.markdown(unsafe_allow_html=True)`, which would otherwise run
  message content through the markdown parser and turn stray backticks and
  `**` into code spans and bold, breaking the one-line row layout.
  New `DashboardConfig` fields: `state_dir`, `show_view_switch`, `default_view`.

### Security

- **`RuntimeInfoCapability` no longer injects the hostname.** Asked "what can
  be found about me online?", the agent lifted the account name out of the
  injected `host=MacBook-Air-von-<user>` line (and out of an absolute workspace
  path) and sent it to DuckDuckGo — putting the operator's username in front of
  a third party, unprompted, in direct contradiction of the workspace's own
  "don't share private data" rule. The hostname bought the model nothing;
  it is now opt-in via `RuntimeInfoCapability(include_host=True)`.
  The stock workspace guidance gained matching red lines: never put local
  identifiers (account name, hostname, absolute paths) into outbound requests,
  and don't compile profiles of third parties who merely share the user's name.

### Removed

- **Phoenix is gone from the project.** `start.sh`/`start.bat` no longer pull,
  start or stop a `arizephoenix/phoenix` container and no longer probe for
  Docker at all; the Phoenix-specific `openinference.project.name` resource
  attribute is dropped from `tracing.setup()`, and every reference in the
  README, `CLAUDE.md`, `doc/selmakit.md` and `pyproject.toml` is removed.
  Instrumentation itself stays — selmakit exports OTLP/HTTP spans to whatever
  collector you configure, it just no longer ships or assumes a backend.
  `DEFAULT_ENDPOINT` moves from Phoenix's `:6006` to the conventional OTLP/HTTP
  `http://localhost:4318/v1/traces`.

### Changed

- **Tracing is now opt-in through a `tracing` config section**
  (`enabled`/`endpoint`/`project_name`/`capture_http`), **disabled by default**.
  `Gateway.serve()` previously called `tracing.setup()` unconditionally against
  the Phoenix port, so with no collector running every turn logged a retry storm
  and a failed-export error. For inspecting runs without a collector, the
  dashboard's Transcript view now covers that ground.
- Require `pydantic-ai >= 2.31.0` (from 2.27.0) and
  `pydantic-ai-harness >= 0.21.0` (from 0.18.0). Notable in the range: FastMCP 4
  / MCP SDK v2 support in `MCPToolset`, deferred tools must be revealed before
  they can be called, and three security fixes in the dev web-chat UI
  (`Agent.to_web()` / `clai web`) that selmakit does not use.
- **`pydantic-ai-harness` is now a core dependency**, not the `subagents` extra,
  because two default capabilities come from it (below). The `subagents` extra
  is kept but empty so existing `selmakit[subagents]` installs keep resolving.
- **`FilesystemCapability` replaced by the harness `FileSystem`.** File tools are
  now sandboxed: absolute paths, `~` and `../` escapes are rejected and symlinks
  resolved before authorization — previously the agent could reach any path on
  the host. The tool surface also grew: `read_file`, `write_file`, `edit_file`,
  `list_directory`, `search_files`, `find_files`, `create_directory`,
  `file_info`.
  The root is the **state directory** (`.selmakit/`), not the project root,
  because the harness walkers hard-skip any path with a dot-prefixed component;
  rooted at the project, the entire state directory listed as empty instead of
  erroring. Agent-visible paths are therefore relative to `.selmakit/`
  (`workspace/SOUL.md`, `sessions/`, `selmakit.json`), and files outside it —
  including the project's own source — are no longer reachable.
  **Breaking:** the tool names changed (`read` → `read_file`, `ls` →
  `list_directory`, `grep` → `search_files`, `find` → `find_files`) and paths
  are now state-dir relative, so workspace files and `SKILL.md`s naming either
  need updating. `selmakit.FilesystemCapability` is gone;
  `make_filesystem_tools()` remains for custom toolsets.
- **The local `web_fetch` may now reach loopback and private addresses**
  (`gateway.local_web_fetch()` passes `allow_local_urls=True`). pydantic-ai
  blocks them by default as SSRF protection, which broke health checks against
  Ollama (:11434) and the gateway (:8000). Deployments exposing the agent to
  untrusted input should reconsider this. Note the fetcher pins each URL to one
  resolved IP, so on a dual-stack host `localhost` can resolve to `::1` and fail
  against an IPv4-only listener — use `127.0.0.1` explicitly.
- **`SkillsPromptCapability` replaced by the harness `Skills`.** Skills are
  deferred capabilities now: only name and description sit in the prompt and the
  model pulls a skill's body in through the `load_capability` tool, instead of
  a permanent `<available_skills>` XML block plus hand-written selection rules.
  `/skill <name>` rewrites to a `load_capability` instruction; `/skills` is
  unchanged. **Breaking:** `selmakit.SkillsPromptCapability` and
  `skills.build_skills_xml()` are gone; `gateway.build_skills_capability()`
  replaces them and returns `None` when the workspace has no skills.
  Also note `Skills` scans its directories **once at construction**, where the
  old capability re-read them every run — editing a `SKILL.md` now needs a
  gateway restart. And the dropped selection rules included "resolve a skill's
  relative paths against its own directory", so skills must spell paths out
  relative to the sandbox root.

### Fixed

- `doc/session.md` and `CLAUDE.md` claimed the rendered instructions were
  stripped from the persisted message history — they never were. Also corrected
  the `model_override` row, which still said the run loop ignored it.

## [0.1.25] — 2026-08-09

### Added

- This changelog, linked from the README and `doc/selmakit.md`.

### Changed

- Require `pydantic-ai >= 2.27.0` (from 2.24.0). Notable in the range:
  `run_stream_events()` became the public `AgentRunEvents` API — selmakit's
  `Agent.run_stream_events()` already builds on it, now on supported ground —
  plus run cancellation via `AgentRun.cancel()`, deferred tool revelation over
  native provider channels, a Snowflake Cortex model/provider, and fixes to
  Anthropic/OpenAI compaction and OpenTelemetry serialization.
- Require `pydantic-ai-harness >= 0.18.0` for the `subagents` extra, which adds
  delegation of open-ended web tasks to a browser-use agent.

### Fixed

- **`ScheduleRunner` was annotated with the builtin `any`, not `typing.Any`**
  (`schedule.py`), so `handler` and `agent` were typed as a function object and
  every use of the agent inside the run loop — `workspace_dir`, `run_stream`,
  calling the handler — was a type error. Both now carry real types: a
  `ScheduleHandler` callable alias and `Agent` under `TYPE_CHECKING`.
- `_format_jobs` called `.isoformat()` on `CronJob.at`, which is `datetime |
  None`, guarded only by `kind == "at"` (`cron.py`). Now guarded on the field
  itself, with a fallback for a job whose `at` is missing.
- Annotated the `history` list in `pydantic_ai_chat.py`.
- With those three, `mypy selmakit/` reports no issues.

## [0.1.24] — 2026-08-05

### Changed

- Require `pydantic-ai >= 2.24.0` (bugfix release: stream part indexes during
  `apply_event` replay, plus Google/Bedrock/Groq/OpenRouter provider fixes).
- Require `pydantic-ai-harness >= 0.17.0` for the `subagents` extra.

## [0.1.23] — 2026-08-05

First release published to PyPI: `pip install selmakit`.

### Added

- **`selmakit` console command** (`selmakit.cli`) with `init`, `gateway` and
  `dashboard` subcommands — the equivalent of the repo's `gateway.py` /
  `dashboard.py` for a pip-installed selmakit, where those files do not exist.
- **Optional dependency extras.** The core install is the agent loop, the
  WebChat channel, sessions, memory and cron; `dashboard` (streamlit),
  `telegram` (python-telegram-bot) and `subagents` (pydantic-ai-harness) are
  opt-in, with `all` pulling in everything. Each is imported lazily at its use
  site, so a core-only install logs which extra to install instead of crashing.
- **Raw HTTP payloads in traces.** `tracing.setup(capture_http=True)`, the
  default, records the request/response bodies of the calls to the model
  provider — what actually went over the wire, next to pydantic-ai's own view.
  Headers are deliberately not captured: they carry the provider API keys.
- Package metadata: README as the long description, SPDX `MIT` license,
  authors, keywords, classifiers and project URLs. The PyPI project page was
  previously blank.
- GitHub Actions release workflow using PyPI Trusted Publishing (OIDC, no
  stored token), with a tag/version consistency check, `twine check` and a
  wheel smoke test before any upload.

### Changed

- **Lowered the Python floor from `>=3.13` to `>=3.11`.** No 3.12/3.13-only
  syntax or stdlib API was in use.
- **Rebuilt `selmakit/tracing.py` on the Logfire SDK**, which ships with
  pydantic-ai — tracing now needs no extra at all. Logfire is used purely as a
  local OpenTelemetry client (`send_to_logfire=False`): no data leaves the
  machine, no account or token. Export moved from OTLP/**gRPC** on `:4317` to
  OTLP/**HTTP** on `http://localhost:6006/v1/traces`, the port Phoenix already
  serves its UI on, so `start.sh`/`start.bat` no longer map `4317`.
- Disabled Logfire's value scrubbing. Its patterns (`session` among them) blank
  a whole value on match, which wiped the entire workspace-files system message
  — `SOUL.md` contains "Every session you start fresh" — and gutted exactly
  what these traces are for. Defensible because the export is local-only;
  re-enable it if `endpoint` ever points at a remote collector.
- Moved the workspace bootstrap from the root `setup.py` to `selmakit/init.py`.
  The root file was an init script, not a packaging script, and invited
  `python setup.py` to create a workspace by accident.
- Trimmed the sdist (`.github/` no longer shipped) and required `pydantic-ai >= 2.23.0`.

### Fixed

- **`init` wrote a config the schema does not read.** The default config placed
  `webchat` at the top level, while `SelmaKitConfig` expects `channels.webchat`
  — so edits to host and port were silently ignored and the defaults used
  instead. The config is now generated from `SelmaKitConfig().model_dump()`, so
  it cannot drift from the schema again.
- **`tracing.setup(project_name=…)` had no effect on Phoenix.** Phoenix groups
  spans by the `openinference.project.name` resource attribute, not by
  `service.name`, so everything landed in its "default" project.
- `selmakit dashboard` reported a bare `No module named streamlit` instead of
  naming the extra to install.
- The Telegram channel's "not installed" message now names `selmakit[telegram]`.

---

Versions before 0.1.23 were never published to PyPI and are not listed here.

[Unreleased]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.30...HEAD
[0.1.30]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.28...v0.1.30
[0.1.28]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.27...v0.1.28
[0.1.27]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.26...v0.1.27
[0.1.26]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.25...v0.1.26
[0.1.25]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.24...v0.1.25
[0.1.24]: https://github.com/gkvoelkl/python-selmakit/compare/74784ca...v0.1.24
[0.1.23]: https://github.com/gkvoelkl/python-selmakit/commit/74784ca
