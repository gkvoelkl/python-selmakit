from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DashboardConfig:
    """Branding and wiring for the Streamlit dashboard.

    Pass the fields you care about to ``selmakit.dashboard.run(...)``; the rest
    fall back to these defaults.
    """

    title: str = "👩🏻 SelmaKit Agent"
    image: str | None = None                          # path to a logo/avatar shown in the sidebar
    input_placeholder: str = "How can I help you today?"   # chat input prompt
    gateway_base_url: str = "http://localhost:8000"   # SSE stream + heartbeat poll are derived from this
    page_icon: str | None = None
    user_name: str = "Admin"
    show_settings: bool = True                         # show the selmakit.json editor in the sidebar
    config_file: str = ".selmakit/selmakit.json"       # file edited by the settings dialog
    stream_timeout: float | None = 120.0               # httpx read timeout (s) for the SSE stream; None disables it for long-running QGIS/STAC turns

    @property
    def stream_url(self) -> str:
        return f"{self.gateway_base_url.rstrip('/')}/webchat/stream"

    @property
    def heartbeat_poll_url(self) -> str:
        return f"{self.gateway_base_url.rstrip('/')}/webchat/heartbeat/poll"
