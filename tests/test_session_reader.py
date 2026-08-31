"""The public reader for persisted session files.

Outside consumers (trace readers, benchmarks, graders) used to open
``sessions/<key>.json`` themselves, which is a dependency on the file layout
that no import reports when it breaks. These pin that `load_session_messages`
reads exactly what `JsonlStore` writes, and that a missing or damaged file is a
normal empty answer rather than an exception at the call site.
"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, UserPromptPart

from selmakit.session import (
    JsonlStore,
    load_session_messages,
    load_session_meta,
    session_file,
)


def test_reads_back_what_the_store_wrote(tmp_path):
    store = JsonlStore(str(tmp_path))
    store.save("s", [ModelRequest(parts=[UserPromptPart(content="hallo")])])
    store.set_meta("s", "last_validated_output", "geprüft")

    messages = load_session_messages(tmp_path, "s")
    assert [p["content"] for m in messages for p in m["parts"]] == ["hallo"]
    assert load_session_meta(tmp_path, "s")["last_validated_output"] == "geprüft"
    assert session_file(tmp_path, "s") == tmp_path / "s.json"


def test_missing_and_damaged_files_read_as_empty(tmp_path):
    assert load_session_messages(tmp_path, "nope") == []
    assert load_session_meta(tmp_path, "nope") == {}

    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "broken.meta.json").write_text("{ not json", encoding="utf-8")
    assert load_session_messages(tmp_path, "broken") == []
    assert load_session_meta(tmp_path, "broken") == {}
