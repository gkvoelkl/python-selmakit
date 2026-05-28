"""
selmakit/tools.py

Pydantic-ai compatible tool functions for selmakit agents.

Filesystem tools (read, write, edit, ls, grep, find) are created via
make_filesystem_tools(cwd) because they resolve paths relative to a
working directory.

Web access is provided by pydantic-ai 2.0's WebSearch / WebFetch capabilities —
pass them via `capabilities=[...]` on the Agent, not as tools.

Usage:
    from pydantic_ai.capabilities import WebSearch, WebFetch
    from selmakit.tools import make_filesystem_tools

    agent = Agent(
        state_dir=".selmakit",
        tools=[*make_filesystem_tools(".")],
        capabilities=[WebSearch(local="duckduckgo"), WebFetch(local=True)],
    )
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# ── Constants ────────────────────────────────────────────────

_MAX_LINES = 2000
_MAX_BYTES = 50 * 1024


# ── Path helpers ─────────────────────────────────────────────

def _resolve(cwd: str, path_str: str) -> Path:
    p = Path(os.path.expanduser(path_str))
    return p if p.is_absolute() else Path(cwd) / p


# ── Truncation ───────────────────────────────────────────────

def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _truncate_head(content: str, start_line: int = 1) -> str:
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= _MAX_LINES and total_bytes <= _MAX_BYTES:
        return content

    if lines and len(lines[0].encode("utf-8")) > _MAX_BYTES:
        return f"[Line {start_line} exceeds {_format_size(_MAX_BYTES)} limit.]"

    kept: list[str] = []
    byte_count = 0
    truncated_by = "lines"

    for i, line in enumerate(lines):
        if i >= _MAX_LINES:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)
        if byte_count + line_bytes > _MAX_BYTES:
            truncated_by = "bytes"
            break
        kept.append(line)
        byte_count += line_bytes

    end_line = start_line + len(kept) - 1
    notice = (
        f"\n\n[Showing lines {start_line}-{end_line} of {start_line + total_lines - 1}. "
        f"Use offset={end_line + 1} to continue.]"
    )
    return "\n".join(kept) + notice


# ── Edit helpers ──────────────────────────────────────────────

def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_for_fuzzy(text: str) -> str:
    lines = [line.rstrip() for line in text.split("\n")]
    result = "\n".join(lines)
    result = re.sub(r"[‘’‚‛]", "'", result)
    result = re.sub(r'[“”„‟]', '"', result)
    result = re.sub(r"[‐‑‒–—―−]", "-", result)
    result = re.sub(r"[  -   　]", " ", result)
    return result


def _fuzzy_find(content: str, old_text: str) -> tuple[bool, int, int, str]:
    idx = content.find(old_text)
    if idx != -1:
        return True, idx, len(old_text), content
    fuzzy_content = _normalize_for_fuzzy(content)
    fuzzy_old = _normalize_for_fuzzy(old_text)
    fuzzy_idx = fuzzy_content.find(fuzzy_old)
    if fuzzy_idx == -1:
        return False, -1, 0, content
    return True, fuzzy_idx, len(fuzzy_old), fuzzy_content


# ── Filesystem tool factory ───────────────────────────────────

def make_filesystem_tools(cwd: str = ".") -> list:
    """
    Return [read, write, edit] tools bound to the given working directory.

    All path arguments are resolved relative to cwd.
    """

    def read(path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read file contents with optional line range.

        Output is truncated to 2000 lines or 50 KB. Use offset and limit for
        large files. offset is 1-based.
        """
        resolved = _resolve(cwd, path)
        if not resolved.exists():
            return f"Error: file not found: {resolved}"
        if not resolved.is_file():
            return f"Error: not a file: {resolved}"
        try:
            text = resolved.read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            return f"Error reading file: {e}"

        all_lines = text.split("\n")
        total_lines = len(all_lines)
        start = (offset - 1) if offset else 0
        if start >= total_lines:
            return f"Error: offset {offset} beyond end of file ({total_lines} lines)"

        selected_lines = all_lines[start : start + limit] if limit else all_lines[start:]
        selected = _truncate_head("\n".join(selected_lines), start_line=start + 1)

        notice_sep = "\n\n["
        if notice_sep in selected:
            body, notice = selected.split(notice_sep, 1)
            notice = notice_sep[2:] + notice
        else:
            body, notice = selected, ""

        numbered = "\n".join(
            f"{start + 1 + i}\t{line}"
            for i, line in enumerate(body.split("\n"))
        )
        return numbered + notice

    def write(path: str, content: str) -> str:
        """Write content to a file, creating parent directories as needed.

        Overwrites the file if it already exists.
        """
        resolved = _resolve(cwd, path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return f"Wrote {len(content.encode('utf-8'))} bytes to {path}."
        except OSError as e:
            return f"Error writing file: {e}"

    def edit(path: str, old_text: str, new_text: str) -> str:
        """Replace an exact unique occurrence of old_text with new_text in a file.

        Supports minor Unicode and trailing-whitespace differences via fuzzy
        matching. Fails if old_text is not found or appears more than once.
        """
        resolved = _resolve(cwd, path)
        if not resolved.exists():
            return f"Error: file not found: {path}"
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as e:
            return f"Error reading file: {e}"

        content = _normalize_to_lf(raw)
        norm_old = _normalize_to_lf(old_text)
        norm_new = _normalize_to_lf(new_text)

        found, idx, match_len, content_for_replacement = _fuzzy_find(content, norm_old)
        if not found:
            return (
                f"Error: text not found in {path}. "
                "old_text must match exactly including whitespace and newlines."
            )

        fuzzy_content = _normalize_for_fuzzy(content_for_replacement)
        fuzzy_old = _normalize_for_fuzzy(norm_old)
        occurrences = fuzzy_content.count(fuzzy_old)
        if occurrences > 1:
            return (
                f"Error: {occurrences} occurrences found in {path}. "
                "Provide more context to make old_text unique."
            )

        new_content = (
            content_for_replacement[:idx]
            + norm_new
            + content_for_replacement[idx + match_len:]
        )
        if new_content == content_for_replacement:
            return f"Error: replacement produces identical content in {path}."

        if "\r\n" in raw:
            new_content = new_content.replace("\n", "\r\n")

        try:
            resolved.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"Error writing file: {e}"

        return f"Successfully replaced text in {path}."

    _LS_LIMIT = 500
    _GREP_LIMIT = 100
    _FIND_LIMIT = 1000
    _GREP_MAX_LINE = 500

    def ls(path: str | None = None, limit: int | None = None) -> str:
        """List directory contents sorted alphabetically.

        Directories have a trailing '/'. Dotfiles are included.
        Output is truncated to 500 entries or 50 KB.
        """
        dir_path = _resolve(cwd, path or ".")
        effective_limit = limit or _LS_LIMIT
        if not dir_path.exists():
            return f"Error: path not found: {dir_path}"
        if not dir_path.is_dir():
            return f"Error: not a directory: {dir_path}"
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            return f"Error reading directory: {e}"

        results: list[str] = []
        limit_reached = False
        for entry in entries:
            if len(results) >= effective_limit:
                limit_reached = True
                break
            results.append(entry.name + ("/" if entry.is_dir() else ""))

        if not results:
            return "(empty directory)"

        output = "\n".join(results)
        notices: list[str] = []
        if limit_reached:
            notices.append(f"{effective_limit} entries limit reached. Use limit={effective_limit * 2} for more.")
        if len(output.encode("utf-8")) > _MAX_BYTES:
            output = _truncate_head(output)
            notices.append(f"{_format_size(_MAX_BYTES)} limit reached.")
        if notices:
            output += "\n\n[" + " ".join(notices) + "]"
        return output

    def grep(
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int | None = None,
    ) -> str:
        """Search file contents for a pattern.

        Returns matching lines with file paths and line numbers.
        Uses ripgrep (rg) when available, falls back to Python re.
        Output is truncated to 100 matches or 50 KB.
        """
        search_path = _resolve(cwd, path or ".")
        effective_limit = max(1, limit or _GREP_LIMIT)
        if not search_path.exists():
            return f"Error: path not found: {search_path}"

        def _rg_available() -> bool:
            try:
                subprocess.run(["rg", "--version"], capture_output=True, timeout=3)
                return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False

        lines: list[str] = []
        limit_reached = False

        if _rg_available():
            args = ["rg", "--line-number", "--color=never", "--hidden", "--no-heading"]
            if ignore_case:
                args.append("--ignore-case")
            if literal:
                args.append("--fixed-strings")
            if glob:
                args.extend(["--glob", glob])
            if context > 0:
                args.extend(["-C", str(context)])
            args.extend([pattern, str(search_path)])
            try:
                result = subprocess.run(args, capture_output=True, timeout=30)
                raw_lines = result.stdout.decode("utf-8", errors="replace").splitlines()
                limit_reached = len(raw_lines) > effective_limit
                lines = raw_lines[:effective_limit]
            except subprocess.TimeoutExpired:
                return "Error: grep timed out."
        else:
            flags = re.IGNORECASE if ignore_case else 0
            try:
                regex = re.compile(re.escape(pattern) if literal else pattern, flags)
            except re.error as e:
                return f"Error: invalid regex: {e}"

            files = (
                [search_path] if search_path.is_file()
                else sorted(search_path.rglob(glob) if glob else (p for p in search_path.rglob("*") if p.is_file()))
            )
            for fp in files:
                try:
                    file_lines = fp.read_text(encoding="utf-8", errors="replace").split("\n")
                except OSError:
                    continue
                try:
                    rel = str(fp.relative_to(search_path)).replace("\\", "/")
                except ValueError:
                    rel = fp.name
                for lineno, line in enumerate(file_lines, 1):
                    if len(lines) >= effective_limit:
                        limit_reached = True
                        break
                    if regex.search(line):
                        display = line if len(line) <= _GREP_MAX_LINE else line[:_GREP_MAX_LINE] + "... [truncated]"
                        for ci in range(context, 0, -1):
                            bi = lineno - 1 - ci
                            if bi >= 0:
                                lines.append(f"{rel}-{lineno - ci}- {file_lines[bi]}")
                        lines.append(f"{rel}:{lineno}: {display}")
                        for ci in range(1, context + 1):
                            ai = lineno - 1 + ci
                            if ai < len(file_lines):
                                lines.append(f"{rel}-{lineno + ci}- {file_lines[ai]}")
                if limit_reached:
                    break

        if not lines:
            return "No matches found."

        output = "\n".join(lines)
        notices: list[str] = []
        if limit_reached:
            notices.append(f"{effective_limit} matches limit reached. Use limit={effective_limit * 2} or refine pattern.")
        if len(output.encode("utf-8")) > _MAX_BYTES:
            output = _truncate_head(output)
            notices.append(f"{_format_size(_MAX_BYTES)} limit reached.")
        if notices:
            output += "\n\n[" + " ".join(notices) + "]"
        return output

    def find(pattern: str, path: str | None = None, limit: int | None = None) -> str:
        """Search for files by glob pattern.

        Returns matching file paths relative to the search directory.
        Output is truncated to 1000 results or 50 KB.
        """
        search_path = _resolve(cwd, path or ".")
        effective_limit = limit or _FIND_LIMIT
        if not search_path.exists():
            return f"Error: path not found: {search_path}"
        try:
            matches = sorted(search_path.rglob(pattern))
        except Exception as e:
            return f"Error searching: {e}"
        if not matches:
            return "No files found matching pattern."

        limit_reached = len(matches) > effective_limit
        matches = matches[:effective_limit]

        lines: list[str] = []
        for p in matches:
            try:
                rel = p.relative_to(search_path)
            except ValueError:
                rel = p
            lines.append(str(rel).replace("\\", "/"))

        output = "\n".join(lines)
        notices: list[str] = []
        if limit_reached:
            notices.append(f"{effective_limit} results limit reached. Use limit={effective_limit * 2} or refine pattern.")
        if len(output.encode("utf-8")) > _MAX_BYTES:
            output = _truncate_head(output)
            notices.append(f"{_format_size(_MAX_BYTES)} limit reached.")
        if notices:
            output += "\n\n[" + " ".join(notices) + "]"
        return output

    return [read, write, edit, ls, grep, find]
