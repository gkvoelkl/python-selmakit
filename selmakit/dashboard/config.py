from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class SidebarContext:
    """What a sidebar panel may look at.

    The two sequences are tuples, so a panel cannot append to the dashboard's
    own lists. The dicts *inside* them are the dashboard's live objects and are
    not copied — a panel must treat them as read-only by convention.
    """

    tool_activity: tuple[Mapping[str, Any], ...] = ()
    """This turn's verbose-log entries, in order — the same entries
    ``render_tool_activity`` receives: ``{"kind": "call"|"result"|"thinking"|
    "approval", "name": str, "args": str, …}``. Empty between turns."""

    messages: tuple[Mapping[str, Any], ...] = ()
    """The chat history. Each assistant message carries its own
    ``tool_activity``, so a panel can fall back to the last completed turn
    while idle. Roles include ``user``/``assistant``/``cron``/``notification``
    — a fallback has to look for the last *assistant* message, not just the
    last one."""

    streaming: bool = False
    """True while a turn is in flight."""


SidebarPanel = Callable[[SidebarContext], None]
"""A plain Streamlit render function: takes the context, draws, returns nothing.

Panels are called repeatedly — once per script run while idle, and again on
every state-bearing event of a turn — so they must be cheap and must not carry
state. Widgets with their own ``st.session_state`` keys are out of scope: they
would collide with the dashboard's own keys sooner or later.
"""


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
    sidebar_width: int = 200                           # px; the default fits branding + buttons, a panel usually needs more
    sidebar_panels: Sequence[SidebarPanel] = ()        # rendered below the built-in sidebar, in this order

    @property
    def stream_url(self) -> str:
        return f"{self.gateway_base_url.rstrip('/')}/webchat/stream"

    @property
    def heartbeat_poll_url(self) -> str:
        return f"{self.gateway_base_url.rstrip('/')}/webchat/heartbeat/poll"
