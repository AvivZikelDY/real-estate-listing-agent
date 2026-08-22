"""Tests for `LiveNearbyServices` — the OpenStreetMap-backed implementation.

Covers requirements 4, 5, 10 and 11 of `docs/tools.md`.

`nearby_services` is live-only: every test injects a stub `OverpassClient`,
so nothing here touches the network, and there is no offline snapshot to
fall back to anymore. A stub that raises means a genuine technical failure
(`status="error"`).
"""

import pytest

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.live_implementations.nearby_services import LiveNearbyServices
from src.listing_agent.tools.live_implementations.overpass_client import OsmPlace
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import NearbyServicesInput

# Rothschild 30, Tel Aviv — the reference point used throughout.
_LAT = 32.0632
_LON = 34.7726


class _DeadOverpass:
    """An Overpass client that always fails to reach the network."""

    async def __aenter__(self) -> "_DeadOverpass":
        raise ConnectionError("overpass is unreachable (test stub)")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _LiveOverpass:
    """An Overpass client returning canned places, with no network access."""

    places: list[OsmPlace] = []

    async def __aenter__(self) -> "_LiveOverpass":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def find_places(
        self, lat: float, lon: float, radius_m: int, categories: list[str]
    ) -> list[OsmPlace]:
        type(self).last_call = (lat, lon, radius_m, tuple(categories))
        return self.places


def _live(places: list[OsmPlace]) -> type[_LiveOverpass]:
    """Build a fresh live-stub class so tests never share state."""
    return type("_Live", (_LiveOverpass,), {"places": places, "last_call": None})


def _place(name: str, category: str, lat: float, lon: float) -> OsmPlace:
    return OsmPlace(name=name, category=category, lat=lat, lon=lon)


# Roughly 100m north, 300m north, and 1.5km north of the reference point.
# 0.0009 degrees of latitude is very close to 100m.
_NEAR = _LAT + 0.0009
_MID = _LAT + 0.0027
_FAR = _LAT + 0.0135


# --- Requirement 4: a location with nearby services ------------------------


@pytest.mark.asyncio
async def test_location_with_services_returns_places_and_categories() -> None:
    places = [
        _place("סופר", "supermarket", _NEAR, _LON),
        _place("בית ספר", "school", _MID, _LON),
        _place("גן", "park", _MID, _LON),
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.status == "ok"
    assert result.data is not None
    assert len(result.data.places) == 3
    assert len(result.data.categories) == 3
    # ~100m away, so the haversine maths is pinned, not just "positive".
    nearest = result.data.places[0]
    assert nearest.name == "סופר"
    assert nearest.distance_m == pytest.approx(100, abs=15)
    assert result.error is None


@pytest.mark.asyncio
async def test_straight_line_limitation_is_always_present() -> None:
    """Mandatory on every result (docs/tools.md:143)."""
    tool = LiveNearbyServices(
        client_factory=_live([_place("סופר", "supermarket", _NEAR, _LON)])
    )

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.data is not None
    text = " ".join(result.data.limitations).lower()
    assert "straight-line" in text
    assert "walking" in text


@pytest.mark.asyncio
async def test_places_are_sorted_nearest_first() -> None:
    places = [
        _place("far", "supermarket", _MID, _LON),
        _place("near", "supermarket", _NEAR, _LON),
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert [p.name for p in result.data.places] == ["near", "far"]


@pytest.mark.asyncio
async def test_category_summary_counts_and_nearest_distance() -> None:
    places = [
        _place("סופר א", "supermarket", _NEAR, _LON),
        _place("סופר ב", "supermarket", _MID, _LON),
        _place("בית ספר", "school", _MID, _LON),
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    summary = {c.category: c for c in result.data.categories}
    assert summary["supermarket"].count == 2
    assert summary["supermarket"].nearest_distance_m == pytest.approx(100, abs=15)
    assert summary["school"].count == 1


@pytest.mark.asyncio
async def test_radius_is_respected() -> None:
    """A place beyond the radius must be excluded entirely.

    Overpass's own `around:` selector narrows by radius server-side, but
    the tool also re-checks locally (a way/relation's computed centre can
    sit just outside the true radius) — this pins that local check.
    """
    places = [
        _place("inside", "supermarket", _NEAR, _LON),
        _place("outside", "supermarket", _FAR, _LON),  # ~1.5km away
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert [p.name for p in result.data.places] == ["inside"]


@pytest.mark.asyncio
async def test_only_requested_categories_are_returned() -> None:
    places = [
        _place("סופר", "supermarket", _NEAR, _LON),
        _place("בית ספר", "school", _NEAR, _LON),
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(
        NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500, categories=["school"])
    )

    assert result.data is not None
    assert [p.category for p in result.data.places] == ["school"]


@pytest.mark.asyncio
async def test_requested_categories_and_point_are_passed_to_the_live_client() -> None:
    """The live query is scoped to what was actually asked for."""
    stub = _live([_place("בית ספר", "school", _NEAR, _LON)])
    tool = LiveNearbyServices(client_factory=stub)

    await tool.execute(
        NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=750, categories=["school", "park"])
    )

    lat, lon, radius_m, categories = stub.last_call
    assert (lat, lon, radius_m) == (_LAT, _LON, 750)
    assert set(categories) == {"school", "park"}


# --- Requirement 5: a location with no nearby services ---------------------


@pytest.mark.asyncio
async def test_location_with_no_services_is_ok_and_low_quality() -> None:
    """Empty is a successful result, not an error."""
    tool = LiveNearbyServices(
        client_factory=_live([_place("far", "supermarket", _FAR, _LON)])
    )

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=200))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.places == []
    assert result.data.categories == []
    assert result.data.evidence_quality == "low"
    assert result.error is None
    # Still discloses the straight-line caveat even with nothing to report.
    assert any("straight-line" in limitation.lower() for limitation in result.data.limitations)


# --- Evidence quality ------------------------------------------------------


@pytest.mark.asyncio
async def test_many_places_across_many_categories_is_high_quality() -> None:
    places = [
        _place(f"p{i}", category, _NEAR, _LON)
        for category in ("supermarket", "school", "park", "pharmacy")
        for i in range(4)
    ]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert result.data.evidence_quality == "high"


@pytest.mark.asyncio
async def test_many_places_in_one_category_is_not_high_quality() -> None:
    """20 bus stops is a well-mapped bus route, not a well-served area."""
    places = [_place(f"stop {i}", "public_transport", _NEAR, _LON) for i in range(20)]
    tool = LiveNearbyServices(client_factory=_live(places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert result.data.evidence_quality == "medium"


# --- Requirement 10: internal failures never escape ------------------------


@pytest.mark.asyncio
async def test_unreachable_overpass_returns_error_not_exception() -> None:
    """>>> No fallback left. <<< A technical failure is a genuine error."""
    tool = LiveNearbyServices(client_factory=_DeadOverpass)

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "error"
    assert result.data is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_unexpected_exception_from_client_returns_error_not_exception() -> None:
    """Any client failure, not just connection errors, becomes `status="error"`."""

    class _Exploding(_LiveOverpass):
        async def find_places(
            self, lat: float, lon: float, radius_m: int, categories: list[str]
        ) -> list[OsmPlace]:
            raise RuntimeError("boom")

    tool = LiveNearbyServices(client_factory=_Exploding)

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "error"
    assert result.error is not None


# --- Requirement 11: interface conformance ---------------------------------


def test_tool_name_matches_the_whitelisted_name() -> None:
    assert LiveNearbyServices().name == "nearby_services"


def test_tool_satisfies_the_base_tool_protocol() -> None:
    assert isinstance(LiveNearbyServices(), BaseTool)


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry() -> None:
    registry = ToolRegistry()
    registry.register(
        LiveNearbyServices(
            client_factory=_live([_place("סופר", "supermarket", _NEAR, _LON)])
        )
    )

    result = await registry.execute("nearby_services", NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "ok"
    assert result.data is not None
    assert len(result.data.places) == 1
