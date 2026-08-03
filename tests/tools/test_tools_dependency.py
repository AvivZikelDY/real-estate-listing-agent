import pytest

from src.listing_agent.config import Settings
from src.listing_agent.tools.dependency import get_tool_registry
from src.listing_agent.tools.mock_implementations import MockGeoResolver
from src.listing_agent.tools.nir_implementations import NirGeoResolver


def test_mock_mode_registers_mock_implementations() -> None:
    registry = get_tool_registry(Settings(tools_mode="mock"))

    assert isinstance(registry.get("geo_resolver"), MockGeoResolver)


def test_nir_mode_registers_nir_implementations_and_never_in_mock_mode() -> None:
    mock_registry = get_tool_registry(Settings(tools_mode="mock"))
    nir_registry = get_tool_registry(Settings(tools_mode="nir"))

    assert not isinstance(mock_registry.get("geo_resolver"), NirGeoResolver)
    assert isinstance(nir_registry.get("geo_resolver"), NirGeoResolver)


def test_unsupported_tools_mode_fails_clearly() -> None:
    with pytest.raises(ValueError, match="Unsupported TOOLS_MODE"):
        get_tool_registry(Settings(tools_mode="some-unknown-mode"))
