"""Nir's real `nearby_services` implementation, backed by OpenStreetMap.

WHAT THIS TOOL DOES
-------------------
Given the coordinates `geo_resolver` produced, it finds the schools, parks,
supermarkets, pharmacies, daycares, transport stops and hospitals within a
radius, and reports how many there are and how far the nearest one is.

WHERE THE DATA COMES FROM
-------------------------
OpenStreetMap, queried via Overpass. Right now the places are read from a
snapshot committed next to this file rather than fetched live; the reason,
and the one-line switch to go live, are marked `SWAP:` inside `execute()`.

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

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.listing_agent.tools.nir_implementations.coordinates import haversine_distance_m
from src.listing_agent.tools.nir_implementations.overpass_client import (
    OsmPlace,
    # Unused until the SWAP block in `execute()` is uncommented. Kept
    # imported so going live is a one-line change and so the live client
    # stays visibly part of this tool's design.
    OverpassClient,  # noqa: F401
)
from src.listing_agent.tools.schemas import (
    NearbyPlace,
    NearbyServicesInput,
    NearbyServicesOutput,
    ServiceCategorySummary,
    ToolResult,
)

logger = logging.getLogger(__name__)

# Default location of the committed snapshot. Tests inject their own path,
# so they never depend on the real file's contents.
DEFAULT_POI_SNAPSHOT_PATH = Path(__file__).parent / "poi_snapshot.json"

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


class PoiSnapshot:
    """Loads and queries the committed offline points-of-interest snapshot.

    Its only jobs are to read the JSON once, keep it cached, and hand back
    `OsmPlace` objects — the *same* type the live Overpass client produces,
    so the distance maths downstream cannot tell the two apart.
    """

    def __init__(self, path: Path = DEFAULT_POI_SNAPSHOT_PATH) -> None:
        self._path = path
        # Parsed lazily on first use, then cached for the process lifetime.
        # The file is static and city-sized, so re-reading it per request
        # would be pure waste.
        self._data: dict[str, Any] | None = None
        self._places: list[OsmPlace] | None = None

    def _load(self) -> dict[str, Any]:
        """Read and cache the snapshot file.

        Deliberately allowed to raise: a missing or corrupt data file is a
        genuine technical failure, and `execute()` turns it into
        `status="error"`. It must not masquerade as "no services nearby",
        which would tell the reader this is a service desert when in fact
        we never looked.
        """
        if self._data is None:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict) or "places" not in parsed:
                raise ValueError(f"POI snapshot at {self._path} is missing the 'places' key.")
            self._data = parsed
        return self._data

    @property
    def generated_at(self) -> str:
        """The date the snapshot was fetched, disclosed in `limitations`."""
        return str(self._load().get("generated_at", "unknown"))

    @property
    def places(self) -> list[OsmPlace]:
        """Every place in the snapshot, validated once and cached.

        Rows that fail validation are skipped rather than raising, so one
        bad record cannot break the whole lookup.
        """
        if self._places is None:
            places: list[OsmPlace] = []
            for row in self._load().get("places", []):
                try:
                    places.append(OsmPlace.model_validate(row))
                except Exception:
                    logger.warning("Skipping malformed place row in POI snapshot: %r", row)
            self._places = places
        return self._places

    def covers(self, lat: float, lon: float) -> bool:
        """Whether a point falls inside the snapshot's bounding box.

        This matters for honesty. A listing outside the captured area would
        find zero places and look like a service desert, when really we
        simply hold no data there. `execute()` uses this to say which of
        the two happened instead of letting them look identical.
        """
        bbox = self._load().get("bbox")
        if not isinstance(bbox, dict):
            # No bbox recorded: cannot make the claim either way, so do not
            # pretend the point is uncovered.
            return True
        return (
            bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]
        )


class NirNearbyServices:
    """Real `nearby_services` tool backed by OpenStreetMap data."""

    # Must match the whitelisted tool name exactly — the registry looks the
    # tool up by this string (`tools/registry.py`).
    name = "nearby_services"

    def __init__(self, snapshot_path: Path = DEFAULT_POI_SNAPSHOT_PATH) -> None:
        # Injectable so tests can point at a fixture instead of the
        # committed file, and so a corrupt-file test is easy to write.
        self._snapshot = PoiSnapshot(snapshot_path)

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

            # --- 1. Get candidate places -------------------------------
            #
            # SWAP: the live path is written and verified but not enabled.
            # Overpass is a free, frequently overloaded service — its
            # canonical mirror returned HTTP 504 on the first real query
            # attempted here — so every request would inherit that
            # flakiness. The snapshot holds the whole city, so nothing is
            # lost except freshness. See `overpass_client.py`.
            #
            # To go live: uncomment the three lines below, then delete the
            # two snapshot lines beneath them. Nothing else changes — both
            # paths return the same `OsmPlace` objects.
            #
            # async with OverpassClient() as client:
            #     candidates = await client.find_places(
            #         input_model.lat, input_model.lon,
            #         input_model.radius_m, list(wanted),
            #     )
            candidates = self._snapshot.places
            outside_coverage = not self._snapshot.covers(input_model.lat, input_model.lon)

            # --- 2. Filter to the requested categories and radius -------
            #
            # Done locally, which is precisely why a whole-city snapshot
            # can serve any listing and any radius: nothing is pre-computed.
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
            return self._build_result(found, input_model, outside_coverage)

        except Exception as exc:
            # Last-resort safety net. A missing/corrupt snapshot, or any
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
        outside_coverage: bool,
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

        limitations = [_STRAIGHT_LINE_LIMITATION, _OSM_LIMITATION, self._snapshot_limitation()]

        if outside_coverage:
            # The critical honesty case: distinguish "nothing is here" from
            # "we hold no data here".
            limitations.insert(
                0,
                "This location falls outside the area covered by the local dataset, so "
                "the absence of nearby services reflects missing data rather than an "
                "absence of services.",
            )

        return ToolResult(
            status="ok",
            data=NearbyServicesOutput(
                places=places,
                categories=categories,
                evidence_quality=self._assess_quality(places, categories, outside_coverage),
                limitations=limitations,
            ),
        )

    @staticmethod
    def _assess_quality(
        places: list[NearbyPlace],
        categories: list[ServiceCategorySummary],
        outside_coverage: bool,
    ) -> str:
        """How much weight a reader should put on this result.

        Judged on breadth as well as volume: twenty bus stops and nothing
        else is not a well-served neighbourhood, it is a well-mapped bus
        route. Requiring several distinct categories for "high" guards
        against that.
        """
        if outside_coverage:
            # We cannot vouch for anything outside the captured area.
            return "low"
        if (
            len(places) >= _HIGH_QUALITY_MIN_PLACES
            and len(categories) >= _HIGH_QUALITY_MIN_CATEGORIES
        ):
            return "high"
        if len(places) >= _MEDIUM_QUALITY_MIN_PLACES:
            return "medium"
        # Includes the empty case, which the contract requires be "low".
        return "low"

    def _snapshot_limitation(self) -> str:
        """Disclose that the places are a point-in-time extract.

        Required honesty while the offline path is active: a reader must
        not take a months-old snapshot for today's high street. Delete this
        alongside the snapshot when the live path is enabled.
        """
        return (
            f"Place data is a point-in-time extract of OpenStreetMap taken on "
            f"{self._snapshot.generated_at}, not a live query, so recently opened or "
            "closed places may be missing."
        )
