import pytest

from src.listing_agent.config import Settings
from src.listing_agent.tools.dependency import get_tool_registry
from src.listing_agent.tools.live_implementations import LiveGeoResolver
from src.listing_agent.tools.mock_implementations import MockGeoResolver


def test_mock_mode_registers_mock_implementations() -> None:
    registry = get_tool_registry(Settings(tools_mode="mock"))

    assert isinstance(registry.get("geo_resolver"), MockGeoResolver)


def test_live_mode_registers_live_implementations_and_never_in_mock_mode() -> None:
    mock_registry = get_tool_registry(Settings(tools_mode="mock"))
    live_registry = get_tool_registry(Settings(tools_mode="live"))

    assert not isinstance(mock_registry.get("geo_resolver"), LiveGeoResolver)
    assert isinstance(live_registry.get("geo_resolver"), LiveGeoResolver)


def test_unsupported_tools_mode_fails_clearly() -> None:
    with pytest.raises(ValueError, match="Unsupported TOOLS_MODE"):
        get_tool_registry(Settings(tools_mode="some-unknown-mode"))
