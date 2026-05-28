import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from selmakit import (
    Agent,
    BootstrapCapability,
    CronCapability,
    CronService,
    CronStore,
    FilesystemCapability,
    JsonlStore,
    RuntimeInfoCapability,
    SessionThinkingCapability,
    SkillsPromptCapability,
    SqliteMemory,
    WorkspacePromptCapability,
)
from selmakit.commands import make_commands
from selmakit.schedule import ScheduleConfig
from selmakit.channels.telegram import TelegramChannel
from selmakit.channels.webchat import WebChatChannel
from selmakit.config import load_config
from selmakit.message import QueueItem
from selmakit.tracing import setup as tracing_setup


# ── Config ────────────────────────────────────────────────────

config = load_config()

_cfg = config.model
_model_str = _cfg.model
_, _model_name = _model_str.split("/", 1) if "/" in _model_str else ("ollama", _model_str)

model = OpenAIChatModel(_model_name, provider=OpenAIProvider(base_url=_cfg.effective_base_url))

_state_dir = ".selmakit"
_workspace_dir = f"{_state_dir}/workspace"

session_store = JsonlStore(
    path=f"{_state_dir}/sessions",
    at_hour=config.session.reset.at_hour,
    idle_minutes=config.session.reset.idle_minutes,
)

memory = None
if config.memory.enabled:
    memory = SqliteMemory(
        workspace_dir=_workspace_dir,
        vector_search=config.memory.vector_search,
        embed_model=config.memory.embed_model,
        embed_base_url=_cfg.base_url,
        temporal_decay=config.memory.temporal_decay,
        temporal_decay_rate=config.memory.temporal_decay_rate,
    )


# ── Cron ──────────────────────────────────────────────────────

cron_store = CronStore(path=f"{_state_dir}/cron/jobs.json")
cron_service: CronService  # wired after agent is constructed


# ── Agent ─────────────────────────────────────────────────────

_hb = config.heartbeat

agent = Agent(
    model=model,
    state_dir=_state_dir,
    session_store=session_store,
    memory=memory,
    capabilities=[
        FilesystemCapability(cwd="."),
        WebSearch(local="duckduckgo"),
        WebFetch(local=True),
        BootstrapCapability(workspace_dir=_workspace_dir),
        WorkspacePromptCapability(workspace_dir=_workspace_dir),
        SkillsPromptCapability(workspace_dir=_workspace_dir),
        RuntimeInfoCapability(model_name=_cfg.model),
        SessionThinkingCapability(session_store=session_store, default_thinking=_cfg.thinking),
        CronCapability(store=cron_store),
    ],
    commands=make_commands(config, cron_store=cron_store),
    heartbeat=ScheduleConfig(
        every=_hb.every,
        active_hours=_hb.active_hours,
        timezone=_hb.timezone,
        target=_hb.target,
        isolated_session=_hb.isolated_session,
    ) if _hb.enabled else None,
)

cron_service = CronService(store=cron_store, agent=agent)


# ── Queue & Worker ────────────────────────────────────────────

queue: asyncio.Queue[QueueItem] = asyncio.Queue()

async def worker() -> None:
    from pydantic_ai.messages import (
        FunctionToolCallEvent, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta,
    )
    while True:
        item: QueueItem = await queue.get()
        try:
            async with agent.run_stream_events(item.prompt, session_key=item.session_key) as (is_cmd, value):
                if is_cmd:
                    await item.reply.send_chunk(value)
                else:
                    async for event in value:
                        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            # pydantic-ai 2.0 may deliver the first token(s) here
                            if event.part.content:
                                await item.reply.send_chunk(event.part.content)
                        elif isinstance(event, FunctionToolCallEvent):
                            await item.reply.send_tool(event.part.tool_name)
                        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                            if event.delta.content_delta:
                                await item.reply.send_chunk(event.delta.content_delta)
            await item.reply.done()
        except Exception as e:
            await item.reply.send_error(e)
        finally:
            queue.task_done()


# ── Channels ─────────────────────────────────────────────────

webchat = WebChatChannel(
    queue=queue,
    alerts=agent.alerts,
    host=config.webchat.host,
    port=config.webchat.port,
    timeout_seconds=config.model.timeout_seconds,
)
telegram = TelegramChannel(token=os.environ["TELEGRAM_TOKEN"], queue=queue)


# ── Main ─────────────────────────────────────────────────────

async def run_gateway() -> None:
    tracing_setup(endpoint="http://localhost:4317")
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    await asyncio.gather(
        webchat.start(),
        telegram.start(),
        worker(),
        agent.run_schedules(),
        cron_service.run(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run_gateway())
    except KeyboardInterrupt:
        print("\nGateway shutting down...")
