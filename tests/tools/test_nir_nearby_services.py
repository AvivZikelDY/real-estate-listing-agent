"""Tests for `NirNearbyServices` — the OpenStreetMap-backed implementation.

Covers requirements 4, 5, 10 and 11 of `docs/nir_handoff.md`.

Most tests build their own tiny POI fixture on disk so distances can be
asserted exactly rather than "greater than zero". A final pair of smoke
tests runs against the real committed snapshot.
"""

import json
from pathlib import Path

import pytest

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.nir_implementations.nearby_services import (
    DEFAULT_POI_SNAPSHOT_PATH,
    NirNearbyServices,
)
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import NearbyServicesInput

# Rothschild 30, Tel Aviv — the reference point used throughout.
_LAT = 32.0632
_LON = 34.7726


def _write_snapshot(tmp_path: Path, places: list[dict], bbox: dict | None = None) -> Path:
    """Write a POI snapshot fixture and return its path."""
    path = tmp_path / "poi.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-10",
                "source": "test fixture",
                "bbox": bbox
                or {"south": 32.01, "west": 34.735, "north": 32.145, "east": 34.84},
                "places": places,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _place(name: str, category: str, lat: float, lon: float) -> dict:
    return {"name": name, "category": category, "lat": lat, "lon": lon}


# Roughly 100m north, 300m north, and 1.5km north of the reference point.
# 0.0009 degrees of latitude is very close to 100m.
_NEAR = _LAT + 0.0009
_MID = _LAT + 0.0027
_FAR = _LAT + 0.0135


# --- Requirement 4: a location with nearby services ------------------------


@pytest.mark.asyncio
async def test_location_with_services_returns_places_and_categories(tmp_path: Path) -> None:
    places = [
        _place("סופר", "supermarket", _NEAR, _LON),
        _place("בית ספר", "school", _MID, _LON),
        _place("גן", "park", _MID, _LON),
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

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
async def test_straight_line_limitation_is_always_present(tmp_path: Path) -> None:
    """Mandatory on every result (docs/nir_handoff.md:136)."""
    tool = NirNearbyServices(
        snapshot_path=_write_snapshot(tmp_path, [_place("סופר", "supermarket", _NEAR, _LON)])
    )

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.data is not None
    text = " ".join(result.data.limitations).lower()
    assert "straight-line" in text
    assert "walking" in text


@pytest.mark.asyncio
async def test_places_are_sorted_nearest_first(tmp_path: Path) -> None:
    places = [
        _place("far", "supermarket", _MID, _LON),
        _place("near", "supermarket", _NEAR, _LON),
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert [p.name for p in result.data.places] == ["near", "far"]


@pytest.mark.asyncio
async def test_category_summary_counts_and_nearest_distance(tmp_path: Path) -> None:
    places = [
        _place("סופר א", "supermarket", _NEAR, _LON),
        _place("סופר ב", "supermarket", _MID, _LON),
        _place("בית ספר", "school", _MID, _LON),
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    summary = {c.category: c for c in result.data.categories}
    assert summary["supermarket"].count == 2
    assert summary["supermarket"].nearest_distance_m == pytest.approx(100, abs=15)
    assert summary["school"].count == 1


@pytest.mark.asyncio
async def test_radius_is_respected(tmp_path: Path) -> None:
    """A place beyond the radius must be excluded entirely."""
    places = [
        _place("inside", "supermarket", _NEAR, _LON),
        _place("outside", "supermarket", _FAR, _LON),  # ~1.5km away
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert [p.name for p in result.data.places] == ["inside"]


@pytest.mark.asyncio
async def test_only_requested_categories_are_returned(tmp_path: Path) -> None:
    places = [
        _place("סופר", "supermarket", _NEAR, _LON),
        _place("בית ספר", "school", _NEAR, _LON),
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(
        NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500, categories=["school"])
    )

    assert result.data is not None
    assert [p.category for p in result.data.places] == ["school"]


# --- Requirement 5: a location with no nearby services ---------------------


@pytest.mark.asyncio
async def test_location_with_no_services_is_ok_and_low_quality(tmp_path: Path) -> None:
    """Empty is a successful result, not an error."""
    tool = NirNearbyServices(
        snapshot_path=_write_snapshot(tmp_path, [_place("far", "supermarket", _FAR, _LON)])
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


@pytest.mark.asyncio
async def test_point_outside_coverage_is_flagged_as_missing_data(tmp_path: Path) -> None:
    """"We hold no data here" must not read as "there is nothing here"."""
    tool = NirNearbyServices(
        snapshot_path=_write_snapshot(tmp_path, [_place("סופר", "supermarket", _NEAR, _LON)])
    )

    # Jerusalem: far outside the Tel Aviv bounding box.
    result = await tool.execute(NearbyServicesInput(lat=31.7767, lon=35.2345))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.places == []
    assert result.data.evidence_quality == "low"
    assert any("outside the area covered" in lim for lim in result.data.limitations)


# --- Evidence quality ------------------------------------------------------


@pytest.mark.asyncio
async def test_many_places_across_many_categories_is_high_quality(tmp_path: Path) -> None:
    places = [
        _place(f"p{i}", category, _NEAR, _LON)
        for category in ("supermarket", "school", "park", "pharmacy")
        for i in range(4)
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert result.data.evidence_quality == "high"


@pytest.mark.asyncio
async def test_many_places_in_one_category_is_not_high_quality(tmp_path: Path) -> None:
    """20 bus stops is a well-mapped bus route, not a well-served area."""
    places = [_place(f"stop {i}", "public_transport", _NEAR, _LON) for i in range(20)]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.data is not None
    assert result.data.evidence_quality == "medium"


# --- Requirement 10: internal failures never escape ------------------------


@pytest.mark.asyncio
async def test_missing_snapshot_returns_error_not_exception(tmp_path: Path) -> None:
    tool = NirNearbyServices(snapshot_path=tmp_path / "does_not_exist.json")

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "error"
    assert result.data is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_corrupt_snapshot_returns_error_not_exception(tmp_path: Path) -> None:
    path = tmp_path / "poi.json"
    path.write_text("{ not valid json ::::", encoding="utf-8")
    tool = NirNearbyServices(snapshot_path=path)

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "error"


@pytest.mark.asyncio
async def test_malformed_place_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    places = [
        _place("good", "supermarket", _NEAR, _LON),
        {"name": "bad", "category": "not_a_category", "lat": _NEAR, "lon": _LON},
        {"name": "worse", "category": "school", "lat": 999, "lon": _LON},
    ]
    tool = NirNearbyServices(snapshot_path=_write_snapshot(tmp_path, places))

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.status == "ok"
    assert result.data is not None
    assert [p.name for p in result.data.places] == ["good"]


# --- Requirement 11: interface conformance ---------------------------------


def test_tool_name_matches_the_whitelisted_name() -> None:
    assert NirNearbyServices().name == "nearby_services"


def test_tool_satisfies_the_base_tool_protocol() -> None:
    assert isinstance(NirNearbyServices(), BaseTool)


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        NirNearbyServices(
            snapshot_path=_write_snapshot(tmp_path, [_place("סופר", "supermarket", _NEAR, _LON)])
        )
    )

    result = await registry.execute("nearby_services", NearbyServicesInput(lat=_LAT, lon=_LON))

    assert result.status == "ok"
    assert result.data is not None
    assert len(result.data.places) == 1


# --- The committed snapshot itself -----------------------------------------


def test_committed_snapshot_covers_every_contract_category() -> None:
    """A category with no data would silently never appear in any result."""
    snapshot = json.loads(DEFAULT_POI_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    present = {place["category"] for place in snapshot["places"]}

    expected = {
        "supermarket",
        "school",
        "park",
        "pharmacy",
        "daycare",
        "public_transport",
        "hospital",
    }
    assert expected <= present, f"missing categories: {sorted(expected - present)}"


@pytest.mark.asyncio
async def test_committed_snapshot_finds_real_services_in_central_tel_aviv() -> None:
    """Smoke test against the real data, with no fixture."""
    tool = NirNearbyServices()

    result = await tool.execute(NearbyServicesInput(lat=_LAT, lon=_LON, radius_m=500))

    assert result.status == "ok"
    assert result.data is not None
    # Central Tel Aviv is dense; anything less would suggest a broken build.
    assert len(result.data.places) > 10
    assert result.data.evidence_quality == "high"
    assert all(p.distance_m <= 500 for p in result.data.places)
