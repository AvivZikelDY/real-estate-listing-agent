import pytest

from src.listing_agent.tools.mock_implementations import MockNearbyServices
from src.listing_agent.tools.schemas import NearbyServicesInput

# Coordinates of the known mock address, HaYarkon 120, Tel Aviv.
_KNOWN_LAT, _KNOWN_LON = 32.0853, 34.7738


@pytest.mark.asyncio
async def test_services_found_near_known_location() -> None:
    tool = MockNearbyServices()

    result = await tool.execute(
        NearbyServicesInput(lat=_KNOWN_LAT, lon=_KNOWN_LON, radius_m=500)
    )

    assert result.status == "ok"
    assert len(result.data.categories) > 0
    assert all(cat.count > 0 for cat in result.data.categories)
    assert result.data.evidence_quality in {"low", "medium", "high"}
    assert len(result.data.places) > 0
    assert all(place.distance_m >= 0 for place in result.data.places)
    assert all(place.source for place in result.data.places)


@pytest.mark.asyncio
async def test_services_result_discloses_straight_line_distance_limitation() -> None:
    tool = MockNearbyServices()

    result = await tool.execute(
        NearbyServicesInput(lat=_KNOWN_LAT, lon=_KNOWN_LON, radius_m=500)
    )

    limitations_text = " ".join(result.data.limitations).lower()
    assert "straight-line" in limitations_text
    assert "walking" in limitations_text


@pytest.mark.asyncio
async def test_no_services_found_far_from_known_locations() -> None:
    tool = MockNearbyServices()

    # Middle of the ocean, far from every hardcoded service.
    result = await tool.execute(NearbyServicesInput(lat=0.0, lon=0.0, radius_m=500))

    assert result.status == "ok"
    assert result.data.categories == []
    assert result.data.evidence_quality == "low"
    assert result.data.places == []


# Coordinates of the interactive agent demo address, Derech HaTayasim 8,
# Ramat Aviv (docs/local_agent_demo.md).
_DEMO_LAT, _DEMO_LON = 32.1128, 34.8042


@pytest.mark.asyncio
async def test_demo_location_park_only_returns_park_category() -> None:
    """Requesting only 'park' near the demo address never introduces other
    categories (school/daycare/public_transport also exist nearby)."""
    tool = MockNearbyServices()

    result = await tool.execute(
        NearbyServicesInput(lat=_DEMO_LAT, lon=_DEMO_LON, categories=["park"], radius_m=500)
    )

    assert result.status == "ok"
    categories = {cat.category for cat in result.data.categories}
    assert categories == {"park"}
    assert all(place.category == "park" for place in result.data.places)
    assert result.data.categories[0].nearest_distance_m <= 500


@pytest.mark.asyncio
async def test_demo_location_school_only_returns_school_category() -> None:
    tool = MockNearbyServices()

    result = await tool.execute(
        NearbyServicesInput(lat=_DEMO_LAT, lon=_DEMO_LON, categories=["school"], radius_m=500)
    )

    assert result.status == "ok"
    categories = {cat.category for cat in result.data.categories}
    assert categories == {"school"}


@pytest.mark.asyncio
async def test_demo_location_park_and_school_returns_both_and_only_those() -> None:
    tool = MockNearbyServices()

    result = await tool.execute(
        NearbyServicesInput(
            lat=_DEMO_LAT, lon=_DEMO_LON, categories=["park", "school"], radius_m=500
        )
    )

    assert result.status == "ok"
    categories = {cat.category for cat in result.data.categories}
    assert categories == {"park", "school"}
