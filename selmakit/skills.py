from __future__ import annotations

import hashlib
import re
from pathlib import Path


def _find_skill_files(workspace_dir: str) -> list[Path]:
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.exists():
        return []
    return sorted(skills_dir.glob("*/SKILL.md"))


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        m = re.match(r'^([\w-]+):\s*"?(.*?)"?\s*$', line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def get_skills_version(workspace_dir: str) -> str:
    """Return a 12-char SHA-256 hash of all SKILL.md contents, or 'v0' when none exist."""
    files = _find_skill_files(workspace_dir)
    if not files:
        return "v0"
    h = hashlib.sha256()
    for path in files:
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def list_skills(workspace_dir: str) -> list[dict[str, str]]:
    """Return [{name, description}, ...] for all skills in the workspace."""
    result = []
    for path in _find_skill_files(workspace_dir):
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        result.append({
            "name": fm.get("name", path.parent.name),
            "description": fm.get("description", ""),
        })
    return result


def load_skill(workspace_dir: str, name: str) -> str | None:
    """Return the full content of the named SKILL.md, or None if not found."""
    for path in _find_skill_files(workspace_dir):
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        skill_name = fm.get("name", path.parent.name)
        if skill_name.lower() == name.lower():
            return path.read_text(encoding="utf-8")
    return None


def get_skill_path(workspace_dir: str, name: str) -> Path | None:
    """Return the Path to the named SKILL.md, or None if not found."""
    for path in _find_skill_files(workspace_dir):
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        skill_name = fm.get("name", path.parent.name)
        if skill_name.lower() == name.lower():
            return path
    return None


def build_skills_xml(workspace_dir: str) -> str:
    """Return an XML block listing all skills found in <workspace>/skills/*/SKILL.md."""
    files = _find_skill_files(workspace_dir)
    if not files:
        return ""

    xml_parts: list[str] = ["<available_skills>"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        name = fm.get("name", path.parent.name)
        description = fm.get("description", "")
        xml_parts += [
            "  <skill>",
            f"    <name>{name}</name>",
            f"    <description>{description}</description>",
            f"    <location>{path}</location>",
            "  </skill>",
        ]
    xml_parts.append("</available_skills>")
    return "\n".join(xml_parts)
