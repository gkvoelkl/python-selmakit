import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

_adapter: TypeAdapter[list[ModelMessage]] = TypeAdapter(list[ModelMessage])


class JsonlStore:
    """
    JSONL-based session persistence.

    Per session key:
      <path>/<session_key>.json       — message history
      <path>/<session_key>.meta.json  — metadata (thinking level, last_interaction_at, …)
    """

    def __init__(
        self,
        path: str,
        max_tokens: int = 50_000,
        compaction_strategy: str = "none",
        at_hour: int = 4,
        idle_minutes: int | None = None,
    ):
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.max_tokens = max_tokens
        self.compaction_strategy = compaction_strategy
        self._at_hour = at_hour
        self._idle_minutes = idle_minutes

    # ── Messages ─────────────────────────────────────────────

    def load(self, session_key: str) -> list[ModelMessage]:
        file = self._dir / f"{session_key}.json"
        if not file.exists():
            return []
        return _adapter.validate_json(file.read_bytes())

    def save(self, session_key: str, messages: list[ModelMessage]) -> None:
        file = self._dir / f"{session_key}.json"
        file.write_bytes(_adapter.dump_json(messages))

    def clear(self, session_key: str) -> None:
        """Delete the session message history."""
        file = self._dir / f"{session_key}.json"
        if file.exists():
            file.unlink()

    # ── Metadata ─────────────────────────────────────────────

    def _meta_path(self, session_key: str) -> Path:
        return self._dir / f"{session_key}.meta.json"

    def _load_meta(self, session_key: str) -> dict:
        p = self._meta_path(session_key)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_meta(self, session_key: str, meta: dict) -> None:
        self._meta_path(session_key).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def get_meta(self, session_key: str, key: str, default=None):
        return self._load_meta(session_key).get(key, default)

    def set_meta(self, session_key: str, key: str, value) -> None:
        meta = self._load_meta(session_key)
        meta[key] = value
        self._save_meta(session_key, meta)

    def touch(self, session_key: str) -> None:
        """Update last_interaction_at after each agent turn."""
        self.set_meta(session_key, "last_interaction_at", datetime.now(timezone.utc).isoformat())

    # ── Stale detection ───────────────────────────────────────

    def list_sessions(self, session_type: str | None = None) -> list[dict]:
        """List sessions with metadata. Optionally filter by session_type ('user', 'schedule')."""
        sessions = []
        for json_file in self._dir.glob("*.json"):
            if json_file.name.endswith(".meta.json"):
                continue
            key = json_file.stem
            meta = self._load_meta(key)
            stype = meta.get("session_type", "user")
            if session_type is not None and stype != session_type:
                continue
            sessions.append({
                "session_key": key,
                "session_type": stype,
                "last_interaction_at": meta.get("last_interaction_at"),
                "thinking": meta.get("thinking"),
            })
        return sorted(sessions, key=lambda s: s["last_interaction_at"] or "", reverse=True)

    def is_fresh(self, session_key: str) -> bool:
        """Return True when the session does not need an auto-reset."""
        at_hour = self._at_hour
        idle_minutes = self._idle_minutes
        last_str = self.get_meta(session_key, "last_interaction_at")
        if not last_str:
            return True
        try:
            last = datetime.fromisoformat(last_str)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            return True

        now = datetime.now(timezone.utc)

        # Daily reset: stale when last interaction was before today's at_hour boundary
        now_local = datetime.now()
        boundary = now_local.replace(hour=at_hour, minute=0, second=0, microsecond=0)
        if now_local < boundary:
            boundary -= timedelta(days=1)
        boundary_utc = boundary.astimezone(timezone.utc)
        if last < boundary_utc:
            return False

        # Idle reset
        if idle_minutes is not None and idle_minutes > 0:
            if now > last + timedelta(minutes=idle_minutes):
                return False

        return True
