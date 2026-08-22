"""Live `nearby_services` implementation, backed by OpenStreetMap.

WHAT THIS TOOL DOES
-------------------
Given the coordinates `geo_resolver` produced, it finds the schools, parks,
supermarkets, pharmacies, daycares, transport stops and hospitals within a
radius, and reports how many there are and how far the nearest one is.

WHERE THE DATA COMES FROM
-------------------------
OpenStreetMap, queried LIVE via Overpass on every request, with mirror
fallback (see `overpass_client.py`). There is no offline snapshot: a
transport failure propagates up to `status="error"`, same as the other
two tools now that they are all live-only.

>>> DISTANCES ARE STRAIGHT LINES <<<
------------------------------------
Every distance here is as-the-crow-flies. It is NOT walking distance and
NOT walking time: a park 200m away across a rail line or a river may be a
fifteen-minute walk. The contract requires this be disclosed on every
single result (`docs/nir_handoff.md:136`), and `_STRAIGHT_LINE_LIMITATION`
is therefore added unconditionally, in every code path, including the
empty one.

THE RESULT CONTRACT (docs/nir_handoff.md)
-----------------------------------------
This tool has no typed `outcome` field, unlike the other two. An area with
no services is simply a successful result that happens to be empty:

    status="ok"  + places=[] + categories=[] + evidence_quality="low"
    status="error"                -> the tool itself broke
"""

import logging
from collections import defaultdict

from src.listing_agent.tools.live_implementations.coordinates import haversine_distance_m
from src.listing_agent.tools.live_implementations.overpass_client import (
    OsmPlace,
    OverpassClient,
)
from src.listing_agent.tools.schemas import (
    NearbyPlace,
    NearbyServicesInput,
    NearbyServicesOutput,
    ServiceCategorySummary,
    ToolResult,
)

logger = logging.getLogger(__name__)

_SOURCE = "OpenStreetMap (ODbL)"

# Mandatory on every result, whatever the outcome.
_STRAIGHT_LINE_LIMITATION = (
    "Distances are straight-line (as-the-crow-flies) estimates, not walking "
    "distances or walking times; actual routes may be considerably longer."
)
_OSM_LIMITATION = (
    "Places come from OpenStreetMap, a volunteer-maintained dataset: coverage is "
    "good in central Tel Aviv but may be incomplete, and a listed place may have "
    "closed since it was mapped."
)

# --- Evidence-quality thresholds -------------------------------------------
#
# How much to trust the answer, judged by how much evidence there is. The
# risk being guarded against is a thin result reading as authoritative:
# finding two places is not proof a neighbourhood is well served, it is
# mostly proof that someone mapped two places.
_HIGH_QUALITY_MIN_PLACES = 15
_HIGH_QUALITY_MIN_CATEGORIES = 4
_MEDIUM_QUALITY_MIN_PLACES = 5


class LiveNearbyServices:
    """Real `nearby_services` tool backed by OpenStreetMap data."""

    # Must match the whitelisted tool name exactly — the registry looks the
    # tool up by this string (`tools/registry.py`).
    name = "nearby_services"

    def __init__(self, client_factory: type[OverpassClient] = OverpassClient) -> None:
        # Injectable so tests can supply a stub and never touch the network.
        self._client_factory = client_factory

    async def execute(
        self, input_model: NearbyServicesInput
    ) -> ToolResult[NearbyServicesOutput]:
        """Find and summarise nearby services.

        Never raises. Every failure path returns a `ToolResult` instead —
        the outermost `try` guarantees it, because `evidence_collector`
        must survive any single tool misbehaving.
        """
        try:
            wanted = set(input_model.categories)

            # --- 1. Get candidate places ---------------------------------
            # Overpass's own `around:` selector already narrows by radius
            # and the `categories` argument by tag, but step 2 below
            # re-checks both locally rather than trusting that blindly —
            # `around:` can return a way/relation whose computed centre
            # sits just outside the true radius.
            async with self._client_factory() as client:
                candidates = await client.find_places(
                    input_model.lat,
                    input_model.lon,
                    input_model.radius_m,
                    list(wanted),
                )

            # --- 2. Filter to the requested categories and radius -------
            found: list[tuple[OsmPlace, float]] = []
            for place in candidates:
                if place.category not in wanted:
                    continue
                distance = haversine_distance_m(
                    input_model.lat, input_model.lon, place.lat, place.lon
                )
                if distance <= input_model.radius_m:
                    found.append((place, distance))

            # Nearest first, so the most relevant evidence reads first.
            found.sort(key=lambda pair: pair[1])

            # --- 3. Build the result ------------------------------------
            # An empty result is still a success, not an error.
            return self._build_result(found, input_model)

        except Exception as exc:
            # Last-resort safety net. An unreachable Overpass, or any
            # unforeseen bug, becomes a structured error rather than an
            # exception escaping into the agent pipeline.
            logger.exception(
                "nearby_services failed at (%s, %s)", input_model.lat, input_model.lon
            )
            return ToolResult(
                status="error",
                error=f"nearby_services could not complete: {type(exc).__name__}: {exc}",
            )

    # --- result building ---------------------------------------------------

    def _build_result(
        self,
        found: list[tuple[OsmPlace, float]],
        input_model: NearbyServicesInput,
    ) -> ToolResult[NearbyServicesOutput]:
        """Assemble the typed output from the matched places."""
        places = [
            NearbyPlace(
                name=place.name,
                category=place.category,
                # Whole metres: the underlying coordinates are not precise
                # enough to justify decimals, and false precision invites
                # readers to over-trust the number.
                distance_m=round(distance),
                source=_SOURCE,
            )
            for place, distance in found
        ]

        # One summary row per category that actually has something in it.
        # Categories with no matches are omitted rather than reported as
        # zero, because `nearest_distance_m` would have no meaningful value.
        grouped: dict[str, list[float]] = defaultdict(list)
        for place, distance in found:
            grouped[place.category].append(distance)

        categories = [
            ServiceCategorySummary(
                category=category,
                count=len(distances),
                nearest_distance_m=round(min(distances)),
            )
            # Sorted by nearest-first so the strongest evidence leads.
            for category, distances in sorted(grouped.items(), key=lambda kv: min(kv[1]))
        ]

        limitations = [_STRAIGHT_LINE_LIMITATION, _OSM_LIMITATION]

        return ToolResult(
            status="ok",
            data=NearbyServicesOutput(
                places=places,
                categories=categories,
                evidence_quality=self._assess_quality(places, categories),
                limitations=limitations,
            ),
        )

    @staticmethod
    def _assess_quality(
        places: list[NearbyPlace],
        categories: list[ServiceCategorySummary],
    ) -> str:
        """How much weight a reader should put on this result.

        Judged on breadth as well as volume: twenty bus stops and nothing
        else is not a well-served neighbourhood, it is a well-mapped bus
        route. Requiring several distinct categories for "high" guards
        against that.
        """
        if (
            len(places) >= _HIGH_QUALITY_MIN_PLACES
            and len(categories) >= _HIGH_QUALITY_MIN_CATEGORIES
        ):
            return "high"
        if len(places) >= _MEDIUM_QUALITY_MIN_PLACES:
            return "medium"
        # Includes the empty case, which the contract requires be "low".
        return "low"
