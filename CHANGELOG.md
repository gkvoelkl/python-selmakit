# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
