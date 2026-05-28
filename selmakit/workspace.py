from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

LOAD_ORDER = [
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "TOOLS.md",
    "MEMORY.md",
]


class WorkspaceFile(BaseModel):
    name: str
    content: str


def load_workspace_files(workspace_dir: str) -> list[WorkspaceFile]:
    workspace = Path(workspace_dir)
    result: list[WorkspaceFile] = []

    for filename in LOAD_ORDER:
        path = workspace / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if content.strip():
            logger.debug("Loaded workspace file: %s", filename)
            result.append(WorkspaceFile(name=filename, content=content))

    for delta in (0, 1):
        day = date.today() - timedelta(days=delta)
        path = workspace / "memory" / f"{day.isoformat()}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if content.strip():
                result.append(WorkspaceFile(name=path.name, content=content))

    bootstrap_path = workspace / "BOOTSTRAP.md"
    if bootstrap_path.exists():
        content = bootstrap_path.read_text(encoding="utf-8")
        if content.strip():
            result.append(WorkspaceFile(name="BOOTSTRAP.md", content=content))

    return result


def detect_bootstrap(workspace_dir: str) -> bool:
    path = Path(workspace_dir) / "BOOTSTRAP.md"
    if not path.exists():
        return False
    return bool(path.read_text(encoding="utf-8").strip())
