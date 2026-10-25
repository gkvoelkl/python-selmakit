"""Chat input recall (↑/↓): which prompts are remembered and how they reach the page.

The key handling itself is browser JavaScript; what is pinned here is the
Python side — the list it is fed and that a prompt cannot break out of the
``<script>`` tag it is embedded in.
"""
from __future__ import annotations

import json
from typing import Any, List

import pytest

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402

from selmakit.dashboard import run  # noqa: E402
from selmakit.dashboard.app import (  # noqa: E402
    _INPUT_HISTORY_LIMIT,
    input_history_script,
    remember_input,
)


def test_remember_input_skips_blank_and_consecutive_duplicates() -> None:
    history: List[str] = []
    for prompt in ["a", "a", "  ", "b", "a"]:
        remember_input(history, prompt)
    assert history == ["a", "b", "a"]


def test_remember_input_keeps_only_the_newest() -> None:
    history: List[str] = []
    for i in range(_INPUT_HISTORY_LIMIT + 5):
        remember_input(history, str(i))
    assert len(history) == _INPUT_HISTORY_LIMIT
    assert history[-1] == str(_INPUT_HISTORY_LIMIT + 4)


def test_script_embeds_history_without_closing_the_tag() -> None:
    script = input_history_script(["hallo", "</script><b>x</b>"])
    assert script.count("</script>") == 1
    start = script.index("w.__skHistory = ") + len("w.__skHistory = ")
    data = script[start:script.index(";\n", start)]
    assert json.loads(data) == ["hallo", "</script><b>x</b>"]


def _run_with(monkeypatch, typed: str | None, html_calls: List[Any]) -> None:
    monkeypatch.setattr(st, "chat_input", lambda *a, **k: typed)
    monkeypatch.setattr(st, "html", lambda body, **k: html_calls.append((body, k)))
    import selmakit.dashboard.app as app

    # No gateway: the turn fails fast, the history is recorded before that.
    monkeypatch.setattr(app.httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(app.httpx.ConnectError("x")))
    run(title="t", show_settings=False)


def test_typed_prompt_is_remembered_and_script_rendered(monkeypatch) -> None:
    st.session_state.input_history = []
    calls: List[Any] = []
    _run_with(monkeypatch, "wie wird das Wetter?", calls)

    assert st.session_state.input_history == ["wie wird das Wetter?"]
    scripts = [(b, k) for b, k in calls if "__skHistory" in b]
    assert len(scripts) == 1
    assert scripts[0][1].get("unsafe_allow_javascript") is True
    assert "wie wird das Wetter?" in scripts[0][0]


def test_approval_command_is_not_remembered(monkeypatch) -> None:
    st.session_state.input_history = []
    st.session_state.pending_prompt = "/approve"
    _run_with(monkeypatch, None, [])

    assert st.session_state.input_history == []
