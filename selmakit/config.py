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
