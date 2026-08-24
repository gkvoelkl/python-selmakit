"""Turning the file paths an answer *names* into files a channel can *send*.

An agent that renders a map or a chart can only put its path into the answer
text. In a browser on the same host that is enough; over Telegram it is not, so
the channel re-reads its own answer and uploads what it finds.

**The text scanned here is model output, not user input.** Whatever the model
wrote arrives here as a plausible-looking path — including one it hallucinated,
and one a prompt-injected web page talked it into naming. Uploading a path
verbatim would therefore hand any file on the host to whoever is in the chat.
The containment check in ``_resolve_inside`` is what stands between "the answer
mentions ``/etc/passwd``" and "the bot sends ``/etc/passwd``": a candidate is
delivered only when it resolves — ``..`` collapsed, symlinks followed — inside
an explicit root. ``root=None`` disables the whole mechanism and is the default
everywhere, so no existing deployment starts uploading anything on upgrade.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

#: Suffixes a channel may render inline rather than as a file download.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

#: Default upload cap. The Telegram bot API rejects documents over 50 MB
#: outright, and a large upload over a slow link is its own kind of failure —
#: it stalls the turn the user is waiting on.
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# Token boundaries: whitespace plus the punctuation that wraps a path in prose
# and in markdown, so `see `out/map.html``, [map](file:///tmp/map.png) and
# "wrote /tmp/a.png, /tmp/b.png" all reduce to bare path tokens.
_SPLIT_RE = re.compile(r"[\s`\"'<>()\[\]{},;|]+")
_STRIP_CHARS = "*_"                     # markdown emphasis around a path
_TRAILING_PUNCTUATION = ".,;:!?"        # sentence punctuation after one
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


@dataclass(frozen=True)
class Attachment:
    """An existing file inside the root that the answer named."""

    path: Path      # resolved, symlink-free, inside the root
    size: int
    too_large: bool  # over the cap — the channel says so instead of uploading

    @property
    def is_image(self) -> bool:
        return self.path.suffix.lower() in IMAGE_SUFFIXES


def _candidates(text: str) -> Iterator[str]:
    """Path-shaped tokens in ``text``, in order of appearance.

    Deliberately generous — markdown links, ``file://`` URLs and bare paths all
    reduce to the same token here. Over-matching costs one ``stat`` on a name
    that turns out not to exist; under-matching means the artefact the user
    asked for never arrives.
    """
    for raw in _SPLIT_RE.split(text):
        token = raw.strip(_STRIP_CHARS).rstrip(_TRAILING_PUNCTUATION)
        if token.startswith("file://"):
            token = unquote(token[len("file://"):])
        # Any other scheme is a remote URL, not a local artefact.
        if not token or "://" in token:
            continue
        # Either it walks a directory or it names a file type. Extensionless
        # paths still count (``/etc/passwd`` is one) — they are refused by the
        # containment check, loudly, rather than never being looked at.
        if "/" not in token and not _EXTENSION_RE.search(token):
            continue
        yield token


def _resolve_inside(candidate: str, root: Path) -> Path | None:
    """``candidate`` as an existing file inside ``root``, or ``None``.

    A relative path is resolved against the root because that is how the agent
    addresses its own files: the harness ``FileSystem`` capability is sandboxed
    to the same directory, so an answer saying ``workspace/map.html`` means the
    file it just wrote there.

    ``resolve()`` before the comparison is what makes this a security check and
    not a string check. It collapses ``..`` and follows symlinks *first*, so
    neither a traversal (``workspace/../../../etc/passwd``) nor a symlink
    planted inside the root can name a file outside it.
    """
    try:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            logger.warning(
                "Attachment refused: %s resolves outside the attach root %s", candidate, root
            )
            return None
        if not resolved.is_file():
            return None  # an answer may name a file it never wrote — skip quietly
        return resolved
    except (OSError, RuntimeError, ValueError) as e:
        logger.debug("Attachment candidate %r unusable: %s", candidate, e)
        return None


def find_attachments(
    text: str,
    root: str | Path | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Attachment]:
    """Files named in ``text`` that exist inside ``root``, in order, deduplicated.

    ``root=None`` returns nothing at all: attaching is opt-in per channel.
    Oversized files are returned with ``too_large`` set rather than dropped, so
    the channel can say why nothing arrived instead of failing silently.
    """
    if root is None:
        return []

    root_path = Path(root)
    found: list[Attachment] = []
    seen: set[Path] = set()
    for candidate in _candidates(text):
        path = _resolve_inside(candidate, root_path)
        if path is None or path in seen:
            continue
        seen.add(path)  # the same artefact named twice is still sent once
        try:
            size = path.stat().st_size
        except OSError as e:
            logger.debug("Attachment %s unreadable: %s", path, e)
            continue
        found.append(Attachment(path=path, size=size, too_large=size > max_bytes))
    return found
