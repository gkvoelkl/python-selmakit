"""
selmakit/cron.py

Agent-manageable scheduled tasks (cron jobs).

The agent creates jobs via the `cron` tool (provided by CronCapability).
CronService runs as an asyncio task alongside the gateway and fires jobs
by placing their prompt text into the alerts queue.

Job kinds:
  at     — one-shot, fires once at a specific datetime, then deactivated
  every  — recurring, fires at the configured interval
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

logger = logging.getLogger(__name__)


# ── Model ──────────────────────────────────────────────────────


class CronJob(BaseModel):
    id: str
    prompt: str
    kind: Literal["at", "every"]
    at: datetime | None = None
    every: str | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    active: bool = True
    created_at: datetime


# ── Store ──────────────────────────────────────────────────────


class CronStore:
    """JSON-backed persistence for cron jobs."""

    def __init__(self, path: str):
        self._path = Path(path)

    def load(self) -> list[CronJob]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return [CronJob(**d) for d in data]

    def save(self, jobs: list[CronJob]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([j.model_dump(mode="json") for j in jobs], indent=2),
            encoding="utf-8",
        )

    def add(self, job: CronJob) -> None:
        jobs = self.load()
        jobs.append(job)
        self.save(jobs)

    def remove(self, job_id: str) -> bool:
        jobs = self.load()
        filtered = [j for j in jobs if j.id != job_id]
        if len(filtered) == len(jobs):
            return False
        self.save(filtered)
        return True

    def update(self, job: CronJob) -> None:
        jobs = self.load()
        self.save([job if j.id == job.id else j for j in jobs])


# ── Time parsing ───────────────────────────────────────────────


def parse_at(text: str) -> datetime | None:
    """Parse a human-readable or ISO datetime string.

    Supported formats:
      "in 2h" / "in 30m" / "in 3d"      — relative offset from now
      "tomorrow 09:00" / "morgen 09:00"  — next day at given time
      "2026-05-24T09:00"                 — ISO datetime
      "2026-05-24 09:00"                 — ISO datetime (space separator)
    """
    text = text.strip()
    now = datetime.now().astimezone()

    lower = text.lower()

    # Relative: "in Xm/h/d"
    if lower.startswith("in "):
        rest = lower[3:].strip()
        try:
            if rest.endswith("m"):
                return now + timedelta(minutes=int(rest[:-1]))
            if rest.endswith("h"):
                return now + timedelta(hours=int(rest[:-1]))
            if rest.endswith("d"):
                return now + timedelta(days=int(rest[:-1]))
        except ValueError:
            pass

    # Tomorrow: "tomorrow HH:MM" or "morgen HH:MM"
    if lower.startswith(("tomorrow ", "morgen ")):
        time_str = text.split(None, 1)[1].strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                t = datetime.strptime(time_str, fmt).time()
                tomorrow = (now + timedelta(days=1)).date()
                return datetime.combine(tomorrow, t, tzinfo=now.tzinfo)
            except ValueError:
                continue

    # Today: "today HH:MM" or "heute HH:MM"
    if lower.startswith(("today ", "heute ")):
        time_str = text.split(None, 1)[1].strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                t = datetime.strptime(time_str, fmt).time()
                return datetime.combine(now.date(), t, tzinfo=now.tzinfo)
            except ValueError:
                continue

    # ISO / absolute
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=now.tzinfo)
        except ValueError:
            continue

    # Bare time: "15:30" or "15:30 Uhr" → today, or tomorrow if already past
    time_str = lower.removesuffix(" uhr").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(time_str, fmt).time()
            dt = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
            if dt <= now:
                dt += timedelta(days=1)  # already past → schedule for tomorrow
            return dt
        except ValueError:
            continue

    return None


# ── Service ────────────────────────────────────────────────────


class CronService:
    """Asyncio scheduler that fires cron jobs as real agent turns.

    Each fired job runs an agent turn in the last active user session so that
    the interaction is saved to history and the agent retains the context.
    The structured result (prompt + reply) is placed in agent.alerts for
    the UI to display.
    """

    def __init__(self, store: CronStore, agent: Any):
        self._store = store
        self._agent = agent

    async def _fire(self, job: CronJob) -> None:
        # Target: last active user session, fall back to a dedicated cron session
        session_key = "cron:main"
        session_store = getattr(self._agent, "_session_store", None)
        if session_store is not None:
            user_sessions = session_store.list_sessions(session_type="user")
            if user_sessions:
                session_key = user_sessions[0]["session_key"]

        logger.info("Cron job fired | id=%s session=%s prompt=%s", job.id, session_key, job.prompt[:60])

        chunks: list[str] = []
        try:
            async with self._agent.run_stream(job.prompt, session_key=session_key, unattended=True) as result:
                async for chunk in result.stream_text(delta=True):
                    chunks.append(chunk)
        except Exception as e:
            logger.error("Cron agent turn failed | id=%s error=%s", job.id, e)
            return

        reply = "".join(chunks).strip()
        await self._agent.alerts.put({"kind": "cron", "prompt": job.prompt, "reply": reply})

    async def run(self) -> None:
        logger.info("CronService started")
        while True:
            now = datetime.now().astimezone()
            for job in [j for j in self._store.load() if j.active]:
                if job.next_run_at > now:
                    continue

                await self._fire(job)
                job.last_run_at = now

                if job.kind == "at":
                    job.active = False
                else:
                    from selmakit.schedule import parse_interval_seconds
                    secs = parse_interval_seconds(job.every or "")
                    if secs > 0:
                        job.next_run_at = now + timedelta(seconds=secs)
                    else:
                        job.active = False

                self._store.update(job)

            active = [j for j in self._store.load() if j.active]
            if active:
                next_due = min(j.next_run_at for j in active)
                sleep_s = min(30.0, max(1.0, (next_due - datetime.now().astimezone()).total_seconds()))
            else:
                sleep_s = 30.0

            await asyncio.sleep(sleep_s)


# ── Capability ─────────────────────────────────────────────────


def _format_jobs(jobs: list[CronJob]) -> str:
    active = [j for j in jobs if j.active]
    if not active:
        return "No active cron jobs."
    lines = []
    for j in active:
        schedule = f"at {j.at.isoformat()}" if j.kind == "at" else f"every {j.every}"
        next_run = j.next_run_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"• `{j.id}` — {schedule} — next: {next_run}\n  {j.prompt}")
    return "\n".join(lines)


@dataclass
class CronCapability(AbstractCapability[Any]):
    """Provides the `cron` tool so the agent can manage scheduled tasks.

    The agent can add one-shot reminders or recurring jobs, list them,
    and remove them by id. Fired jobs are delivered via the alerts queue.
    """

    store: CronStore

    def get_toolset(self) -> AgentToolset[Any] | None:
        store = self.store

        async def cron(
            action: Literal["add", "list", "remove"],
            prompt: str = "",
            at: str = "",
            every: str = "",
            job_id: str = "",
        ) -> str:
            """Manage scheduled reminders and recurring tasks.

            Actions:
              add    — create a job (requires prompt + at OR every)
              list   — show all active jobs
              remove — delete a job (requires job_id)

            Time formats for 'at':
              "15:30" or "15:30 Uhr"    — today at that time (tomorrow if past)
              "heute 16:00"             — today at 16:00
              "tomorrow 09:00"          — next day at 09:00
              "morgen 09:00"            — same, German
              "in 2h" / "in 30m"        — relative offset
              "2026-05-24T09:00"        — absolute ISO datetime

            Interval formats for 'every':
              "30m", "2h", "1d"
            """
            if action == "list":
                return _format_jobs(store.load())

            if action == "remove":
                if not job_id:
                    return "Error: job_id is required for remove."
                ok = store.remove(job_id)
                return f"Removed job `{job_id}`." if ok else f"Job `{job_id}` not found."

            if action == "add":
                if not prompt:
                    return "Error: prompt is required for add."
                now = datetime.now().astimezone()

                if at:
                    dt = parse_at(at)
                    if dt is None:
                        return f"Error: could not parse 'at' value: {at!r}"
                    if dt <= now:
                        return f"Error: scheduled time {dt.isoformat()} is in the past."
                    job = CronJob(
                        id=uuid.uuid4().hex[:8],
                        prompt=prompt,
                        kind="at",
                        at=dt,
                        next_run_at=dt,
                        created_at=now,
                    )
                    store.add(job)
                    return (
                        f"Scheduled: `{job.id}` — fires at {dt.strftime('%Y-%m-%d %H:%M')}\n"
                        f"Message: {prompt}"
                    )

                if every:
                    from selmakit.schedule import parse_interval_seconds
                    secs = parse_interval_seconds(every)
                    if secs == 0:
                        return f"Error: could not parse 'every' value: {every!r}"
                    next_run = now + timedelta(seconds=secs)
                    job = CronJob(
                        id=uuid.uuid4().hex[:8],
                        prompt=prompt,
                        kind="every",
                        every=every,
                        next_run_at=next_run,
                        created_at=now,
                    )
                    store.add(job)
                    return (
                        f"Scheduled: `{job.id}` — repeats every {every}, "
                        f"first run at {next_run.strftime('%Y-%m-%d %H:%M')}\n"
                        f"Message: {prompt}"
                    )

                return "Error: provide 'at' (one-shot) or 'every' (recurring)."

            return f"Error: unknown action {action!r}."

        return FunctionToolset([cron])

    def get_instructions(self):
        return (
            "## Cron Jobs\n"
            "Use the `cron` tool to schedule reminders and recurring tasks:\n"
            "- One-shot at a time: `cron(action='add', prompt='...', at='15:30')` "
            "or `at='heute 16:00'` / `at='tomorrow 09:00'` / `at='in 2h'` / `at='2026-05-24T09:00'`\n"
            "- Recurring: `cron(action='add', prompt='...', every='1h')` — supports s/m/h/d\n"
            "- List: `cron(action='list')`\n"
            "- Remove: `cron(action='remove', job_id='...')`\n"
            "The prompt is the message delivered to the user when the job fires."
        )
