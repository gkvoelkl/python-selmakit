"""Artefact delivery for the Telegram channel (channels.telegram.attach_files).

Everything here drives ``TelegramReply.done()`` with a fake message object — no
bot token, no network, no python-telegram-bot import. The message records what
would have been uploaded.

The containment tests are the load-bearing ones: the text being scanned is the
model's own answer, so a path outside the attach root must never be sent, and
neither must a symlink that points out of it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from selmakit.attachments import (
    DEFAULT_MAX_BYTES,
    find_attachments,
    pair_renderable_siblings,
)
from selmakit.channels.telegram import TelegramReply
from selmakit.config import TelegramConfig


class _FakeMessage:
    """Stands in for a telegram ``Message``, recording every send."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.photos: list[Path] = []
        self.documents: list[Path] = []
        self.actions: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)

    async def reply_chat_action(self, action: str) -> None:
        self.actions.append(action)

    async def reply_photo(self, photo, caption=None) -> None:
        self.photos.append(Path(photo))

    async def reply_document(self, document, caption=None) -> None:
        self.documents.append(Path(document))


def _deliver(text: str, attach_root: Path | None, msg: _FakeMessage | None = None) -> _FakeMessage:
    """Run one full turn: the agent said ``text``, the channel flushes it."""
    msg = msg or _FakeMessage()
    reply = TelegramReply(msg, attach_root=attach_root)
    asyncio.run(_send(reply, text))
    return msg


async def _send(reply: TelegramReply, text: str) -> None:
    await reply.send_chunk(text)
    await reply.done()


def test_image_inside_root_is_sent_as_a_photo(tmp_path):
    """Photos render inline in the chat — that is the point of the feature."""
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    msg = _deliver(f"Here is the map: {tmp_path / 'map.png'}", tmp_path)

    assert msg.photos == [tmp_path / "map.png"]
    assert msg.documents == []


def test_other_file_inside_root_is_sent_as_a_document(tmp_path):
    (tmp_path / "report.html").write_text("<html></html>")

    msg = _deliver(f"Report written to {tmp_path / 'report.html'}", tmp_path)

    assert msg.documents == [tmp_path / "report.html"]
    assert msg.photos == []


def test_relative_path_resolves_against_the_root(tmp_path):
    """How the agent actually names files: its file tools are sandboxed to the
    same directory, so it reports ``workspace/…``, not an absolute path."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "map.png").write_bytes(b"\x89PNG")

    msg = _deliver("Saved to `workspace/map.png`.", tmp_path)

    assert msg.photos == [tmp_path / "workspace" / "map.png"]


def test_path_outside_the_root_is_never_sent(tmp_path, caplog):
    """The answer is model output. A path it names outside the root — coaxed,
    hallucinated or traversed — must not leave the host."""
    root = tmp_path / "state"
    root.mkdir()
    secret = tmp_path / "passwd"
    secret.write_text("root:x:0:0")

    with caplog.at_level(logging.WARNING):
        msg = _deliver(f"Sure, here you go: {secret}", root)

    assert msg.photos == [] and msg.documents == []
    assert "outside" in caplog.text


def test_traversal_out_of_the_root_is_never_sent(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (tmp_path / "passwd").write_text("root:x:0:0")

    msg = _deliver("See ../passwd for details.", root)

    assert msg.documents == []


def test_symlink_pointing_outside_the_root_is_never_sent(tmp_path):
    """Containment is checked after resolving links, not on the spelling of the
    path — a link planted inside the root still points outside it."""
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG")
    (root / "innocent.png").symlink_to(outside)

    msg = _deliver(f"Rendered {root / 'innocent.png'}", root)

    assert msg.photos == [] and msg.documents == []


def test_missing_file_is_skipped_quietly(tmp_path):
    """An answer may name a file it never wrote."""
    msg = _deliver(f"I saved it to {tmp_path / 'nope.png'}.", tmp_path)

    assert msg.photos == [] and msg.documents == []
    assert len(msg.replies) == 1  # only the answer text itself


def test_the_same_path_named_twice_is_sent_once(tmp_path):
    (tmp_path / "map.png").write_bytes(b"\x89PNG")
    path = tmp_path / "map.png"

    msg = _deliver(f"Wrote {path}. As promised, {path} is the map.", tmp_path)

    assert msg.photos == [path]


def test_oversized_file_is_explained_not_uploaded(tmp_path):
    """A file over the cap gets one line saying so — not silence, not a stalled
    upload. The cap is squeezed here instead of writing 20 MB to disk."""
    big = tmp_path / "huge.png"
    big.write_bytes(b"\0" * 64)

    msg = _FakeMessage()
    reply = TelegramReply(msg, attach_root=tmp_path, max_bytes=8)
    asyncio.run(_send(reply, f"See {big}"))

    assert msg.photos == [] and msg.documents == []
    assert any("huge.png" in r and "too large" in r for r in msg.replies)
    # The same file is under the shipped cap, so nothing about it is inherently big.
    assert big.stat().st_size < DEFAULT_MAX_BYTES


def test_attach_root_none_attaches_nothing(tmp_path):
    """The default: an existing deployment behaves exactly as before."""
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    msg = _deliver(f"Here: {tmp_path / 'map.png'}", None)

    assert msg.photos == [] and msg.documents == []
    assert len(msg.replies) == 1


def test_markdown_link_and_file_url_are_recognised(tmp_path):
    (tmp_path / "map.png").write_bytes(b"\x89PNG")
    (tmp_path / "report.html").write_text("<html></html>")

    msg = _deliver(
        f"[the map](file://{tmp_path / 'map.png'}) and **{tmp_path / 'report.html'}**",
        tmp_path,
    )

    assert msg.photos == [tmp_path / "map.png"]
    assert msg.documents == [tmp_path / "report.html"]


def test_http_urls_are_not_treated_as_files(tmp_path):
    msg = _deliver("Source: https://example.com/data.png", tmp_path)

    assert msg.photos == [] and msg.documents == []


def test_upload_failure_does_not_break_the_turn(tmp_path, caplog):
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    class _FailingMessage(_FakeMessage):
        async def reply_photo(self, photo, caption=None) -> None:
            raise RuntimeError("Telegram is down")

    msg = _FailingMessage()
    with caplog.at_level(logging.WARNING):
        _deliver(f"Here: {tmp_path / 'map.png'}", tmp_path, msg)

    assert msg.replies  # the answer itself still arrived
    assert "could not send" in caplog.text


def test_attach_files_defaults_to_off():
    assert TelegramConfig().attach_files is False


# -- picking a form the channel can show ----------------------------------
#
# The answer names whichever form the agent happened to write about, and the
# agent must not know which channel is reading. Telegram's viewer shows a
# folium map as a blank page, so a `.png` lying beside the `.html` goes out
# too. The rule is a convention (same dir, same stem), not a search.


def test_unrenderable_file_brings_its_sibling_picture(tmp_path):
    (tmp_path / "map.html").write_text("<html>needs a CDN</html>")
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    msg = _deliver(f"The map is at {tmp_path / 'map.html'}", tmp_path)

    # Both go out: the HTML is still worth opening on a desktop later.
    assert msg.photos == [tmp_path / "map.png"]
    assert msg.documents == [tmp_path / "map.html"]


def test_the_picture_is_sent_before_the_file_it_stands_in_for(tmp_path):
    """Order is the feature: the reader should see the map without scrolling."""
    (tmp_path / "map.html").write_text("<html></html>")
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    class _OrderedMessage(_FakeMessage):
        def __init__(self):
            super().__init__()
            self.order: list[str] = []

        async def reply_photo(self, photo, caption=None) -> None:
            await super().reply_photo(photo, caption)
            self.order.append("photo")

        async def reply_document(self, document, caption=None) -> None:
            await super().reply_document(document, caption)
            self.order.append("document")

    msg = _deliver(f"See {tmp_path / 'map.html'}", tmp_path, _OrderedMessage())

    assert msg.order == ["photo", "document"]


def test_no_sibling_means_the_file_goes_out_alone(tmp_path):
    """Nothing beside it — exactly the behaviour before this feature existed."""
    (tmp_path / "report.html").write_text("<html></html>")

    msg = _deliver(f"Report: {tmp_path / 'report.html'}", tmp_path)

    assert msg.documents == [tmp_path / "report.html"]
    assert msg.photos == []


def test_a_sibling_the_answer_also_names_is_sent_once(tmp_path):
    (tmp_path / "map.html").write_text("<html></html>")
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    msg = _deliver(
        f"Interactive: {tmp_path / 'map.html'} — a picture: {tmp_path / 'map.png'}",
        tmp_path,
    )

    assert msg.photos == [tmp_path / "map.png"]
    assert msg.documents == [tmp_path / "map.html"]


def test_a_sibling_pointing_outside_the_root_is_never_sent(tmp_path):
    """The sibling is derived from model output too, so it is not trusted more
    than the path that produced it."""
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG")
    (root / "map.html").write_text("<html></html>")
    (root / "map.png").symlink_to(outside)

    msg = _deliver(f"See {root / 'map.html'}", root)

    assert msg.photos == []
    assert msg.documents == [root / "map.html"]


def test_a_channel_that_renders_html_substitutes_nothing(tmp_path):
    """The renderable set lives in the channel, so a channel that can show HTML
    — web chat — keeps sending exactly what the answer named."""
    (tmp_path / "map.html").write_text("<html></html>")
    (tmp_path / "map.png").write_bytes(b"\x89PNG")

    found = find_attachments(f"See {tmp_path / 'map.html'}", tmp_path)
    paired = pair_renderable_siblings(
        found,
        tmp_path,
        renderable={".html", ".png"},
        substitutes=(".png",),
    )

    assert [a.path for a in paired] == [tmp_path / "map.html"]


def test_pairing_is_off_without_a_root(tmp_path):
    """``root=None`` is the disabled state of the whole mechanism, here too."""
    assert pair_renderable_siblings([], None, renderable=set(), substitutes=(".png",)) == []
