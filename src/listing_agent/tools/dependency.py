"""Dependency function building the active `ToolRegistry`.

Selection is driven entirely by `Settings.tools_mode` (env var
`TOOLS_MODE`):
- "mock" -> Mock* implementations (deterministic, offline)
- "nir"  -> Nir*  implementations (real implementations; currently raise
            NotImplementedError until Nir replaces them)

Any other value fails clearly and immediately. Nir* implementations are
never selected while TOOLS_MODE=mock.
"""

from src.listing_agent.config import Settings, get_settings
from src.listing_agent.tools.mock_implementations import (
    MockGeoResolver,
    MockNearbyServices,
    MockPriceSignal,
)
from src.listing_agent.tools.nir_implementations import (
    NirGeoResolver,
    NirNearbyServices,
    NirPriceSignal,
)
from src.listing_agent.tools.registry import ToolRegistry

SUPPORTED_TOOLS_MODES = ("mock", "nir")


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

    if mode == "nir":
        registry.register(NirGeoResolver())
        registry.register(NirNearbyServices())
        registry.register(NirPriceSignal())
        return registry

    raise ValueError(
        f"Unsupported TOOLS_MODE: '{settings.tools_mode}'. "
        f"Supported values are: {', '.join(SUPPORTED_TOOLS_MODES)}."
    )
