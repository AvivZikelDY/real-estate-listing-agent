import pytest

from src.listing_agent.tools.mock_implementations import MockGeoResolver
from src.listing_agent.tools.schemas import GeoResolverInput


@pytest.mark.asyncio
async def test_known_address_returns_deterministic_coordinates() -> None:
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address="HaYarkon 120, Tel Aviv"))

    assert result.status == "ok"
    assert result.error is None
    assert result.data.outcome == "resolved"
    assert result.data.lat == pytest.approx(32.0853)
    assert result.data.lon == pytest.approx(34.7738)
    assert result.data.neighborhood == "Old North (HaTzafon HaYashan)"


@pytest.mark.asyncio
async def test_known_address_lookup_is_case_and_whitespace_insensitive() -> None:
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address="  hayarkon   120,  tel aviv  "))

    assert result.status == "ok"
    assert result.data.lat == pytest.approx(32.0853)


@pytest.mark.asyncio
async def test_known_address_lookup_tolerates_trailing_punctuation() -> None:
    """A `triage_planner`-extracted `raw_address` sometimes carries a
    trailing comma/period left over from splitting a longer sentence (e.g.
    "...8, תל אביב," before the next clause). That must not block an
    otherwise-exact match against the known-address table."""
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address="HaYarkon 120, Tel Aviv,"))

    assert result.status == "ok"
    assert result.data.outcome == "resolved"
    assert result.data.lat == pytest.approx(32.0853)


@pytest.mark.asyncio
async def test_unknown_address_returns_structured_not_found_outcome() -> None:
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address="1 Nowhere St, Atlantis"))

    assert result.status == "ok"
    assert result.error is None
    assert result.data.outcome == "not_found"
    assert result.data.lat is None
    assert result.data.lon is None
    assert result.data.limitations
    assert "1 Nowhere St, Atlantis" in result.data.limitations[0]


@pytest.mark.asyncio
async def test_ambiguous_address_returns_structured_ambiguous_outcome() -> None:
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address="Dizengoff 50"))

    assert result.status == "ok"
    assert result.error is None
    assert result.data.outcome == "ambiguous"
    assert len(result.data.candidates) >= 2
    assert result.data.lat is None
    assert result.data.limitations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "דרך הטייסים 8, תל אביב",
        "Derech HaTayasim 8, Tel Aviv",
        "Derech HaTayasim 8, Tel Aviv-Yafo",
        "  דרך הטייסים 8, תל אביב-יפו  ",
    ],
)
async def test_interactive_demo_address_resolves_deterministically(address: str) -> None:
    """The GUI demo (README.md "Try the GUI") listing address
    resolves the same way regardless of small punctuation/spelling variants
    triage_planner might extract verbatim from the pasted listing text."""
    tool = MockGeoResolver()

    result = await tool.execute(GeoResolverInput(address=address))

    assert result.status == "ok"
    assert result.data.outcome == "resolved"
    assert result.data.lat == pytest.approx(32.1128)
    assert result.data.lon == pytest.approx(34.8042)
    assert result.data.neighborhood == "Ramat Aviv"
