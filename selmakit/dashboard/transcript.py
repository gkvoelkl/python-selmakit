"""
selmakit/dashboard/transcript.py

The transcript view: one row per message part, closer to a trace than a chat.

Data comes from the persisted session file (``sessions/<key>.json``) — the raw
pydantic-ai message list — not from the SSE stream, because only the file has
the assembled instructions and a reliable tool-call → tool-return pairing. The
dashboard streams a turn live as usual and re-reads the file once it is done
(see ``app.py``), so the view upgrades to full fidelity at the end of each turn.

Messages are parsed as plain dicts rather than through pydantic-ai's
``TypeAdapter``: the view only needs a handful of fields, and reading them
loosely keeps it from breaking when the message schema gains parts.
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import streamlit as st

# Splits the instructions blob at markdown H2s so the injected sections
# (Workspace Files, Skills, Runtime, …) become their own CONTEXT rows.
_SECTION_RE = re.compile(r"^## +(.+)$", re.MULTILINE)

# WorkspacePromptCapability pastes each workspace file's raw content under
# "## Workspace Files (injected)", one "### <file>" per file — so that block's
# H2s belong to the files, not to a capability. It is therefore carved out
# first and split at its "### " file markers instead.
_WORKSPACE_HEADING = "## Workspace Files"
_FILE_RE = re.compile(r"^### +(.+)$", re.MULTILINE)
# Capability sections emitted after the workspace block; the first one that
# appears ends it.
_AFTER_WORKSPACE = ("\n## Skills", "\n## Runtime")

# Badge colours per row kind: (text, background). Both are set explicitly and
# the background is translucent, so the palette survives Streamlit's light and
# dark themes without a second set of values.
_BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    "SYSTEM":    ("#6b7280", "rgba(107, 114, 128, 0.16)"),
    "CONTEXT":   ("#2f9e5f", "rgba(47, 158, 95, 0.16)"),
    "USER":      ("#3b6fd4", "rgba(59, 111, 212, 0.16)"),
    "ASSISTANT": ("#7c5cd6", "rgba(124, 92, 214, 0.16)"),
    "THINKING":  ("#8a8f98", "rgba(138, 143, 152, 0.14)"),
    "TOOL":      ("#d1791f", "rgba(209, 121, 31, 0.16)"),
    "RETRY":     ("#cc3b3b", "rgba(204, 59, 59, 0.16)"),
}

_MAX_TEXT = 400      # chars kept for prose rows
_MAX_ARGS = 90       # chars kept for a tool call's argument blob
_MAX_RESULT = 160    # chars kept for the inline "→ result" preview


@dataclass
class Row:
    """One rendered line of the transcript."""

    kind: str                  # SYSTEM | CONTEXT | USER | ASSISTANT | THINKING | TOOL | RETRY
    text: str                  # main content (already truncated)
    full: str = ""             # untruncated content, revealed when the row is expanded
    mono: str = ""             # monospace lead-in (tool name + args)
    result: str = ""           # the "→ …" suffix on TOOL rows
    turn: int | None = None    # set only on the first row of a turn
    error: bool = False
    truncated: bool = False    # collapsed view hides something → render as expandable


def sessions_dir_for(config_file: str, state_dir: str | None = None) -> str:
    """Locate the sessions directory from the dashboard's config file path.

    ``.selmakit/selmakit.json`` → ``.selmakit/sessions``. An explicit
    ``state_dir`` wins when the caller keeps state somewhere else.
    """
    root = state_dir or os.path.dirname(config_file) or "."
    return os.path.join(root, "sessions")


def load_session_messages(sessions_dir: str, session_key: str) -> List[dict]:
    """Read the persisted message list for a session ( ``[]`` when absent)."""
    path = os.path.join(sessions_dir, f"{session_key}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _clip(value: Any, limit: int) -> str:
    """Collapse to a single line and truncate to ``limit`` chars."""
    return _clip2(value, limit)[0]


def _clip2(value: Any, limit: int) -> Tuple[str, bool]:
    """``_clip`` plus a flag for whether anything was hidden.

    Collapsing newlines counts as hiding something: the row still reads as one
    line, but expanding it is the only way to see the original shape.
    """
    raw = str(value or "")
    text = " ".join(raw.split())
    if len(text) > limit:
        return text[:limit] + "…", True
    return text, text != raw.strip()


def _h2_sections(text: str) -> List[Tuple[str, str]]:
    """Split a span at H2 headings into ``(heading, body)`` pairs."""
    matches = list(_SECTION_RE.finditer(text))
    sections: List[Tuple[str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((match.group(1), text[match.end():end].strip()))
    return sections


def _fence_ranges(text: str) -> List[Tuple[int, int]]:
    """Spans covered by ``` fenced code blocks."""
    fences = [m.start() for m in re.finditer(r"^```", text, re.MULTILINE)]
    return [(fences[i], fences[i + 1]) for i in range(0, len(fences) - 1, 2)]


def _workspace_span(instructions: str) -> Tuple[int, int] | None:
    """Locate the workspace-files block, whose H2s belong to the files."""
    start = instructions.find(_WORKSPACE_HEADING)
    if start == -1:
        return None
    ends = [i for i in (instructions.find(m, start) for m in _AFTER_WORKSPACE) if i != -1]
    return start, min(ends) if ends else len(instructions)


def _split_instructions(instructions: str) -> List[Tuple[str, str]]:
    """Break the instructions blob into one SYSTEM and N CONTEXT rows.

    The blob is a single string assembled from every capability's
    ``get_instructions()``. Splitting it back into those fragments is what
    makes the rows readable: H2 headings mark capability sections, except
    inside the workspace block, which is split per injected file instead.
    """
    span = _workspace_span(instructions)
    head, workspace, tail = instructions, "", ""
    if span:
        head, workspace, tail = instructions[: span[0]], instructions[span[0]: span[1]], instructions[span[1]:]

    rows: List[Tuple[str, str]] = []
    head_sections = _h2_sections(head)
    # Capabilities that emit no heading (memory, sub-agents) land ahead of the
    # first one; when even that is empty the SYSTEM row still marks the start.
    first_h2 = _SECTION_RE.search(head)
    lead = head[: first_h2.start()].strip() if first_h2 else head.strip()
    rows.append(("SYSTEM", lead or f"Initial system prompt · {len(instructions)} chars"))
    rows += [("CONTEXT", f"{h} — {b}" if b else h) for h, b in head_sections]

    if workspace:
        # A workspace file may show "### …" inside a fenced example (the stock
        # TOOLS.md does) — those are content, not file markers.
        fences = _fence_ranges(workspace)
        files = [
            m for m in _FILE_RE.finditer(workspace)
            if not any(start <= m.start() < end for start, end in fences)
        ]
        for i, match in enumerate(files):
            end = files[i + 1].start() if i + 1 < len(files) else len(workspace)
            rows.append(("CONTEXT", f"{match.group(1)} — {workspace[match.end():end].strip()}"))
        if not files:
            rows.append(("CONTEXT", workspace.strip()))

    rows += [("CONTEXT", f"{h} — {b}" if b else h) for h, b in _h2_sections(tail)]
    return rows


def _tool_returns(messages: List[dict]) -> Dict[str, Tuple[str, bool]]:
    """Map ``tool_call_id`` → (result, is_error) across the whole history.

    A tool's return arrives in the *following* request, so the pairing needs a
    pass over every message before rows can be emitted.
    """
    returns: Dict[str, Tuple[str, bool]] = {}
    for message in messages:
        for part in message.get("parts", []):
            kind = part.get("part_kind")
            if kind == "tool-return":
                returns[part.get("tool_call_id", "")] = (str(part.get("content", "")), False)
            elif kind == "retry-prompt":
                # A retry prompt is the failure channel for a tool call.
                returns[part.get("tool_call_id", "")] = (str(part.get("content", "")), True)
    return returns


def build_rows(messages: List[dict]) -> List[Row]:
    """Flatten a pydantic-ai message list into transcript rows."""
    returns = _tool_returns(messages)
    rows: List[Row] = []
    seen_instructions = ""      # only re-emit SYSTEM/CONTEXT when the prompt changes
    turn_by_run: Dict[str, int] = {}
    pending_turn: int | None = None

    for message in messages:
        run_id = message.get("run_id") or ""
        if run_id and run_id not in turn_by_run:
            turn_by_run[run_id] = len(turn_by_run) + 1
            pending_turn = turn_by_run[run_id]

        instructions = message.get("instructions") or ""
        if instructions and instructions != seen_instructions:
            seen_instructions = instructions
            for kind, text in _split_instructions(instructions):
                clipped, cut = _clip2(text, _MAX_TEXT)
                rows.append(Row(kind=kind, text=clipped, full=text, truncated=cut))

        for part in message.get("parts", []):
            kind = part.get("part_kind")
            row: Row | None = None

            if kind in ("user-prompt", "system-prompt", "text", "thinking"):
                content = str(part.get("content", ""))
                clipped, cut = _clip2(content, _MAX_TEXT)
                row = Row(
                    kind={
                        "user-prompt": "USER",
                        "system-prompt": "SYSTEM",
                        "text": "ASSISTANT",
                        "thinking": "THINKING",
                    }[kind],
                    text=clipped,
                    full=content,
                    truncated=cut,
                )
            elif kind == "tool-call":
                call_id = part.get("tool_call_id", "")
                result, is_error = returns.get(call_id, ("", False))
                args = part.get("args")
                args_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                clipped_args, args_cut = _clip2(args_str, _MAX_ARGS)
                clipped_result, result_cut = _clip2(result, _MAX_RESULT)
                row = Row(
                    kind="TOOL",
                    text="",
                    full=f"{part.get('tool_name', 'tool')}({args_str})\n\n→ {result}",
                    mono=f"{part.get('tool_name', 'tool')} {clipped_args}",
                    result=clipped_result,
                    error=is_error,
                    truncated=args_cut or result_cut,
                )
            # tool-return / retry-prompt parts are merged into their TOOL row above.

            if row is None:
                continue
            if pending_turn is not None:
                row.turn = pending_turn
                pending_turn = None
            rows.append(row)

    return rows


def text_row(kind: str, content: str, *, turn: int | None = None) -> Row:
    """Build a prose row (USER/ASSISTANT/THINKING/…) with the same truncation
    rules the persisted transcript uses, so a live row and its saved
    counterpart render identically."""
    clipped, cut = _clip2(content, _MAX_TEXT)
    return Row(kind=kind, text=clipped, full=content, truncated=cut, turn=turn)


def tool_row(
    name: str, args: str, result: str = "", *, error: bool = False, turn: int | None = None
) -> Row:
    """Build a TOOL row from a live tool call, optionally with its result."""
    clipped_args, args_cut = _clip2(args, _MAX_ARGS)
    clipped_result, result_cut = _clip2(result, _MAX_RESULT)
    return Row(
        kind="TOOL",
        text="",
        full=f"{name}({args})" + (f"\n\n→ {result}" if result else ""),
        mono=f"{name} {clipped_args}".strip(),
        result=clipped_result,
        error=error,
        truncated=args_cut or result_cut,
        turn=turn,
    )


def next_turn(rows: List[Row]) -> int:
    """The turn number a new live turn should carry, given the persisted rows."""
    turns = [r.turn for r in rows if r.turn]
    return (max(turns) + 1) if turns else 1


def _row_html(row: Row) -> str:
    """Render one row as a grid line.

    A row whose collapsed form hides something becomes a ``<details>``: the
    summary keeps the single-line shape and clicking it reveals the full text
    below. Plain HTML disclosure, so it needs no JavaScript and survives
    Streamlit reruns without any widget state.
    """
    fg, bg = _BADGE_COLORS.get(row.kind, _BADGE_COLORS["SYSTEM"])
    turn = f"Turn {row.turn}" if row.turn else ""

    if row.mono:
        arrow = "⚠ →" if row.error else "→"
        collapsed = (
            f'<span class="sk-mono">{html.escape(row.mono)}</span>'
            f'<span class="sk-arrow"> {arrow} </span>'
            f'<span class="sk-result">{html.escape(row.result)}</span>'
        )
    else:
        collapsed = html.escape(row.text)

    if row.truncated and row.full:
        cell = (
            f'<details class="sk-details"><summary>{collapsed}</summary>'
            f'<div class="sk-full">{html.escape(row.full)}</div></details>'
        )
    else:
        # Nothing hidden — keep the tooltip so hovering still shows the text
        # in full when the column is too narrow for it.
        cell = f'<span title="{html.escape(row.text or row.full, quote=True)}">{collapsed}</span>'

    return (
        f'<div class="sk-turn">{html.escape(turn)}</div>'
        f'<div class="sk-badgecell"><span class="sk-badge" '
        f'style="color:{fg};background:{bg}">{html.escape(row.kind)}</span></div>'
        f'<div class="sk-content">{cell}</div>'
    )


_CSS = """
<style>
.sk-transcript {
  display: grid;
  grid-template-columns: 62px 96px minmax(0, 1fr);
  align-items: baseline;
  font-size: 0.86rem;
  line-height: 1.5;
  overflow-x: auto;
}
.sk-transcript > div { padding: 6px 8px; border-top: 1px solid rgba(128,128,128,0.18); }
.sk-turn { font-size: 0.68rem; opacity: 0.55; text-align: left; white-space: nowrap; }
.sk-badgecell { text-align: right; }
.sk-badge {
  display: inline-block; padding: 1px 7px; border-radius: 5px;
  font-size: 0.64rem; font-weight: 700; letter-spacing: 0.04em;
}
.sk-content { min-width: 0; }
.sk-content > span,
.sk-details > summary { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sk-mono, .sk-result, .sk-arrow { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.sk-mono { font-weight: 600; }
.sk-arrow { opacity: 0.45; }
.sk-result { opacity: 0.75; }

/* Expandable rows: hide the native triangle and mark them with a caret that
   rotates on open, so a collapsed row still reads as one clean line. */
.sk-details > summary { cursor: pointer; list-style: none; padding-right: 14px; position: relative; }
.sk-details > summary::-webkit-details-marker { display: none; }
.sk-details > summary::after {
  content: "⌄"; position: absolute; right: 0; top: -1px;
  opacity: 0.4; font-size: 0.8em; transition: transform 0.12s;
}
.sk-details[open] > summary::after { transform: rotate(180deg); }
.sk-details[open] > summary { white-space: normal; }
.sk-full {
  white-space: pre-wrap; word-break: break-word;
  margin: 6px 0 2px; padding: 8px 10px;
  font-size: 0.82rem; line-height: 1.45;
  background: rgba(128,128,128,0.09); border-radius: 6px;
  max-height: 420px; overflow: auto;
}
</style>
"""


def render_transcript(rows: List[Row]) -> None:
    """Render the transcript rows. Shows a hint when the session is empty.

    Uses ``st.html`` rather than ``st.markdown(unsafe_allow_html=True)``: the
    latter still runs the content through the markdown parser, so backticks,
    ``**`` and ``_`` inside a message would turn into code spans and emphasis
    and break the one-line-per-row layout. ``st.html`` emits the markup as-is.
    """
    if not rows:
        st.caption("Noch kein persistierter Verlauf für diese Session — schick eine Nachricht.")
        return
    body = "".join(_row_html(row) for row in rows)
    st.html(f'{_CSS}<div class="sk-transcript">{body}</div>')
