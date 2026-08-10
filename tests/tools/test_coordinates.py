"""Tests for govmap coordinate conversion.

The important test here is `test_known_tel_aviv_address_decodes_correctly`:
a round-trip alone would pass even with a completely wrong projection
constant, so the suite pins a real, verified point from the live API.
"""

import pytest

from src.listing_agent.tools.nir_implementations.coordinates import (
    parse_wkt_point,
    web_mercator_to_wgs84,
    wgs84_to_web_mercator,
)

# A real response from govmap's autocomplete for "רוטשילד 30 תל אביב"
# (Rothschild 30, Tel Aviv), captured from the live API.
_ROTHSCHILD_30_SHAPE = "POINT(3870871.7058859584 3771605.2672468172)"
_ROTHSCHILD_30_LAT = 32.0632
_ROTHSCHILD_30_LON = 34.7726


def test_known_tel_aviv_address_decodes_correctly() -> None:
    """Pins the projection against ground truth.

    If someone "corrects" this module to EPSG:2039 (ITM) — which the public
    reference clients wrongly claim govmap uses — this test fails loudly
    instead of the tool silently searching the wrong place on Earth.
    """
    point = parse_wkt_point(_ROTHSCHILD_30_SHAPE)
    assert point is not None

    lat, lon = web_mercator_to_wgs84(*point)

    assert lat == pytest.approx(_ROTHSCHILD_30_LAT, abs=0.001)
    assert lon == pytest.approx(_ROTHSCHILD_30_LON, abs=0.001)


def test_decoded_coordinates_satisfy_the_schema_constraints() -> None:
    """The whole reason this module exists: WGS84 bounds in `schemas.py`."""
    point = parse_wkt_point(_ROTHSCHILD_30_SHAPE)
    assert point is not None
    lat, lon = web_mercator_to_wgs84(*point)

    assert -90 <= lat <= 90
    assert -180 <= lon <= 180


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (32.0804, 34.7805),  # Tel Aviv
        (31.7767, 35.2345),  # Jerusalem
        (32.7940, 34.9896),  # Haifa
        (31.2530, 34.7915),  # Beer Sheva
        (0.0, 0.0),  # null island, guards against a divide-by-zero style bug
    ],
)
def test_round_trip_is_lossless(lat: float, lon: float) -> None:
    """WGS84 -> Web Mercator -> WGS84 must return the original point."""
    x, y = wgs84_to_web_mercator(lat, lon)
    back_lat, back_lon = web_mercator_to_wgs84(x, y)

    assert back_lat == pytest.approx(lat, abs=1e-9)
    assert back_lon == pytest.approx(lon, abs=1e-9)


def test_extreme_latitudes_are_clamped_not_infinite() -> None:
    """Web Mercator is undefined at the poles; clamping must keep it finite."""
    x, y = wgs84_to_web_mercator(90.0, 0.0)

    assert x == pytest.approx(0.0)
    # Finite rather than inf, which is what an unclamped tan() would give.
    assert y == pytest.approx(20_037_508, rel=0.01)


@pytest.mark.parametrize(
    "shape",
    [
        None,
        "",
        "   ",
        "POINT(1)",  # too few components
        "POINT(1 2 3)",  # too many
        "POINT(a b)",  # non-numeric
        "POINT(1 2",  # unterminated
        "LINESTRING(1 2)",  # wrong geometry type
        # The deal endpoints return this in the same `shape` field, so it
        # is an expected input, not an exceptional one.
        "MULTIPOLYGON(((3871146.97 3771474.95,3871154.91 3771452.61)))",
    ],
)
def test_malformed_wkt_returns_none_rather_than_raising(shape: str | None) -> None:
    assert parse_wkt_point(shape) is None


def test_wkt_parsing_is_case_insensitive() -> None:
    """The API is not consistent about casing."""
    assert parse_wkt_point("point(1.5 2.5)") == (1.5, 2.5)
    assert parse_wkt_point("POINT(1.5 2.5)") == (1.5, 2.5)
