"""Coordinate conversion between govmap's projection and WGS84.

WHY THIS FILE EXISTS
--------------------
Our frozen contract in `src/listing_agent/tools/schemas.py` is WGS84
degrees: `GeoResolverOutput.lat` is constrained `ge=-90, le=90` and
`.lon` is `ge=-180, le=180`.

govmap does not speak WGS84. It returns coordinates as WKT strings of
projected metres, e.g.:

    "POINT(3870871.7058859584 3771605.2672468172)"

Handing that straight to the schema would fail validation instantly (3.87
million is not a valid latitude). So this module converts at the boundary,
purely to satisfy that contract. `price_signal` itself never needs WGS84 —
it passes govmap's own projected values straight back to govmap, because
that is the only thing govmap understands.

>>> WHICH PROJECTION, EXACTLY <<<
---------------------------------
This was verified against the live API rather than assumed, and the answer
is NOT what the public reference clients claim.

Israeli government geodata is usually EPSG:2039 (Israeli TM Grid / "ITM"),
and the widely-referenced open-source govmap clients label these values
"ITM coordinates". That is wrong. Decoding the point above:

    as EPSG:3857 (Web Mercator) -> 32.063168 N, 34.772632 E  <- Rothschild 30, Tel Aviv. Correct.
    as EPSG:2039 (ITM)          -> nonsense; the true ITM for that address
                                   is (178697, 663505), three orders of
                                   magnitude away from the API's values.

So govmap's real-estate endpoints use **EPSG:3857 Web Mercator**, the same
projection as web slippy maps. That is what is implemented below.

Practical consequence worth knowing: Web Mercator metres are not true
ground metres. They are inflated by a factor of 1/cos(latitude), which at
Tel Aviv's latitude (~32 N) is about 1.18. A `radius` of 300 passed to
govmap therefore covers roughly 254 real metres. See `METRE_INFLATION_AT_TEL_AVIV`.

NO NEW DEPENDENCY
-----------------
Hand-rolled rather than using `pyproj`, because `docs/nir_handoff.md:227`
requires clearing any new third-party package with Aviv first. Web Mercator
is a simple closed-form spherical projection, so this is a few lines of
maths with no accuracy concerns.
"""

import math

# Web Mercator (EPSG:3857) treats the Earth as a sphere of exactly this
# radius - the GRS80/WGS84 semi-major axis. This is the single constant
# the whole projection is built from.
_EARTH_RADIUS_M = 6_378_137.0

# Web Mercator is undefined at the poles (y tends to infinity), so the
# standard convention clamps latitude to this value, which is what makes
# the projected world a square.
_MAX_LATITUDE_DEG = 85.05112878

# Web Mercator metres are stretched by 1/cos(lat) relative to true ground
# distance. Recorded here as documentation for anyone choosing a radius:
# at Tel Aviv this is ~1.18, so a 300-unit radius is ~254 real metres.
METRE_INFLATION_AT_TEL_AVIV = 1.0 / math.cos(math.radians(32.07))


def web_mercator_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Convert EPSG:3857 metres to WGS84 degrees.

    This is the direction that matters for the contract: govmap hands us
    projected metres, and the schema needs lat/lon.

    Args:
        x: Easting in Web Mercator metres.
        y: Northing in Web Mercator metres.

    Returns:
        A `(lat, lon)` tuple in decimal degrees. Note the order is
        lat-then-lon to match the schema's field order — the reverse of the
        `(x, y)` easting-then-northing convention of the inputs.
    """
    # Longitude is the easy direction: Web Mercator's x axis is a plain
    # linear scaling of longitude.
    lon = math.degrees(x / _EARTH_RADIUS_M)
    # Latitude is the Mercator part: y is the inverse Gudermannian of the
    # latitude, so undoing it needs atan(exp(...)).
    lat = math.degrees(2.0 * math.atan(math.exp(y / _EARTH_RADIUS_M)) - math.pi / 2.0)
    return lat, lon


def wgs84_to_web_mercator(lat: float, lon: float) -> tuple[float, float]:
    """Convert WGS84 degrees to EPSG:3857 metres.

    Needed whenever we have a lat/lon (for example one that `geo_resolver`
    already resolved) and want to query govmap with it, since govmap's URL
    paths take projected coordinates.

    Args:
        lat: Latitude in decimal degrees, clamped to the Web Mercator limit.
        lon: Longitude in decimal degrees.

    Returns:
        An `(x, y)` tuple in Web Mercator metres, in the easting-then-northing
        order govmap expects in its URL paths, e.g.
        `/real-estate/deals/{x},{y}/{radius}`.
    """
    # Clamp before the tan() below, which blows up approaching the poles.
    clamped_lat = max(-_MAX_LATITUDE_DEG, min(_MAX_LATITUDE_DEG, lat))
    x = _EARTH_RADIUS_M * math.radians(lon)
    y = _EARTH_RADIUS_M * math.log(math.tan(math.pi / 4.0 + math.radians(clamped_lat) / 2.0))
    return x, y


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres.

    Used by `nearby_services` to measure how far each place is from the
    listing. Straight-line ("as the crow flies") only — it knows nothing
    about streets, one-way systems, or walking time, which is exactly why
    `nearby_services` is required to disclose that in its `limitations`.

    Haversine rather than the flat-plane approximation because it stays
    correct regardless of latitude, and it costs nothing at these volumes.

    Args:
        lat1: Latitude of the first point, in decimal degrees.
        lon1: Longitude of the first point, in decimal degrees.
        lat2: Latitude of the second point, in decimal degrees.
        lon2: Longitude of the second point, in decimal degrees.

    Returns:
        Distance in metres, always non-negative.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    # atan2 keeps this numerically stable for very small distances, where a
    # plain asin(sqrt(a)) loses precision.
    return 2.0 * _EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def parse_wkt_point(shape: str | None) -> tuple[float, float] | None:
    """Extract `(x, y)` projected metres from a WKT `POINT(x y)` string.

    govmap's autocomplete endpoint returns coordinates as a `shape` string
    like `"POINT(3870871.70 3771605.26)"` rather than as numeric fields.

    Deliberately returns `None` instead of raising on anything malformed —
    empty, wrong prefix, non-numeric, or the wrong number of components.
    Two reasons:

      * Upstream data quality is not under our control, and one bad row
        must never take down the whole tool.
      * The deal endpoints return `MULTIPOLYGON(...)` in the same `shape`
        field, so a non-POINT geometry is expected, not exceptional. The
        caller treats `None` as "no usable coordinate here" and moves on.
    """
    if not shape:
        return None
    text = shape.strip()
    # Case-insensitive because the API is not consistent about casing.
    if not text.upper().startswith("POINT(") or not text.endswith(")"):
        return None
    # Strip the "POINT(" prefix (6 chars) and the trailing ")".
    parts = text[6:-1].strip().split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        # Non-numeric content inside the parentheses.
        return None
