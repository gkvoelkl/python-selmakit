# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.25...HEAD
[0.1.25]: https://github.com/gkvoelkl/python-selmakit/compare/v0.1.24...v0.1.25
[0.1.24]: https://github.com/gkvoelkl/python-selmakit/compare/74784ca...v0.1.24
[0.1.23]: https://github.com/gkvoelkl/python-selmakit/commit/74784ca
