from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_HEARTBEAT_OK = "HEARTBEAT_OK"


class ScheduleContext(BaseModel):
    session_key: str
    workspace_dir: str


class ScheduleConfig(BaseModel):
    every: str
    active_hours: tuple[str, str] | None = None
    timezone: str = "UTC"
    target: str = "last"
    isolated_session: bool = True
    ack_max_chars: int = 300


def parse_interval_seconds(every: str) -> int:
    s = every.strip().lower()
    _units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for suffix, factor in _units.items():
        if s.endswith(suffix):
            try:
                return int(s[:-1]) * factor
            except ValueError:
                break
    logger.warning("Schedule: unknown interval %r — disabled", every)
    return 0


def _within_active_hours(start: str, end: str, timezone: str) -> bool:
    try:
        tz = ZoneInfo(timezone)
        now: dtime = datetime.now(tz).time()
        return dtime.fromisoformat(start) <= now <= dtime.fromisoformat(end)
    except Exception as e:
        logger.warning("Schedule: active_hours error | %s", e)
        return True


def _strip_ack_token(text: str) -> tuple[str, bool]:
    """Remove HEARTBEAT_OK from start/end. Returns (cleaned, was_stripped)."""
    trailing_re = re.compile(re.escape(_HEARTBEAT_OK) + r"[^\w]{0,4}$")
    changed = True
    did_strip = False
    while changed:
        changed = False
        t = text.strip()
        if t.startswith(_HEARTBEAT_OK):
            text = t[len(_HEARTBEAT_OK):].lstrip()
            did_strip = True
            changed = True
            continue
        if trailing_re.search(t):
            idx = t.rfind(_HEARTBEAT_OK)
            text = (t[:idx].rstrip() + t[idx + len(_HEARTBEAT_OK):].lstrip()).rstrip()
            did_strip = True
            changed = True
    return " ".join(text.split()), did_strip


def _is_heartbeat_user_message(msg: object) -> bool:
    for part in getattr(msg, "parts", []):
        if getattr(part, "part_kind", None) == "user-prompt":
            content = getattr(part, "content", "")
            if isinstance(content, str) and content.startswith("Heartbeat check."):
                return True
    return False


def filter_heartbeat_messages(messages: list) -> list:
    """Remove heartbeat turn pairs (user prompt + assistant reply) from a message list.

    Safe to call on any message list; returns the list unchanged if no
    heartbeat turns are found. Useful when heartbeat runs non-isolated
    and its turns would otherwise appear in compaction summaries or the UI.
    """
    result: list = []
    skip_next = False
    for msg in messages:
        if skip_next:
            skip_next = False
            continue
        if _is_heartbeat_user_message(msg):
            skip_next = True  # drop this user turn and the following assistant reply
            continue
        result.append(msg)
    return result


def build_heartbeat_prompt(workspace_dir: str) -> str:
    """Return heartbeat prompt from HEARTBEAT.md, or '' to skip the turn."""
    from pathlib import Path

    path = Path(workspace_dir) / "HEARTBEAT.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    # Strip comment lines and blanks to decide if file is effectively empty
    actionable = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not actionable:
        return ""
    return (
        "Heartbeat check. Work through the tasks listed in HEARTBEAT.md:\n\n"
        + content.strip()
        + "\n\nFor each task: check, act if needed, report findings concisely. "
        "Then call heartbeat_respond to signal your outcome."
    )


def is_silent_ack(text: str, max_chars: int = 300) -> bool:
    """Return True when the reply is purely a heartbeat ack."""
    if not text or not text.strip():
        return True
    cleaned, stripped = _strip_ack_token(text.strip())
    if not stripped:
        return False
    return not cleaned or len(cleaned) <= max_chars


class ScheduleRunner:
    def __init__(
        self,
        handler: any,
        cfg: ScheduleConfig,
        agent: any,
        alerts: asyncio.Queue,
    ):
        self._handler = handler
        self._cfg = cfg
        self._agent = agent
        self._alerts = alerts
        self.next_run_at: datetime | None = None

    async def run(self) -> None:
        interval_s = parse_interval_seconds(self._cfg.every)
        if interval_s == 0:
            logger.info("Schedule disabled (every=0)")
            return

        logger.info("Schedule started | every=%s (%ds)", self._cfg.every, interval_s)

        while True:
            self.next_run_at = datetime.now().astimezone().replace(microsecond=0)
            from datetime import timedelta
            self.next_run_at += timedelta(seconds=interval_s)
            await asyncio.sleep(interval_s)
            self.next_run_at = None

            if self._cfg.active_hours:
                start, end = self._cfg.active_hours
                if not _within_active_hours(start, end, self._cfg.timezone):
                    logger.debug("Schedule skipped: outside active_hours")
                    continue

            store = getattr(self._agent, "_session_store", None)
            if not self._cfg.isolated_session and store is not None:
                user_sessions = store.list_sessions(session_type="user")
                session_key = user_sessions[0]["session_key"] if user_sessions else "schedule:main"
            elif self._cfg.isolated_session:
                session_key = f"schedule:{uuid.uuid4().hex[:8]}"
            else:
                session_key = "schedule:main"

            if self._cfg.isolated_session and store is not None:
                store.set_meta(session_key, "session_type", "schedule")

            ctx = ScheduleContext(
                session_key=session_key,
                workspace_dir=str(self._agent.workspace_dir),
            )

            try:
                prompt = await self._handler(ctx)
            except Exception as e:
                logger.error("Schedule handler error | %s", e)
                continue

            if not prompt:
                continue

            from selmakit.capabilities import HeartbeatCapability

            hb_cap = HeartbeatCapability()
            chunks: list[str] = []
            try:
                async with self._agent.run_stream(
                    prompt, session_key=session_key, extra_capabilities=[hb_cap], unattended=True
                ) as result:
                    async for chunk in result.stream_text(delta=True):
                        chunks.append(chunk)
            except Exception as e:
                logger.error("Schedule agent turn failed | %s", e)
                continue

            if hb_cap.was_called:
                # Structured result from heartbeat_respond tool
                if hb_cap.should_alert:
                    logger.info("Schedule alert (tool) | chars=%d", len(hb_cap.alert_text))
                    if self._cfg.target == "last":
                        await self._alerts.put({"kind": "heartbeat", "prompt": prompt, "reply": hb_cap.alert_text})
                else:
                    logger.debug("Schedule: heartbeat_respond(notify=False) — silent")
            else:
                # Fallback: scan raw text for HEARTBEAT_OK
                reply = "".join(chunks).strip()
                if not reply or is_silent_ack(reply, self._cfg.ack_max_chars):
                    logger.debug("Schedule: silent ack (text) — no delivery")
                else:
                    logger.info("Schedule alert (text) | chars=%d", len(reply))
                    if self._cfg.target == "last":
                        await self._alerts.put({"kind": "heartbeat", "prompt": prompt, "reply": reply})
