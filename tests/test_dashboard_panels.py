"""Sidebar panels: the three assurances that make the hook usable.

A panel is called when the sidebar renders; it is called *again* during a turn
(the point of a panel over the chronological tool log); and an exception in one
does not end the turn. All three run without a gateway — the SSE stream is a
stub — and outside a Streamlit runtime, which only logs "missing
ScriptRunContext" warnings.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402

from selmakit.dashboard import SidebarContext, run  # noqa: E402
from selmakit.dashboard.app import render_sidebar_panels  # noqa: E402


class FakeBox:
    """Stands in for ``st.sidebar.empty()``: hands out a container context."""

    def __init__(self) -> None:
        self.entered = 0

    def container(self) -> "FakeBox":
        return self

    def __enter__(self) -> "FakeBox":
        self.entered += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeResponse:
    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._events = events

    def iter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeClient:
    """Replaces ``httpx.Client`` so a turn can be driven with canned events."""

    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._events = events

    def __call__(self, *args: object, **kwargs: object) -> "FakeClient":
        return self

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def stream(self, *args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(self._events)


def _drive_turn(monkeypatch, panel, events: List[Dict[str, Any]]) -> None:
    """Run one dashboard turn with a stubbed stream and a typed prompt."""
    import selmakit.dashboard.app as app

    monkeypatch.setattr(st, "chat_input", lambda *a, **k: "hallo")
    monkeypatch.setattr(app.httpx, "Client", FakeClient(events))
    run(title="t", show_settings=False, sidebar_panels=[panel])


def test_panel_is_called_when_the_sidebar_renders(monkeypatch) -> None:
    seen: List[SidebarContext] = []
    monkeypatch.setattr(st, "chat_input", lambda *a, **k: None)

    run(title="t", show_settings=False, sidebar_panels=[seen.append])

    assert len(seen) == 1
    assert seen[0].streaming is False
    assert seen[0].tool_activity == ()


def test_panel_is_repainted_during_a_turn(monkeypatch) -> None:
    """Two tool calls must reach the panel *while* the turn runs, each time with
    the activity so far — otherwise the panel is blind exactly when it matters."""
    seen: List[SidebarContext] = []
    _drive_turn(
        monkeypatch,
        seen.append,
        [
            {"type": "tool", "name": "write_plan", "args": '{"step": 1}'},
            {"type": "tool", "name": "write_plan", "args": '{"step": 2}'},
            {"type": "chunk", "text": "fertig"},
            {"type": "done"},
        ],
    )

    streaming = [ctx for ctx in seen if ctx.streaming]
    assert len(streaming) == 2
    assert [len(ctx.tool_activity) for ctx in streaming] == [1, 2]
    assert streaming[-1].tool_activity[-1]["args"] == '{"step": 2}'
    # The turn ends with a settled draw, so the panel does not stay "streaming".
    assert seen[-1].streaming is False


def test_thinking_deltas_do_not_repaint_panels(monkeypatch) -> None:
    """``repaint()`` fires per thinking delta — panels must stay out of that path."""
    seen: List[SidebarContext] = []
    _drive_turn(
        monkeypatch,
        seen.append,
        [
            {"type": "thinking", "text": "a"},
            {"type": "thinking", "text": "b"},
            {"type": "thinking", "text": "c"},
            {"type": "done"},
        ],
    )

    assert [ctx for ctx in seen if ctx.streaming] == []


def test_panel_exception_does_not_end_the_turn(monkeypatch) -> None:
    def exploding(ctx: SidebarContext) -> None:
        raise ValueError("boom")

    # Reaches the end of the stream despite the panel raising on every repaint.
    _drive_turn(
        monkeypatch,
        exploding,
        [
            {"type": "tool", "name": "write_plan", "args": "{}"},
            {"type": "chunk", "text": "fertig"},
            {"type": "done"},
        ],
    )

    assert st.session_state.messages[-1]["content"] == "fertig"


def test_panel_error_is_reported_in_its_own_box() -> None:
    boxes = [FakeBox(), FakeBox()]
    reported: List[str] = []
    ok: List[SidebarContext] = []

    def exploding(ctx: SidebarContext) -> None:
        raise ValueError("boom")

    original = st.error
    st.error = reported.append  # type: ignore[assignment]
    try:
        render_sidebar_panels(boxes, [exploding, ok.append], SidebarContext())
    finally:
        st.error = original  # type: ignore[assignment]

    assert len(reported) == 1
    assert "exploding" in reported[0] and "boom" in reported[0]
    # The second panel still ran: one failure does not take out the rest.
    assert len(ok) == 1
