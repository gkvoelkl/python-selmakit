"""
selmakit/dashboard/app.py

The reusable Streamlit dashboard. A custom agent ships a tiny ``dashboard.py``:

    from selmakit.dashboard import run
    run(title="🌦️ Wetter-Agent", image="images/wetter.png",
        input_placeholder="Frag mich nach dem Wetter…")

and starts it with ``uv run streamlit run dashboard.py``. ``run()`` renders the
whole app, so it must be the first Streamlit call in the script.
"""
from __future__ import annotations

import html
import json
import os
import re
import uuid
from typing import Any, Dict, Generator, List

import httpx
import streamlit as st
import streamlit.components.v1 as components

from selmakit.dashboard.config import DashboardConfig

# Matches local .html references in a reply: bare paths, file:// URLs or
# markdown links. Captures the path part so it can be read from disk.
_HTML_REF_RE = re.compile(r"(?:file://)?(/?[\w./\-]+\.html)\b", re.IGNORECASE)

# Curated hosted models selmakit's build_model() can dispatch on (provider/model).
# Extend freely — the string is written verbatim into selmakit.json's model.model.
_COMMERCIAL_MODELS: List[str] = [
    "anthropic/claude-opus-4-8",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
]


def list_ollama_models(base_url: str) -> List[str]:
    """Return installed Ollama models as ``ollama/<name>`` strings.

    Queries the native ``/api/tags`` endpoint derived from the OpenAI-compatible
    ``base_url`` (…/v1). Returns an empty list if Ollama is unreachable.
    """
    host = base_url.rstrip("/")
    host = host.removesuffix("/v1")
    try:
        resp = httpx.get(f"{host}/api/tags", timeout=2.0)
        resp.raise_for_status()
        return [f"ollama/{m['name']}" for m in resp.json().get("models", [])]
    except Exception:
        return []


def send_command(stream_url: str, user_id: str, user_name: str, text: str, timeout: float = 10.0) -> None:
    """Send a slash command through the SSE stream and drain the reply.

    Used to apply a live ``/model`` switch to the running gateway session
    without a restart. Raises on connection failure so the caller can report it.
    """
    payload = {"user_id": user_id, "text": text, "user_name": user_name}
    with httpx.Client() as client:
        with client.stream("POST", stream_url, json=payload, timeout=timeout) as response:
            for _ in parse_sse_events(response):
                pass


def read_model_config(config_file: str) -> Dict[str, Any]:
    """Read the ``model`` section of selmakit.json (empty dict on failure)."""
    try:
        return json.loads(read_raw_file(config_file)).get("model", {})
    except Exception:
        return {}


def write_model_config(config_file: str, model: str, api_key: str | None) -> None:
    """Patch the ``model`` section of selmakit.json, preserving every other key.

    Writes ``model.model``; sets ``model.api_key`` only when a non-empty key is
    given (an empty field leaves the existing key untouched).
    """
    data: Dict[str, Any] = json.loads(read_raw_file(config_file))
    model_section = data.setdefault("model", {})
    model_section["model"] = model
    if api_key:
        model_section["api_key"] = api_key
    write_raw_file(config_file, json.dumps(data, indent=4))


def parse_sse_events(response: httpx.Response) -> Generator[dict, None, None]:
    """Reads SSE lines, yields all events as dicts."""
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        yield event
        if event.get("type") in ("done", "error"):
            break


def read_raw_file(filepath: str) -> str:
    """Reads the file as raw text."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def write_raw_file(filepath: str, content: str) -> None:
    """Writes the provided raw string content to a file."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def find_html_files(text: str) -> List[str]:
    """Returns paths of existing local ``.html`` files referenced in ``text``.

    The agent typically reports a generated file as a path or ``file://`` URL
    under the workspace; we resolve those that actually exist on disk so the
    dashboard (running on the same host as the gateway) can embed them.
    """
    found: List[str] = []
    for match in _HTML_REF_RE.finditer(text):
        path = match.group(1)
        if os.path.isfile(path) and path not in found:
            found.append(path)
    return found


def render_html_files(text: str) -> None:
    """Embeds any local ``.html`` files referenced in ``text`` inline."""
    for path in find_html_files(text):
        try:
            html = read_raw_file(path)
        except OSError:
            continue
        st.caption(f"📄 {path}")
        components.html(html, height=600, scrolling=True)


def render_attachments(files: List[dict], key_prefix: str = "live") -> None:
    """Render the files a channel delivered via ``send_file`` (SSE ``file`` events).

    The event carries a local path, not a download URL — the dashboard runs on
    the gateway's host, the same assumption ``render_html_files`` already makes.
    Images are shown; anything else gets a download button so the file is one
    click away instead of a path to copy.
    """
    for n, item in enumerate(files):
        path = item.get("path", "")
        caption = item.get("caption") or os.path.basename(path)
        if not os.path.isfile(path):
            st.caption(f"📎 {path} (not found)")
            continue
        if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            st.image(path, caption=caption)
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            st.caption(f"📎 {path} (unreadable)")
            continue
        st.download_button(
            f"📎 {caption}", data,
            file_name=os.path.basename(path),
            key=f"dl_{key_prefix}_{n}",  # unique per message, not per path
        )


# A tool result longer than this is collapsed behind a <details> rather than
# shown inline — long enough that ordinary one-line results stay readable at a
# glance, short enough that a file dump does not bury the rest of the log.
_RESULT_INLINE_LIMIT = 240


def _entry_html(entry: Dict[str, Any]) -> str:
    """One verbose log entry as HTML.

    Built as HTML, not markdown, for the reason the codebase already learned the
    hard way: tool args and results are arbitrary text, and running them through
    the markdown parser turns stray backticks and ``**`` into code spans and bold.
    """
    kind = entry.get("kind")
    if kind == "call":
        args = html.escape(entry.get("args") or "")
        name = html.escape(entry.get("name", "tool"))
        return f'<div class="sk-call">→ <b>{name}</b>(<code>{args}</code>)</div>'

    if kind == "result":
        name = html.escape(entry.get("name", "tool"))
        result = entry.get("result") or ""
        dur = entry.get("duration")
        meta = f" <span class='sk-dim'>({dur:.2f}s)</span>" if isinstance(dur, (int, float)) else ""
        if entry.get("error"):
            # A retry-prompt: the model was sent back to try again. Never collapsed
            # — this is the line you opened the log to find.
            return (
                f'<div class="sk-retry">↻ <b>{name}</b>{meta} — <i>Retry</i>'
                f'<pre>{html.escape(result)}</pre></div>'
            )
        body = html.escape(result)
        if len(result) > _RESULT_INLINE_LIMIT:
            head = html.escape(result[:_RESULT_INLINE_LIMIT].rstrip())
            return (
                f'<details class="sk-result"><summary>← <b>{name}</b>{meta} '
                f'<span class="sk-dim">{len(result)} Zeichen</span> — {head}…</summary>'
                f'<pre>{body}</pre></details>'
            )
        return f'<div class="sk-result">← <b>{name}</b>{meta}: <code>{body}</code></div>'

    if kind == "thinking":
        return f'<details class="sk-think"><summary>💭 Reasoning</summary><pre>{html.escape(entry.get("text", ""))}</pre></details>'

    if kind == "approval":
        names = ", ".join(str(p.get("tool_name", "?")) for p in entry.get("pending", []))
        return f'<div class="sk-approval">🔐 Freigabe angefordert: <b>{html.escape(names)}</b></div>'

    return ""


def _metrics_html(metrics: Dict[str, Any] | None) -> str:
    """The end-of-turn accounting line, or '' when the turn reported none."""
    if not metrics:
        return ""
    bits: List[str] = []
    if metrics.get("model"):
        bits.append(f"🤖 {html.escape(str(metrics['model']))}")
    dur = metrics.get("duration")
    if isinstance(dur, (int, float)):
        bits.append(f"⏱ {dur:.2f}s")
    tin, tout = metrics.get("input_tokens"), metrics.get("output_tokens")
    if tin is not None or tout is not None:
        bits.append(f"🔤 {tin or 0} → {tout or 0} ({metrics.get('total_tokens') or 0} total)")
    if metrics.get("requests"):
        bits.append(f"📡 {metrics['requests']} Anfrage(n)")
    return f'<div class="sk-metrics">{" &nbsp;·&nbsp; ".join(bits)}</div>' if bits else ""


_ACTIVITY_CSS = """
<style>
.sk-log { font-size: 0.86rem; line-height: 1.55; }
.sk-log pre { white-space: pre-wrap; word-break: break-word; margin: .35em 0 .1em 1.2em;
              font-size: .82rem; opacity: .85; max-height: 22em; overflow: auto; }
.sk-log code { word-break: break-all; }
.sk-log summary { cursor: pointer; }
.sk-dim { opacity: .6; }
.sk-retry { border-left: 3px solid #d9822b; padding-left: .5em; margin: .2em 0; }
.sk-approval { border-left: 3px solid #c94f4f; padding-left: .5em; margin: .2em 0; }
.sk-metrics { margin-top: .6em; padding-top: .4em; border-top: 1px solid rgba(128,128,128,.3);
              font-size: .82rem; opacity: .8; }
</style>
"""


def render_tool_activity(
    target: Any,
    entries: List[Dict[str, Any]],
    metrics: Dict[str, Any] | None = None,
    key_prefix: str = "",
) -> None:
    """Render the verbose log (→ calls, ← results, ↻ retries, 💭 reasoning) plus
    the turn's accounting into ``target`` (an ``st.empty()`` placeholder).

    Collapsible as a whole so it stays out of the way of the reply, and long
    results collapse individually inside it. Note the inner collapsibles are
    ``<details>`` rather than nested ``st.expander``s — Streamlit refuses to nest
    those. No-op when there is nothing to show.
    """
    if not entries and not metrics:
        return
    body = "".join(_entry_html(e) for e in entries) + _metrics_html(metrics)
    with target.container():
        with st.expander("🔧 Tool-Aktivität", expanded=True):
            st.html(_ACTIVITY_CSS + f'<div class="sk-log">{body}</div>')


def render_approval(pending: List[dict]) -> None:
    """Render the approval prompt + Freigeben/Ablehnen buttons for gated tool
    calls awaiting a decision. Clicking queues /approve or /deny as the next turn."""
    names = "\n".join(f"- `{p.get('tool_name','tool')}({p.get('args','')})`" for p in pending)
    st.warning(f"🔐 **Freigabe nötig** für folgende Tool-Aufruf(e):\n{names}")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("✅ Freigeben", key="mcp_approve", use_container_width=True):
        st.session_state.pending_prompt = "/approve"
        st.rerun()
    if c2.button("🚫 Ablehnen", key="mcp_deny", use_container_width=True):
        st.session_state.pending_prompt = "/deny"
        st.rerun()


def run(config: DashboardConfig | None = None, **overrides: Any) -> None:
    """Render the dashboard. Either pass a ``DashboardConfig`` or keyword
    overrides (e.g. ``run(title=..., image=..., input_placeholder=...)``)."""
    cfg = config or DashboardConfig(**overrides)

    # -- Configuration
    st.set_page_config(
        page_title=cfg.title,
        page_icon=cfg.page_icon,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if "config_editing" not in st.session_state:
        st.session_state.config_editing = False

    # -- Custom CSS for fixed sidebar width
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {
            min-width: 200px;
            max-width: 200px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if "user_id" not in st.session_state:
        st.session_state.user_id = str(uuid.uuid4())[:8]

    if "messages" not in st.session_state:
        st.session_state.messages = []

    # -- Settings Dialog
    @st.dialog("⚙️ Settings", width="large")
    def settings_dialog():
        st.subheader("Configuration")

        if "config_raw_content" not in st.session_state or not st.session_state.config_editing:
            st.session_state.config_raw_content = read_raw_file(cfg.config_file)

        if not st.session_state.config_editing:
            col_a, col_b = st.columns([0.8, 0.2])
            with col_a:
                st.info(f"Currently viewing: `{cfg.config_file}`")
            with col_b:
                if st.button("✎ Edit File"):
                    st.session_state.config_editing = True
                    st.rerun()

            try:
                # Parse the raw string into a typed Dictionary for the st.json viewer
                json.loads(st.session_state.config_raw_content)
                st.json(json.loads(st.session_state.config_raw_content))
            except Exception as e:
                st.error(f"Error details: {e.msg} at line {e.lineno}, column {e.colno}")
                st.code(st.session_state.config_raw_content)
        else:
            # --- EDIT MODE ---
            st.subheader("Edit Mode")
            st.caption("Editing raw text. No comments allowed in standard JSON.")

            edited_text: str = st.text_area(
                label="JSON Content",
                value=st.session_state.config_raw_content,
                height=400,
            )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("💾 Save Changes"):
                    try:
                        # Validation: ensure the input string is valid JSON
                        _: Dict[str, Any] = json.loads(edited_text)
                        write_raw_file(cfg.config_file, edited_text)
                        st.session_state.config_raw_content = edited_text
                        st.session_state.config_editing = False
                        st.success("File updated successfully.")
                        st.rerun()
                    except json.JSONDecodeError as e:
                        st.error(f"Validation Failed: {e.msg} at line {e.lineno}")
            with col2:
                if st.button("✖ Discard"):
                    st.session_state.config_editing = False
                    st.rerun()

    # -- Sidebar
    st.sidebar.header(cfg.title)
    if cfg.image:
        st.sidebar.image(cfg.image, width=200)

    if cfg.show_settings:
        if st.sidebar.button("⚙️ Settings"):
            settings_dialog()
        if st.session_state.config_editing:
            settings_dialog()

        # -- Chat model selector: Ollama (live) + curated hosted models
        with st.sidebar.expander("🤖 Chat model", expanded=False):
            model_cfg = read_model_config(cfg.config_file)
            current = model_cfg.get("model", "")

            options = list_ollama_models(model_cfg.get("ollama_base_url") or model_cfg.get("base_url", "http://localhost:11434/v1"))
            options += _COMMERCIAL_MODELS
            if current and current not in options:  # keep whatever is configured selectable
                options.insert(0, current)

            index = options.index(current) if current in options else 0
            selected = st.selectbox("Model", options, index=index)
            api_key = st.text_input(
                "API key",
                type="password",
                placeholder="leave blank to keep current / use env var",
                help="Stored in selmakit.json (model.api_key). Only needed for hosted providers.",
            )

            if st.button("💾 Save model"):
                try:
                    # 1) Persist to selmakit.json (survives restart; key read fresh
                    #    by the gateway when it builds the override model).
                    write_model_config(cfg.config_file, selected, api_key or None)
                    # 2) Apply live to this session via /model — no restart needed.
                    try:
                        send_command(cfg.stream_url, st.session_state.user_id, cfg.user_name, f"/model {selected}")
                        st.success(f"Model switched live to `{selected}`.")
                    except Exception:
                        st.warning("Saved to config, but gateway unreachable — applies after restart.")
                except Exception as e:
                    st.error(f"Could not save: {e}")

    # -- Alert polling (runs every 5 s independently of user input)
    @st.fragment(run_every="5s")
    def poll_alerts():
        try:
            resp = httpx.get(cfg.heartbeat_poll_url, timeout=2.0)
            if resp.status_code == 200:
                alert = resp.json().get("alert")
                if alert:
                    if isinstance(alert, dict) and alert.get("kind") in ("cron", "heartbeat"):
                        st.session_state.messages.append({"role": "cron", "content": alert["prompt"]})
                        if alert.get("reply"):
                            st.session_state.messages.append({"role": "assistant", "content": alert["reply"]})
                    else:
                        text = alert.get("reply", str(alert)) if isinstance(alert, dict) else str(alert)
                        st.session_state.messages.append({"role": "notification", "content": text})
                    st.rerun()
        except Exception:
            pass

    poll_alerts()

    # -- History, from the in-memory chat log.
    last_idx = len(st.session_state.messages) - 1
    for idx, message in enumerate(st.session_state.messages):
        if message["role"] == "notification":
            st.warning(f"🔔 {message['content']}")
        elif message["role"] == "cron":
            st.info(f"⏰ {message['content']}")
        else:
            with st.chat_message(message["role"]):
                if message["role"] == "assistant" and message.get("tool_activity"):
                    render_tool_activity(
                        st.empty(), message["tool_activity"], message.get("turn_metrics"), key_prefix=f"msg{idx}"
                    )
                st.markdown(message["content"])
                if message["role"] == "assistant":
                    render_html_files(message["content"])
                    render_attachments(message.get("attachments", []), key_prefix=f"msg{idx}")
                    # Only the most recent message can still await a decision.
                    if message.get("pending_approval") and idx == last_idx:
                        render_approval(message["pending_approval"])

    # A queued /approve or /deny (from the approval buttons) takes precedence
    # over freshly typed input; both drive an identical streamed turn.
    typed_prompt = st.chat_input(cfg.input_placeholder)
    prompt = st.session_state.pop("pending_prompt", None) or typed_prompt
    if prompt:
        display = {"/approve": "✅ Freigegeben", "/deny": "🚫 Abgelehnt"}.get(prompt, prompt)
        st.session_state.messages.append({"role": "user", "content": display})
        with st.chat_message("user"):
            st.markdown(display)

        with st.chat_message("assistant"):
            try:
                payload = {
                    "user_id": st.session_state.user_id,
                    "text": prompt,
                    "user_name": cfg.user_name,
                }
                activity_box = st.empty()
                tool_status = st.empty()
                reply_box = st.empty()
                full_reply = ""
                activity: List[Dict[str, Any]] = []  # verbose log entries, in order
                thinking_idx: int | None = None      # index of the open reasoning entry
                turn_metrics: Dict[str, Any] = {}    # end-of-turn accounting
                pending_approval: List[dict] = []    # gated tool calls awaiting a decision
                attachments: List[dict] = []         # files delivered via send_file

                def repaint() -> None:
                    render_tool_activity(activity_box, activity, turn_metrics or None)

                with httpx.Client() as client:
                    with client.stream("POST", cfg.stream_url, json=payload, timeout=cfg.stream_timeout) as response:
                        for event in parse_sse_events(response):
                            match event.get("type"):
                                case "tool":
                                    name = event.get("name", "tool")
                                    args = event.get("args")
                                    if args is not None:   # verbose: log it
                                        activity.append({"kind": "call", "name": name, "args": args})
                                        repaint()
                                    else:                  # quiet mode: a passing caption
                                        tool_status.caption(f"🔧 {name}…")
                                case "tool_result":
                                    activity.append({
                                        "kind": "result",
                                        "name": event.get("name", "tool"),
                                        "result": str(event.get("result", "")),
                                        "duration": event.get("duration"),
                                        "error": bool(event.get("error")),
                                    })
                                    repaint()
                                case "thinking":
                                    # Deltas accumulate into one entry so reasoning
                                    # stays a single collapsible, not one per token.
                                    if thinking_idx is None:
                                        thinking_idx = len(activity)
                                        activity.append({"kind": "thinking", "text": ""})
                                    activity[thinking_idx]["text"] += event.get("text", "")
                                    repaint()
                                case "metrics":
                                    turn_metrics = {k: v for k, v in event.items() if k != "type"}
                                    repaint()
                                case "approval":
                                    pending_approval = event.get("pending", []) or []
                                    activity.append({"kind": "approval", "pending": pending_approval})
                                    repaint()
                                case "file":
                                    attachments.append(event)
                                case "chunk":
                                    tool_status.empty()
                                    thinking_idx = None    # answer started; close the reasoning entry
                                    full_reply += event.get("text", "")
                                    reply_box.markdown(full_reply + "▌")
                                case "error":
                                    tool_status.empty()
                                    raise RuntimeError(event.get("message", "Unknown error."))
                                case "done":
                                    tool_status.empty()
                                    reply_box.markdown(full_reply)

                render_html_files(full_reply)
                render_attachments(attachments)
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": full_reply,
                    "tool_activity": activity,
                    "turn_metrics": turn_metrics,
                    "pending_approval": pending_approval,
                    "attachments": attachments,
                })
                # Rerun so the approval buttons render for the new last message.
                if pending_approval:
                    st.rerun()
            except httpx.ConnectError:
                st.error("❌ Gateway unreachable. Is `gateway.py` running?")
            except RuntimeError as e:
                st.error(f"❌ {e}")
            except Exception as e:
                st.error(f"An error occurred: {e}")
