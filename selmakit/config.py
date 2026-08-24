import json
import time
from pathlib import Path
from typing import Dict, Tuple

from pydantic import BaseModel

CACHE_VALIDITY_SECONDS = 120
_config_cache: Dict[str, Tuple["SelmaKitConfig", float]] = {}


class ModelConfig(BaseModel):
    model: str = "ollama/llama3.2"
    base_url: str = "http://localhost:11434/v1"
    ollama_base_url: str | None = None  # alias — overrides base_url when set
    api_key: str | None = None  # key for hosted providers; falls back to the provider's env var
    timeout_seconds: int = 300
    thinking: str | None = None  # default thinking level for new sessions (off/low/medium/high)

    @property
    def effective_base_url(self) -> str:
        return self.ollama_base_url or self.base_url


class WebChatConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"


class TelegramConfig(BaseModel):
    enabled: bool = False  # opt-in; also requires TELEGRAM_TOKEN in the environment
    # Chat ids allowed to talk to the bot; every other chat is ignored. Empty
    # means "accept everyone" — that is what the channel did before this field
    # existed, and tightening it here would lock every running deployment out of
    # its own bot on upgrade. TelegramChannel warns at start while it is empty.
    allowed_chat_ids: list[int] = []
    # Upload the files an answer names (a rendered map, a PNG) instead of only
    # printing their paths — which over Telegram the user cannot open. Off by
    # default: the answer text is model output, so the channel will only ever
    # send files inside the state dir, and even that is a decision to make
    # deliberately. See selmakit/attachments.py.
    attach_files: bool = False
    # Post the names of the tools a turn calls, so a multi-minute run is visibly
    # working instead of silent. One message per turn, edited in place — the Bot
    # API allows a bot roughly one message per second per chat, and a local model
    # calls tools far faster than that. See selmakit/channels/telegram.py.
    show_tools: bool = True


class ChannelsConfig(BaseModel):
    webchat: WebChatConfig = WebChatConfig()
    telegram: TelegramConfig = TelegramConfig()


class SessionResetConfig(BaseModel):
    at_hour: int = 4
    idle_minutes: int | None = None


class SessionConfig(BaseModel):
    reset: SessionResetConfig = SessionResetConfig()


class MemoryConfig(BaseModel):
    enabled: bool = True
    vector_search: bool = False
    embed_model: str = "nomic-embed-text"
    temporal_decay: bool = False
    temporal_decay_rate: float = 0.01


class HeartbeatConfig(BaseModel):
    enabled: bool = False
    every: str = "30m"
    active_hours: tuple[str, str] | None = None
    timezone: str = "UTC"
    target: str = "last"
    isolated_session: bool = False


class McpServerConfig(BaseModel):
    """One MCP server, in the standard ``mcpServers`` shape (same fields Claude
    Desktop / Claude Code use) plus a few selmakit extras.

    Provide either the stdio fields (``command``/``args``/``env``/``cwd`` — a
    local subprocess) or the HTTP fields (``url``/``headers`` — a remote server).
    ``env`` and ``headers`` values are expanded with ``os.path.expandvars`` so
    ``${VAR}`` pulls secrets from the environment instead of the JSON.
    """
    # stdio transport (local subprocess)
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None
    # http transport (remote server)
    url: str | None = None
    headers: dict[str, str] = {}
    # selmakit extras
    enabled: bool = True
    prefix: str | None = None            # namespace this server's tool names
    allow_tools: list[str] | None = None  # whitelist by (unprefixed) tool name; None = all
    require_approval: bool = False       # gate this server's tool calls behind human approval


class McpConfig(BaseModel):
    enabled: bool = False
    servers: dict[str, McpServerConfig] = {}


class TracingConfig(BaseModel):
    """OpenTelemetry export, off unless you point it at a collector.

    Any OTLP/HTTP collector works; ``endpoint`` defaults to the conventional
    OTLP/HTTP port. Disabled by default so a gateway with no collector running
    does not spend every turn retrying a refused connection.
    """
    enabled: bool = False
    endpoint: str = "http://localhost:4318/v1/traces"
    project_name: str = "selmakit"   # exported as the OTel service name
    capture_http: bool = True        # also record raw provider request/response bodies


class SubAgentModelConfig(BaseModel):
    """One entry on the sub-agent model menu — a model an individual delegation
    can be routed to, keyed in ``SubAgentsConfig.models`` by the name the parent
    picks it by. Name the keys for the job (``"fast"``, ``"deep"``) rather than
    for the vendor: the key and its ``description`` are what the parent routes on.
    """
    model: str                    # "provider/model", same syntax as the main model
    description: str = ""         # routing hint, listed in the prompt next to the key
    thinking: str | None = None   # reasoning effort for this option (off/low/medium/high)


class SubAgentConfig(BaseModel):
    """One delegatable sub-agent. The parent calls it by ``name`` via the
    ``delegate_task`` tool; each run is isolated (never sees the parent chat)."""
    name: str
    description: str                      # shown to the parent so it knows when to delegate
    system_prompt: str = ""
    model: str | None = None             # "provider/model"; defaults to the main model
    timeout_seconds: float | None = None  # wall-clock budget per delegation
    max_calls: int | None = None         # tool-call budget per delegation
    models: list[str] | None = None      # menu keys this delegate accepts; None = all


class SubAgentsConfig(BaseModel):
    enabled: bool = False
    agents: list[SubAgentConfig] = []
    models: dict[str, SubAgentModelConfig] = {}  # routing menu; empty = no per-delegation choice


class SelmaKitConfig(BaseModel):
    model: ModelConfig = ModelConfig()
    memory: MemoryConfig = MemoryConfig()
    channels: ChannelsConfig = ChannelsConfig()
    session: SessionConfig = SessionConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    mcp: McpConfig = McpConfig()
    subagents: SubAgentsConfig = SubAgentsConfig()
    tracing: TracingConfig = TracingConfig()


def build_model(cfg: ModelConfig):
    """Build a pydantic-ai model from the ``provider/model`` string in ``cfg.model``.

    Dispatches on the provider prefix so selmakit can drive multiple backends
    from the same config knob:

      - ``ollama/…``               → OpenAI-compatible endpoint at ``effective_base_url``
                                     (default; local, verified tool-caller)
      - ``openai/…``               → OpenAI API (key from ``OPENAI_API_KEY``,
                                     endpoint override via ``OPENAI_BASE_URL``)
      - ``anthropic/…``            → Anthropic API (key from ``ANTHROPIC_API_KEY``)
      - ``google/…`` / ``gemini/…``→ Gemini API (key from ``GEMINI_API_KEY`` /
                                     ``GOOGLE_API_KEY``)

    A bare model string with no ``provider/`` prefix defaults to ``ollama``.
    Only the ``ollama`` branch uses ``cfg.base_url`` — the hosted providers read
    their endpoint from the environment. For credentials they prefer
    ``cfg.api_key`` (set via the dashboard's model selector) and otherwise fall
    back to the provider's env var (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` /
    ``GEMINI_API_KEY``).
    """
    provider, _, model_name = cfg.model.partition("/")
    if not model_name:  # no slash → whole string is the model name, provider defaults to ollama
        provider, model_name = "ollama", provider
    provider = provider.lower()

    if provider == "ollama":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.ollama import OllamaProvider
        return OpenAIChatModel(model_name, provider=OllamaProvider(base_url=cfg.effective_base_url))

    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        provider_obj = OpenAIProvider(api_key=cfg.api_key) if cfg.api_key else OpenAIProvider()
        return OpenAIChatModel(model_name, provider=provider_obj)

    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        if cfg.api_key:
            from pydantic_ai.providers.anthropic import AnthropicProvider
            return AnthropicModel(model_name, provider=AnthropicProvider(api_key=cfg.api_key))
        return AnthropicModel(model_name)

    if provider in ("google", "gemini", "google-gla"):
        from pydantic_ai.models.google import GoogleModel
        if cfg.api_key:
            from pydantic_ai.providers.google import GoogleProvider
            return GoogleModel(model_name, provider=GoogleProvider(api_key=cfg.api_key))
        return GoogleModel(model_name)

    raise ValueError(
        f"Unknown model provider {provider!r} in {cfg.model!r}. "
        "Use one of: ollama, openai, anthropic, google/gemini."
    )


def load_config(state_dir: str = ".selmakit", config_name: str = "selmakit.json") -> SelmaKitConfig:
    config_path = Path(state_dir) / config_name
    cache_key = str(config_path.resolve())

    now = time.monotonic()
    if cache_key in _config_cache:
        cached, ts = _config_cache[cache_key]
        if now - ts < CACHE_VALIDITY_SECONDS:
            return cached

    if not config_path.exists():
        config = SelmaKitConfig()
    else:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        config = SelmaKitConfig(**data)

    _config_cache[cache_key] = (config, now)
    return config


def setup(state_dir: str = ".selmakit") -> None:
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    config_file = path / "selmakit.json"
    if not config_file.exists():
        config_file.write_text(
            json.dumps(SelmaKitConfig().model_dump(), indent=4),
            encoding="utf-8",
        )
