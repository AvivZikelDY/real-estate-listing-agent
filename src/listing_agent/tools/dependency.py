"""Dependency function building the active `ToolRegistry`.

Selection is driven entirely by `Settings.tools_mode` (env var
`TOOLS_MODE`):
- "mock" -> Mock* implementations (deterministic, offline)
- "live" -> Live* implementations (real, live GovMap + Overpass HTTP calls)

Any other value fails clearly and immediately.
"""

from src.listing_agent.config import Settings, get_settings
from src.listing_agent.tools.live_implementations import (
    LiveGeoResolver,
    LiveNearbyServices,
    LivePriceSignal,
)
from src.listing_agent.tools.mock_implementations import (
    MockGeoResolver,
    MockNearbyServices,
    MockPriceSignal,
)
from src.listing_agent.tools.registry import ToolRegistry

SUPPORTED_TOOLS_MODES = ("mock", "live")


def get_tool_registry(settings: Settings | None = None) -> ToolRegistry:
    """Return a `ToolRegistry` populated per `settings.tools_mode`."""
    settings = settings or get_settings()
    mode = settings.tools_mode.strip().lower()

    registry = ToolRegistry()

    if mode == "mock":
        registry.register(MockGeoResolver())
        registry.register(MockNearbyServices())
        registry.register(MockPriceSignal())
        return registry

    if mode == "live":
        registry.register(LiveGeoResolver())
        registry.register(LiveNearbyServices())
        registry.register(LivePriceSignal())
        return registry

    raise ValueError(
        f"Unsupported TOOLS_MODE: '{settings.tools_mode}'. "
        f"Supported values are: {', '.join(SUPPORTED_TOOLS_MODES)}."
    )
