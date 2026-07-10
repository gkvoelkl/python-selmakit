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


class SelmaKitConfig(BaseModel):
    model: ModelConfig = ModelConfig()
    memory: MemoryConfig = MemoryConfig()
    channels: ChannelsConfig = ChannelsConfig()
    session: SessionConfig = SessionConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()


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
