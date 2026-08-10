"""Live async client for the Overpass API (OpenStreetMap points of interest).

WHAT THIS IS
------------
The data source behind `nearby_services`. OpenStreetMap is the practical
choice for Israeli points of interest: govmap has no usable POI endpoint
(its `layers-catalog/entitiesByPoint` returns HTTP 400 for every payload
shape the public reference clients document), while OSM has dense,
well-tagged coverage of Tel Aviv and answers in plain WGS84 lat/lon —
which is exactly what `NearbyServicesInput` already carries, so no
coordinate conversion is needed anywhere in this path.

No authentication, no API key. Overpass asks only that clients identify
themselves with a real User-Agent and do not hammer it.

>>> MIRROR FALLBACK IS NOT OPTIONAL <<<
---------------------------------------
The main instance is frequently overloaded. Measured while building this:
`overpass-api.de` returned **HTTP 504** on the very first real query,
while `overpass.kumi.systems` answered the identical query in full (82
places around Rothschild 30). So `MIRRORS` is tried in order, and a dead
mirror is a reason to move on rather than to fail.

That flakiness is also the main argument for `nearby_services` reading a
committed snapshot rather than calling this live on every request — see
`build_poi_snapshot.py`.
"""

import asyncio
import logging

import httpx
from pydantic import BaseModel, Field

from src.listing_agent.tools.schemas import NearbyServiceCategory

# NOTE: typed tool errors are imported lazily inside `_post`, NOT at module
# level. Importing from `agent.tool_executor` runs its `__init__.py`, which
# imports `tools.dependency`, which imports this package back — a circular
# import that fails at startup. Same workaround as `govmap_client.py`.

logger = logging.getLogger(__name__)

# Tried in order. The first mirror is the canonical one but is the least
# reliable in practice; the second answered when the first timed out.
MIRRORS: tuple[str, ...] = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)

# Overpass rejects requests with a default library User-Agent (HTTP 406).
# Identifying the client is also simply the polite thing to do for a free,
# donation-funded service.
USER_AGENT = "listing-agent/0.1 (Tel Aviv listing triage; university project)"

# Generous: Overpass is a query engine, not a lookup table, and a cold
# query over a city-sized area genuinely takes tens of seconds. This client
# is used by the offline snapshot builder, where that is fine.
REQUEST_TIMEOUT_S = 90.0

# Maps our seven contract categories onto OpenStreetMap tags.
#
# OSM has no single "category" field — meaning is carried by different keys
# depending on the feature (`amenity`, `shop`, `leisure`, `highway`), so
# each of our categories becomes one or more `key=value` pairs.
#
# Choices worth noting:
#   * `daycare` maps to `kindergarten`, which is what גני ילדים are tagged
#     as in Israel; `childcare` is rare here.
#   * `public_transport` deliberately includes bus stops, light-rail stops
#     and train stations, because for a listing they are interchangeable
#     as "can I get out of here without a car".
#   * `supermarket` includes `convenience` — in dense Tel Aviv a corner
#     grocery genuinely serves that role.
_OSM_TAGS: dict[NearbyServiceCategory, tuple[tuple[str, str], ...]] = {
    "supermarket": (("shop", "supermarket"), ("shop", "convenience")),
    "school": (("amenity", "school"),),
    "park": (("leisure", "park"), ("leisure", "garden")),
    "pharmacy": (("amenity", "pharmacy"),),
    "daycare": (("amenity", "kindergarten"),),
    "public_transport": (
        ("highway", "bus_stop"),
        ("railway", "tram_stop"),
        ("railway", "station"),
    ),
    "hospital": (("amenity", "hospital"), ("amenity", "clinic")),
}


class OsmPlace(BaseModel):
    """One point of interest, normalised into our own vocabulary.

    IMPORTANT: this is the shared currency between the live path and the
    offline snapshot, exactly as `ComparableDeal` is for `price_signal`.
    Both produce this type, so the distance maths downstream is identical
    either way and switching cannot silently change a result.
    """

    name: str
    category: NearbyServiceCategory
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class OverpassClient:
    """Async httpx client for Overpass, with mirror fallback.

    Use as an async context manager so the connection pool is closed:

        async with OverpassClient() as client:
            places = await client.find_places(32.06, 34.77, radius_m=500)
    """

    def __init__(
        self,
        mirrors: tuple[str, ...] = MIRRORS,
        timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self._mirrors = mirrors
        self._timeout_s = timeout_s
        # Created lazily so constructing the client is side-effect free.
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OverpassClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout_s,
            headers={"User-Agent": USER_AGENT},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- internal plumbing -------------------------------------------------

    async def _post(self, query: str) -> dict:
        """Run an Overpass QL query, trying each mirror in turn.

        Transport failures become the project's *typed* tool errors so the
        executor can decide whether a retry is worthwhile
        (`RETRYABLE_TOOL_ERROR_CODES` in the executor's `errors.py`). Only
        the failure from the LAST mirror is reported — an earlier mirror
        being down is not interesting once another one answered.
        """
        # Deliberately a local import — see the note at the top of this
        # module about the circular import this avoids.
        from src.listing_agent.agent.tool_executor.errors import (
            ToolProviderUnavailableError,
            ToolRateLimitedError,
            ToolTimeoutError,
        )

        if self._http is None:
            raise RuntimeError(
                "OverpassClient must be used as an async context manager "
                "(`async with OverpassClient() as client:`)."
            )

        last_error: Exception | None = None
        for mirror in self._mirrors:
            try:
                response = await self._http.post(mirror, data={"data": query})
            except httpx.TimeoutException:
                last_error = ToolTimeoutError(f"Overpass timed out after {self._timeout_s}s.")
                logger.warning("Overpass mirror timed out, trying next: %s", mirror)
                continue
            except httpx.HTTPError:
                last_error = ToolProviderUnavailableError("Overpass is unreachable.")
                logger.warning("Overpass mirror unreachable, trying next: %s", mirror)
                continue

            # 429 and 504 are the two ways an overloaded Overpass says "not
            # now". Both are worth trying on a different mirror.
            if response.status_code == 429:
                last_error = ToolRateLimitedError("Overpass rate-limited the request.")
                logger.warning("Overpass mirror rate-limited, trying next: %s", mirror)
                continue
            if response.status_code >= 500:
                last_error = ToolProviderUnavailableError(
                    f"Overpass returned HTTP {response.status_code}."
                )
                logger.warning(
                    "Overpass mirror returned %d, trying next: %s", response.status_code, mirror
                )
                continue

            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                last_error = ToolProviderUnavailableError("Overpass returned a non-JSON response.")
                continue

        # Every mirror failed.
        raise last_error or ToolProviderUnavailableError("No Overpass mirror answered.")

    @staticmethod
    def _build_query(
        categories: list[NearbyServiceCategory],
        *,
        around: tuple[float, float, int] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> str:
        """Compose an Overpass QL query for the requested categories.

        Exactly one of `around` (lat, lon, radius_m) or `bbox`
        (south, west, north, east) must be given: the first is the live
        per-listing query, the second is how the snapshot builder sweeps a
        whole city.

        `nwr` matches nodes, ways and relations together, because the same
        real-world thing is tagged differently across OSM — a park is
        usually a way, a bus stop always a node, a hospital either. `out
        center` then gives every match a single representative coordinate,
        so ways and relations come back with a usable point instead of a
        list of vertices.
        """
        if (around is None) == (bbox is None):
            raise ValueError("Pass exactly one of `around` or `bbox`.")

        if around is not None:
            lat, lon, radius_m = around
            selector = f"(around:{radius_m},{lat},{lon})"
        else:
            south, west, north, east = bbox  # type: ignore[misc]
            selector = f"({south},{west},{north},{east})"

        clauses = []
        for category in categories:
            for key, value in _OSM_TAGS.get(category, ()):
                clauses.append(f'  nwr{selector}["{key}"="{value}"];')

        body = "\n".join(clauses)
        # `[out:json]` because the XML default would need a second parser.
        return f"[out:json][timeout:60];\n(\n{body}\n);\nout center tags;"

    @staticmethod
    def _parse_elements(data: dict) -> list[OsmPlace]:
        """Turn a raw Overpass response into `OsmPlace` objects.

        Rows are skipped rather than raising when they are unusable:

          * no coordinate — a relation with no computable centre;
          * no name — an unnamed bus shelter is real, but "unnamed place
            42m away" is not evidence a reader can act on;
          * no recognised tag — the query can match a feature whose tags we
            do not map.
        """
        # Invert `_OSM_TAGS` once so each element is classified by lookup
        # rather than by scanning every category.
        tag_to_category: dict[tuple[str, str], NearbyServiceCategory] = {
            pair: category for category, pairs in _OSM_TAGS.items() for pair in pairs
        }

        places: list[OsmPlace] = []
        for element in data.get("elements", []):
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}

            # Nodes carry lat/lon directly; ways and relations carry a
            # computed `center` because of `out center`.
            lat = element.get("lat")
            lon = element.get("lon")
            if lat is None or lon is None:
                center = element.get("center") or {}
                lat = center.get("lat")
                lon = center.get("lon")
            if lat is None or lon is None:
                continue

            category: NearbyServiceCategory | None = None
            for key, value in tags.items():
                found = tag_to_category.get((key, value))
                if found is not None:
                    category = found
                    break
            if category is None:
                continue

            # Prefer the Hebrew name, then English, then the generic tag.
            name = tags.get("name") or tags.get("name:he") or tags.get("name:en")
            if not name:
                continue

            try:
                places.append(
                    OsmPlace(name=str(name).strip(), category=category, lat=lat, lon=lon)
                )
            except Exception:
                # Out-of-range coordinates or similar; skip the row.
                continue

        return places

    # --- public API --------------------------------------------------------

    async def find_places(
        self,
        lat: float,
        lon: float,
        radius_m: int = 500,
        categories: list[NearbyServiceCategory] | None = None,
    ) -> list[OsmPlace]:
        """Find points of interest within `radius_m` of a WGS84 point.

        This is the live per-listing query `nearby_services` would call.

        Returns an empty list (never raises) when the area genuinely has no
        matching places — a business "no answer", which the caller reports
        as `status="ok"` with `places=[]`. Transport failures still
        propagate as typed tool errors from `_post`.
        """
        categories = categories or list(_OSM_TAGS.keys())
        query = self._build_query(categories, around=(lat, lon, radius_m))
        data = await self._post(query)
        return self._parse_elements(data)

    async def find_places_in_bbox(
        self,
        south: float,
        west: float,
        north: float,
        east: float,
        categories: list[NearbyServiceCategory] | None = None,
    ) -> list[OsmPlace]:
        """Sweep every matching place in a bounding box.

        Used only by `build_poi_snapshot.py` to capture a whole city in one
        query, so that `nearby_services` can answer any point inside that
        box offline.
        """
        categories = categories or list(_OSM_TAGS.keys())
        query = self._build_query(categories, bbox=(south, west, north, east))
        data = await self._post(query)
        return self._parse_elements(data)


async def _self_test() -> None:  # pragma: no cover - manual helper
    """Quick manual check: `python -m ...overpass_client`."""
    async with OverpassClient() as client:
        places = await client.find_places(32.0632, 34.7726, 500)
        print(f"{len(places)} places found")
        for place in places[:10]:
            print(f"  {place.category:18s} {place.name}")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_self_test())
