"""Workspace bootstrap — creates ``.selmakit/`` with config and workspace files.

Exposed as ``selmakit init`` (see :mod:`selmakit.cli`). Every step is
skip-if-exists, so running it again on a live workspace is safe.

The config is written from ``SelmaKitConfig().model_dump()`` rather than a
hand-maintained dict: the defaults can then never drift out of sync with the
schema in :mod:`selmakit.config`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from rich import print

from selmakit.config import SelmaKitConfig

# ── Default workspace files ──────────────────────────────────────────────────

DEFAULT_WORKSPACE_FILES = {
    "SOUL.md": """\
# SOUL.md - Who You Are

You're not a chatbot. You're becoming someone.

## Core Truths

**Be genuinely helpful, not performatively helpful.**
No "Great question!" — just help.

**Have opinions.**
You're allowed to disagree, prefer things, find something boring.
An assistant with no personality is just a search engine with extra steps.

**Try first, ask second.**
Read the file. Check the context. Search. Then ask if you're stuck.

**Private things stay private.**
You have access to the user's files. Treat that with respect.

## Continuity

Every session you start fresh. These files are your memory.
Read them. Update them.

---

_This file is yours. Evolve it over time._
""",
    "IDENTITY.md": """\
Name: Agent
Role: Personal Assistant
""",
    "USER.md": """\
User: (your name here)
Preferences: (describe your preferences and interests)
""",
    "HEARTBEAT.md": """\
# HEARTBEAT.md

# Leave empty to skip heartbeat calls.
# Add tasks below when the agent should check something periodically.

# Examples:
# - Check emails for anything urgent
# - Calendar: any events in the next 24h?
# - Review open tasks from memory/
""",
    "BOOTSTRAP.md": """\
# BOOTSTRAP.md - First Run

You just came online. Time to figure out who you are.

## The Conversation

Start casually, not robotically. Something like:

> "Hey. I just started up. Who am I? Who are you?"

Figure out together:

1. **Your name** — What should they call you?
2. **Your nature** — What kind of thing are you?
3. **Your vibe** — Formal? Casual? Direct? Warm?
4. **Your emoji** — Your signature.

## Afterwards

Write what you learned into these files:

- `IDENTITY.md` — your name, nature, vibe, emoji
- `USER.md` — the user's name, how to address them, timezone

Then go through `SOUL.md` together:
- What matters to them
- How they want you to behave
- Boundaries and preferences

## When you're done

Remove the complete content of this file. You don't need a bootstrap script anymore.
""",
}

# ── .env.example ────────────────────────────────────────────────────────────

ENV_EXAMPLE = """\
# Telegram bot token (required if Telegram channel is used)
TELEGRAM_TOKEN=your-token-here
"""


def init(state_dir: str = ".selmakit") -> None:
    """Initialize the selmakit directory structure, config, and workspace files."""
    base = Path.cwd()
    selmakit_dir = Path(state_dir)
    if not selmakit_dir.is_absolute():
        selmakit_dir = base / selmakit_dir
    config_path = selmakit_dir / "selmakit.json"
    workspace_dir = selmakit_dir / "workspace"
    memory_dir = workspace_dir / "memory"
    skills_dst = workspace_dir / "skills"
    skills_src = base / "skills"
    env_example = base / ".env.example"

    print(f"[bold blue]Initializing selmakit[/bold blue]\n[dim]Root: {base}[/dim]")

    # 1. State directory
    _ensure_dir(selmakit_dir, base)

    # 2. Config file — defaults taken straight from the pydantic schema
    if not config_path.exists():
        config_path.write_text(
            json.dumps(SelmaKitConfig().model_dump(), indent=4),
            encoding="utf-8",
        )
        print(f"[green]✔[/green] Created config:    [cyan]{_rel(config_path, base)}[/cyan]")
    else:
        print(f"[yellow]![/yellow] Config exists:     [cyan]{_rel(config_path, base)}[/cyan]  (skipped)")

    # 3. Workspace directory
    _ensure_dir(workspace_dir, base)

    # 4. Memory directory + index file
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_index = memory_dir / "MEMORY.md"
    if not memory_index.exists():
        memory_index.write_text("# Memory\n", encoding="utf-8")
        print("[green]✔[/green] Created:           [cyan]workspace/memory/MEMORY.md[/cyan]")
    else:
        print("[yellow]![/yellow] Already exists:    [cyan]workspace/memory/MEMORY.md[/cyan]  (skipped)")

    # 5. Default workspace files (SOUL.md, IDENTITY.md, USER.md, HEARTBEAT.md, BOOTSTRAP.md)
    _deploy_workspace_files(workspace_dir)

    # 6. Skills directory
    skills_dst.mkdir(parents=True, exist_ok=True)

    # 7. Copy skills from skills/ → workspace/skills/ (if source exists)
    _deploy_skills(skills_src, skills_dst)

    # 8. .env.example
    if not env_example.exists():
        env_example.write_text(ENV_EXAMPLE, encoding="utf-8")
        print("[green]✔[/green] Created:           [cyan].env.example[/cyan]")
    else:
        print("[yellow]![/yellow] Already exists:    [cyan].env.example[/cyan]  (skipped)")

    print("\n[bold green]Setup complete.[/bold green]")
    print(f"[dim]Edit [cyan]{_rel(config_path, base)}[/cyan] to configure the model and channels.[/dim]")
    print("[dim]Edit workspace files (SOUL.md, IDENTITY.md, USER.md) to give the agent its identity.[/dim]")


def _rel(path: Path, base: Path) -> str:
    """Path relative to ``base`` when it is below it, else the absolute path."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _ensure_dir(path: Path, base: Path) -> None:
    rel = _rel(path, base)
    if not path.exists():
        path.mkdir(parents=True)
        print(f"[green]✔[/green] Created directory: [cyan]{rel}[/cyan]")
    else:
        print(f"[yellow]![/yellow] Already exists:    [cyan]{rel}[/cyan]  (skipped)")


def _deploy_workspace_files(workspace_dir: Path) -> None:
    for name, content in DEFAULT_WORKSPACE_FILES.items():
        dest = workspace_dir / name
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            print(f"[green]✔[/green] Created:           [cyan]workspace/{name}[/cyan]")
        else:
            print(f"[yellow]![/yellow] Already exists:    [cyan]workspace/{name}[/cyan]  (skipped)")


def _deploy_skills(skills_src: Path, skills_dst: Path) -> None:
    if not skills_src.exists():
        print("[dim]  No skills/ directory found — skipping skill deployment.[/dim]")
        return

    skill_dirs = sorted(
        d for d in skills_src.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )
    if not skill_dirs:
        print("[dim]  No skills found in skills/ — skipping.[/dim]")
        return

    copied = 0
    skipped = 0
    for src_dir in skill_dirs:
        dst_dir = skills_dst / src_dir.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        new_files = [f for f in src_dir.iterdir() if f.is_file() and not (dst_dir / f.name).exists()]
        if not new_files:
            skipped += 1
            continue
        for f in new_files:
            shutil.copy2(f, dst_dir / f.name)
        print(f"[green]✔[/green] Skill deployed:    [cyan]{src_dir.name}[/cyan]  ({len(new_files)} file(s))")
        copied += 1

    if skipped and not copied:
        print("[yellow]![/yellow] All skills already present in workspace/skills/  (skipped)")
