"""Nir's real `geo_resolver` implementation, backed by govmap autocomplete.

WHAT THIS TOOL DOES
-------------------
Takes a free-text address as a human wrote it ("רוטשילד 30 תל אביב", or
something vaguer like "דיזנגוף") and decides whether it points at exactly
one real place. If it does, it returns the canonical form, WGS84
coordinates, and the neighbourhood. If it doesn't, it says so honestly
rather than guessing.

>>> WHY THIS ONE IS LIVE, UNLIKE price_signal <<<
--------------------------------------------------
`price_signal` reads a committed offline snapshot. This tool cannot, and
the difference is worth understanding rather than treating as an
inconsistency:

  * `price_signal` needs AGGREGATE data over a FINITE set of
    neighbourhoods. Ten buckets cover the city, so a snapshot works.
  * `geo_resolver` needs a LOOKUP over an INFINITE key space — every
    address anyone might type, including misspellings. A pre-fetched
    gazetteer would answer `not_found` for anything not on the list, which
    is worse than useless: it would silently turn real addresses into
    failures.

It is also cheap: the common path is a single HTTP call (~0.3s) against
the 8s `LocalToolExecutor` ceiling.

THE RESULT CONTRACT (docs/nir_handoff.md)
-----------------------------------------
`ToolResult.status` says whether the *tool* worked; `outcome` says what it
*found*. "We looked and this address is not real" is a successful run.

    status="ok" + outcome="resolved"    -> one confident match
    status="ok" + outcome="ambiguous"   -> several plausible, none dominant
    status="ok" + outcome="not_found"   -> no match at all, or nothing that
                                           scored well enough to believe
    status="error"                      -> the tool itself broke

TWO INDEPENDENT CONFIDENCE CHECKS
---------------------------------
A match must pass BOTH before it is reported as `resolved`:

  1. an ABSOLUTE floor  (`_MIN_CONFIDENT_SCORE`) - is this match any good?
  2. a RELATIVE gap     (`_DOMINANT_SCORE_RATIO`) - is it clearly better
                                                   than the alternatives?

Only having (2) was a real bug: govmap's fuzzy search always returns its
best guesses and never says "no match", so when the type filter removed
every rival, a junk match was left alone in the list and (2) waved it
through by definition. See `_MIN_CONFIDENT_SCORE`.
"""

import logging

from src.listing_agent.tools.nir_implementations.coordinates import (
    parse_wkt_point,
    web_mercator_to_wgs84,
)
from src.listing_agent.tools.nir_implementations.govmap_client import GovmapClient
from src.listing_agent.tools.schemas import GeoResolverInput, GeoResolverOutput, ToolResult

logger = logging.getLogger(__name__)

_SOURCE = "govmap.gov.il (Israeli government address search)"

# --- Resolution tuning -----------------------------------------------------

# How much the best match must outscore the runner-up to count as a single
# confident answer rather than an ambiguous set.
#
# Calibrated against real API responses:
#   "שבזי 20 תל אביב"  -> 4725 vs 515   (9.2x)  clearly one answer
#   "הרצל תל אביב"     -> 3948 vs 3581  (1.1x)  genuinely ambiguous
#   "דיזנגוף"          -> six results ALL at 1358.9 across different
#                          cities (1.0x) - the API itself cannot choose
#
# 2.0 sits comfortably between the two clusters.
_DOMINANT_SCORE_RATIO = 2.0

# The lowest score a match may have and still be believed at all.
#
# `_DOMINANT_SCORE_RATIO` above is purely RELATIVE — it compares candidates
# against each other. That leaves a hole: when the type filter below removes
# every rival, a single weak survivor has nothing to be compared against and
# was accepted as "confident" no matter how badly it scored. Live example
# that motivated this floor: "כתובת לא קיימת 999 תל אביב" ("address does
# not exist") had its two `transportation` matches filtered out, leaving one
# `street` match for "הקרן קיימת" — scoring 202 — which was then reported as
# a RESOLVED address roughly 5km away, and every dependent tool ran against
# those wrong coordinates.
#
# Sampled from the live API (best addressable match per query):
#   10 real addresses across 7 cities -> 2782 .. 7282
#     lowest:  "יפו 97 ירושלים"        -> 2782
#     highest: "ביאליק 25 רמת גן"      -> 7282
#   nonsense queries                   ->  202 ..  269
#     "כתובת לא קיימת 999 תל אביב"     ->  202  (street "הקרן קיימת")
#     "רחוב שלא נמצא 777 חיפה"         ->  269  (settlement literally "רחוב")
#
# 1000 sits in the empty decade between the two clusters: ~2.8x below the
# weakest real address and ~3.7x above the strongest nonsense match.
_MIN_CONFIDENT_SCORE = 1000.0

# How many alternatives to report when the outcome is ambiguous. Enough to
# be useful to a reader, few enough to stay readable.
_MAX_CANDIDATES = 5

# govmap's result `type` maps directly onto our resolution quality, because
# it says how precisely the query was matched:
#   "address"      -> pinned to a specific building
#   "street"       -> the right street, but no house number
#   "neighborhood" -> only the general area
#   "settlement"   -> only the town
_QUALITY_BY_TYPE: dict[str, str] = {
    "address": "exact",
    "street": "approximate",
    "neighborhood": "low_confidence",
    "settlement": "low_confidence",
    "poi": "approximate",
}

# Only these result types describe a PLACE SOMEONE CAN LIVE AT. Anything
# else govmap indexes is not an address and must never be returned as one.
#
# This filter is load-bearing. Searching the nonsense address
# "כתובת לא קיימת 999 תל אביב" returns three matches — two
# `type="transportation"` entries both literally named "999", plus
# `type="ways"` for "כביש 999" (Highway 999). Without this filter the tool
# reported that gibberish as an `ambiguous` address with candidates
# ["999", "999"], instead of the honest `not_found`.
_ADDRESSABLE_TYPES = frozenset(_QUALITY_BY_TYPE)

_STRAIGHT_LINE_NOTE = (
    "Coordinates are the centre point govmap records for the matched address, "
    "not a surveyed position of the building itself."
)


class NirGeoResolver:
    """Real `geo_resolver` tool backed by the govmap address index."""

    # Must match the whitelisted tool name exactly — the registry looks the
    # tool up by this string (`tools/registry.py`).
    name = "geo_resolver"

    def __init__(
        self,
        client_factory: type[GovmapClient] = GovmapClient,
        resolve_neighborhood: bool = True,
    ) -> None:
        """
        Args:
            client_factory: Injectable so tests can substitute a stub and
                never touch the network. Defaults to the real client.
            resolve_neighborhood: Whether to spend two extra HTTP calls
                discovering the neighbourhood name (see
                `_lookup_neighborhood`). On by default because
                `price_signal` depends on it, but switchable for callers
                that only need coordinates.
        """
        self._client_factory = client_factory
        self._resolve_neighborhood = resolve_neighborhood

    async def execute(self, input_model: GeoResolverInput) -> ToolResult[GeoResolverOutput]:
        """Resolve an address.

        Never raises. Every failure path returns a `ToolResult` instead —
        the outermost `try` guarantees it, because `evidence_collector`
        must survive any single tool misbehaving.
        """
        try:
            # A city hint is folded into the query text rather than sent as
            # a separate parameter, because govmap has no city field — its
            # search is one free-text box. Appending it is what turns the
            # hopeless "דיזנגוף" (six equal matches across six cities) into
            # something resolvable.
            query = input_model.address.strip()
            if input_model.city_hint and input_model.city_hint.strip().lower() not in query.lower():
                query = f"{query} {input_model.city_hint.strip()}"

            async with self._client_factory() as client:
                results = await client.autocomplete(query)

                # --- no match at all: a successful "this is not real" -----
                if not results:
                    return self._not_found(input_model.address)

                # --- keep only things that are actually addresses --------
                # govmap indexes roads, junctions and transport features in
                # the same search. They are not places to live, and letting
                # them through turns nonsense queries into confident-looking
                # answers. See `_ADDRESSABLE_TYPES`.
                usable = [
                    r
                    for r in results
                    if isinstance(r, dict)
                    and str(r.get("type", "")).lower() in _ADDRESSABLE_TYPES
                ]
                if not usable:
                    return self._not_found(input_model.address)

                best = usable[0]

                # --- is the best match believable AT ALL? ----------------
                # Checked before dominance, because dominance is only a
                # relative test: with a single survivor there is nothing to
                # compare against, so a junk match would otherwise pass
                # unchallenged. If even the best candidate scores below the
                # floor, the whole result set is noise and the honest answer
                # is not_found — never a confident address 5km away.
                if not self._clears_score_floor(best):
                    logger.info(
                        "govmap best match for %r scored %s, below the %.0f floor; "
                        "reporting not_found",
                        query,
                        best.get("score"),
                        _MIN_CONFIDENT_SCORE,
                    )
                    return self._not_found(input_model.address)

                if not self._is_dominant(usable):
                    return self._ambiguous(usable)

                # --- one confident match: extract coordinates ------------
                point = parse_wkt_point(best.get("shape"))
                if point is None:
                    # Matched a name but got no usable position. There is
                    # nothing to act on, so this is honestly "not found"
                    # rather than a fabricated coordinate.
                    logger.info("govmap match for %r carried no parsable point", query)
                    return self._not_found(input_model.address)

                # THE CONTRACT BOUNDARY: govmap works in Web Mercator
                # metres, the schema requires WGS84 degrees. This is the one
                # place the conversion is needed.
                lat, lon = web_mercator_to_wgs84(*point)

                neighborhood = None
                if self._resolve_neighborhood:
                    neighborhood = await self._lookup_neighborhood(client, point)

                return self._resolved(best, lat, lon, neighborhood)

        except Exception as exc:
            # Last-resort safety net: an unreachable data source or any
            # unforeseen bug becomes a structured error, never an exception
            # escaping into the agent pipeline.
            logger.exception("geo_resolver failed for %r", input_model.address)
            return ToolResult(
                status="error",
                error=f"geo_resolver could not complete: {type(exc).__name__}: {exc}",
            )

    # --- decision logic ----------------------------------------------------

    @staticmethod
    def _clears_score_floor(result: dict) -> bool:
        """Whether a match scores highly enough to be believed at all.

        An ABSOLUTE check, deliberately separate from `_is_dominant`'s
        relative one — see `_MIN_CONFIDENT_SCORE` for the sampled data
        behind the threshold.

        A missing or unparsable score is treated as failing the floor: the
        API always sends one, so its absence means we cannot judge this
        match, and guessing in favour of it is exactly the mistake this
        check exists to prevent.
        """
        try:
            return float(result.get("score", 0)) >= _MIN_CONFIDENT_SCORE
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_dominant(results: list[dict]) -> bool:
        """Whether the top result is clearly the single right answer.

        A single result is dominant by definition. Otherwise the best score
        must beat the runner-up by `_DOMINANT_SCORE_RATIO`.

        The score gap, not the result count, is what matters: govmap
        routinely returns four matches where the first is 9x the second
        (a clear answer plus noise), and equally returns six matches with
        *identical* scores in six different cities (no answer at all).
        Counting results would misclassify both.
        """
        if len(results) == 1:
            return True
        try:
            best = float(results[0].get("score", 0))
            runner_up = float(results[1].get("score", 0))
        except (TypeError, ValueError):
            return False
        if runner_up <= 0:
            return best > 0
        return best / runner_up >= _DOMINANT_SCORE_RATIO

    async def _lookup_neighborhood(
        self, client: GovmapClient, point: tuple[float, float]
    ) -> str | None:
        """Find the neighbourhood name for a resolved point.

        Costs two extra HTTP calls, and is deliberately best-effort: a
        failure here downgrades the answer (no neighbourhood) but must not
        fail the whole resolution, because the coordinates are already
        correct and useful without it.

        WHY THE ROUNDABOUT ROUTE: govmap has no "neighbourhood at this
        point" endpoint. Its `layers-catalog/entitiesByPoint` returns HTTP
        400 for every payload shape documented by the public reference
        clients. What does work is the real-estate data, where every deal
        carries a `neighborhood` field — so we find one polygon near the
        point and read the neighbourhood off its transactions.

        This matters because `price_signal` keys its comparables by
        neighbourhood, and the value must be the same Hebrew name the deal
        data uses (e.g. "לב העיר") for the two tools to line up.
        """
        try:
            polygon_ids = await client.polygons_by_radius(*point)
            if not polygon_ids:
                return None
            # Any single polygon answers this: every polygon belongs to
            # exactly one neighbourhood, and all deals it returns carry the
            # same name. Verified across 10 polygons per address.
            deals = await client.neighborhood_deals(polygon_ids[0])
            for deal in deals:
                if deal.neighborhood:
                    return deal.neighborhood.strip()
            return None
        except Exception:
            logger.warning("Neighbourhood lookup failed; returning without it", exc_info=True)
            return None

    # --- result builders ---------------------------------------------------

    def _resolved(
        self,
        best: dict,
        lat: float,
        lon: float,
        neighborhood: str | None,
    ) -> ToolResult[GeoResolverOutput]:
        """One confident match."""
        result_type = str(best.get("type", "")).lower()
        quality = _QUALITY_BY_TYPE.get(result_type, "low_confidence")

        limitations = [_STRAIGHT_LINE_NOTE]
        if quality != "exact":
            limitations.append(
                f"Matched at '{result_type or 'unknown'}' level rather than a specific "
                "building, so the position is approximate."
            )
        if neighborhood is None:
            limitations.append(
                "The neighbourhood for this address could not be determined, which may "
                "limit downstream price comparison."
            )

        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="resolved",
                # govmap's canonical spelling, not the caller's raw input.
                normalized_address=str(best.get("text", "")).strip() or None,
                lat=lat,
                lon=lon,
                resolution_quality=quality,
                neighborhood=neighborhood,
                source=_SOURCE,
                limitations=limitations,
            ),
        )

    def _ambiguous(self, results: list[dict]) -> ToolResult[GeoResolverOutput]:
        """Several plausible matches, none dominant.

        Per the contract, `lat`/`lon`/`normalized_address` stay unset —
        there is no single answer to act on, and picking one arbitrarily
        would hand downstream tools a confident-looking wrong location.
        """
        # Deduplicated, because govmap routinely returns the same place
        # name several times (different underlying records). Listing
        # "999, 999" as two distinct options helps nobody choose.
        seen: set[str] = set()
        candidates: list[str] = []
        for result in results:
            text = str(result.get("text", "")).strip()
            if text and text not in seen:
                seen.add(text)
                candidates.append(text)
            if len(candidates) >= _MAX_CANDIDATES:
                break
        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="ambiguous",
                candidates=candidates,
                source=_SOURCE,
                limitations=[
                    f"{len(candidates)} comparable matches were found and none was clearly "
                    "the intended one; a city or house number would disambiguate.",
                ],
            ),
        )

    @staticmethod
    def _not_found(address: str) -> ToolResult[GeoResolverOutput]:
        """No match at all — still a successful run, not an error."""
        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="not_found",
                source=_SOURCE,
                limitations=[
                    f"No Israeli address matching '{address}' was found in the government "
                    "address index.",
                ],
            ),
        )
