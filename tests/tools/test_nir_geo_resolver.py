"""Tests for `NirGeoResolver` — the govmap-backed real implementation.

Covers requirements 1-3, 10 and 11 of `docs/nir_handoff.md`.

Every test injects a stub client, so nothing here touches the network. The
stub responses are real captured govmap payloads (note the Web Mercator
`shape` values and the pipe-delimited `id`s), which is what makes these
tests meaningful rather than self-fulfilling.
"""

import pytest

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.nir_implementations.geo_resolver import NirGeoResolver
from src.listing_agent.tools.nir_implementations.govmap_client import ComparableDeal
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import GeoResolverInput

# --- Real captured govmap autocomplete payloads ----------------------------

_ROTHSCHILD = {
    "id": "address|ADDR|53785356|רוטשילד|30|תל אביב",
    "text": "רוטשילד 30 תל אביב",
    "type": "address",
    "score": 4462.8228,
    "shape": "POINT(3870871.7058859584 3771605.2672468172)",
}
# "הרצל תל אביב" really returns these three, with scores close enough that
# govmap itself cannot pick a winner.
_HERZL_AMBIGUOUS = [
    {"id": "street|ST|1|הרצל||תל אביב", "text": "הרצל תל-אביב", "type": "street",
     "score": 3948.6, "shape": "POINT(3871000.0 3771000.0)"},
    {"id": "neighborhood|NB|2|גבעת הרצל||תל אביב", "text": "גבעת הרצל תל-אביב",
     "type": "neighborhood", "score": 3581.5, "shape": "POINT(3870000.0 3770000.0)"},
    {"id": "street|ST|3|רוזנבלום הרצל||תל אביב", "text": "רוזנבלום הרצל תל-אביב",
     "type": "street", "score": 3269.7, "shape": "POINT(3869000.0 3769000.0)"},
]


class _StubClient:
    """Minimal stand-in for `GovmapClient`, with no network access.

    Class attributes are set per-test before use, because `NirGeoResolver`
    constructs the factory itself with no arguments.
    """

    results: list[dict] = []
    queries: list[str] = []
    polygons: list[str] = ["7442-22"]
    deals: list[ComparableDeal] = []
    raise_on_autocomplete: Exception | None = None

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def autocomplete(self, search_text: str) -> list[dict]:
        # Every query is recorded, not just the last: the street-level
        # fallback issues a SECOND autocomplete, so "what did the tool ask
        # for?" now has more than one answer and the tests below assert on
        # `queries[0]`, the query the tool actually starts from.
        type(self).last_query = search_text
        type(self).queries = [*type(self).queries, search_text]
        if self.raise_on_autocomplete is not None:
            raise self.raise_on_autocomplete
        return self.results

    async def polygons_by_radius(self, x: float, y: float, radius: int = 300) -> list[str]:
        return self.polygons

    async def neighborhood_deals(self, polygon_id: str) -> list[ComparableDeal]:
        return self.deals


def _stub(**attrs) -> type[_StubClient]:
    """Build a fresh stub class so tests never share state."""
    defaults = {
        "results": [],
        "polygons": ["7442-22"],
        "deals": [],
        "raise_on_autocomplete": None,
        "last_query": None,
        "queries": [],
    }
    return type("_Stub", (_StubClient,), {**defaults, **attrs})


def _deal(neighborhood: str) -> ComparableDeal:
    return ComparableDeal(price=5_000_000, area_sqm=100, neighborhood=neighborhood)


# --- Requirement 1: a known address resolves -------------------------------


@pytest.mark.asyncio
async def test_known_address_resolves_with_coordinates() -> None:
    tool = NirGeoResolver(client_factory=_stub(results=[_ROTHSCHILD], deals=[_deal("לב העיר")]))

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.normalized_address == "רוטשילד 30 תל אביב"
    assert result.data.resolution_quality == "exact"
    assert result.data.neighborhood == "לב העיר"
    # The Web Mercator point must be converted to real WGS84 degrees.
    assert result.data.lat == pytest.approx(32.0632, abs=0.001)
    assert result.data.lon == pytest.approx(34.7726, abs=0.001)
    assert result.error is None


@pytest.mark.asyncio
async def test_resolved_coordinates_satisfy_the_schema_bounds() -> None:
    """The whole point of the coordinate conversion (schemas.py:83-84)."""
    tool = NirGeoResolver(client_factory=_stub(results=[_ROTHSCHILD]))

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.data is not None
    assert -90 <= result.data.lat <= 90
    assert -180 <= result.data.lon <= 180


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_type", "expected_quality"),
    [
        ("address", "exact"),
        ("street", "approximate"),
        ("neighborhood", "low_confidence"),
        ("settlement", "low_confidence"),
    ],
)
async def test_resolution_quality_follows_the_match_type(
    result_type: str, expected_quality: str
) -> None:
    """A street-level match must not claim building-level precision."""
    payload = {**_ROTHSCHILD, "type": result_type}
    tool = NirGeoResolver(client_factory=_stub(results=[payload]))

    result = await tool.execute(GeoResolverInput(address="whatever"))

    assert result.data is not None
    assert result.data.resolution_quality == expected_quality


@pytest.mark.asyncio
@pytest.mark.parametrize("result_type", ["transportation", "ways", "something_new"])
async def test_non_address_result_types_are_rejected(result_type: str) -> None:
    """Roads and junctions are not places to live.

    Real failure this guards: searching the nonsense address
    "כתובת לא קיימת 999 תל אביב" returns two `transportation` records both
    named "999" plus `ways` for Highway 999. Before the type filter, the
    tool reported that as an `ambiguous` address with candidates
    ["999", "999"] instead of the honest `not_found`.

    Unknown future types are rejected too — a whitelist means an
    unrecognised category is excluded by default rather than silently
    treated as an address.
    """
    tool = NirGeoResolver(client_factory=_stub(results=[{**_ROTHSCHILD, "type": result_type}]))

    result = await tool.execute(GeoResolverInput(address="כתובת לא קיימת 999"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "not_found"


@pytest.mark.asyncio
async def test_duplicate_candidate_names_are_deduplicated() -> None:
    """Listing "999, 999" as two options helps nobody choose."""
    results = [
        # Scores mirror the real "הרצל תל אביב" response: close enough that
        # none dominates, but all comfortably above `_MIN_CONFIDENT_SCORE`,
        # so this stays a test about deduplicating candidate NAMES rather
        # than about the score floor (covered separately below).
        {**_ROTHSCHILD, "text": "הרצל תל-אביב", "type": "street", "score": 3948.6},
        {**_ROTHSCHILD, "text": "הרצל תל-אביב", "type": "street", "score": 3910.2},
        {**_ROTHSCHILD, "text": "גבעת הרצל תל-אביב", "type": "street", "score": 3269.7},
    ]
    tool = NirGeoResolver(client_factory=_stub(results=results))

    result = await tool.execute(GeoResolverInput(address="הרצל"))

    assert result.data is not None
    assert result.data.outcome == "ambiguous"
    assert result.data.candidates == ["הרצל תל-אביב", "גבעת הרצל תל-אביב"]


@pytest.mark.asyncio
async def test_dominant_score_resolves_despite_extra_matches() -> None:
    """4 results, but the top one is 9x the runner-up -> one clear answer.

    Real behaviour for "שבזי 20 תל אביב": govmap returns near-miss noise
    alongside the obvious answer. Counting results would wrongly call this
    ambiguous.

    The query names the same address as `_ROTHSCHILD` so that the
    house-number check is satisfied and this stays a test of the DOMINANCE
    rule alone.
    """
    noise = [{**_ROTHSCHILD, "text": f"noise {i}", "score": 515.0} for i in range(3)]
    tool = NirGeoResolver(client_factory=_stub(results=[_ROTHSCHILD, *noise]))

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.data is not None
    assert result.data.outcome == "resolved"


# --- Requirement 2: an unresolvable address -------------------------------


@pytest.mark.asyncio
async def test_unknown_address_returns_not_found_not_an_error() -> None:
    tool = NirGeoResolver(client_factory=_stub(results=[]))

    result = await tool.execute(GeoResolverInput(address="כתובת שלא קיימת בכלל 999"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "not_found"
    assert result.data.lat is None
    assert result.data.lon is None
    assert result.error is None


@pytest.mark.asyncio
async def test_match_without_a_usable_coordinate_is_not_found() -> None:
    """A name with no position is nothing to act on — never invent one."""
    tool = NirGeoResolver(client_factory=_stub(results=[{**_ROTHSCHILD, "shape": "GARBAGE"}]))

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.data is not None
    assert result.data.outcome == "not_found"


# --- The house-number check (`_HOUSE_NUMBER_RE`) ---------------------------
#
# Regression tests for a real false positive: govmap's fuzzy search never
# says "no match", so a junk query still returns its best guesses. Once the
# type filter removed the non-address ones, the single weak survivor was
# accepted as `resolved` because the only confidence check was RELATIVE and
# a lone candidate has no rival to lose to.
#
# The last two tests below are the reason this is a house-number check and
# not a minimum score: the score depends on the QUERY WORDING, so a correct
# match scores 4508 for a clean query and 236 for the prefixed form
# triage_planner actually produces — overlapping the 202 of the junk match.


@pytest.mark.asyncio
async def test_lone_junk_survivor_is_not_found_not_resolved() -> None:
    """The exact live payload that resolved "address does not exist" to a real street.

    Captured from govmap for "כתובת לא קיימת 999 תל אביב": two
    `transportation` matches literally named "999" (filtered out by type),
    leaving one `street` match for "הקרן קיימת" — scoring 202 against the
    ~2800-7300 a genuine address scores. It was reported as a confident
    address roughly 5km away, and every dependent tool then ran against
    those coordinates.
    """
    results = [
        {"id": "vts_DaAB", "text": "999", "type": "transportation", "score": 222.95834,
         "shape": "MULTIPOINT (3907824.30 3870293.16)"},
        {"id": "LBE_DaAB", "text": "999", "type": "transportation", "score": 220.71552,
         "shape": "MULTIPOINT (3918985.30 3738188.70)"},
        {"id": "street|STREET_MID_POINT|36187|הקרן קיימת|תל אביב",
         "text": "הקרן קיימת תל אביב", "type": "street", "score": 202.316,
         "shape": "POINT(3874629.383445255 3779145.1508724773)"},
    ]
    tool = NirGeoResolver(client_factory=_stub(results=results))

    result = await tool.execute(
        GeoResolverInput(address="כתובת לא קיימת 999", city_hint="תל אביב")
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "not_found"
    assert result.data.lat is None, "no coordinates may leak from a rejected match"
    assert result.data.lon is None


@pytest.mark.asyncio
async def test_settlement_match_missing_the_house_number_is_not_found() -> None:
    """Live case: "רחוב שלא נמצא 777 חיפה" matched a settlement named "רחוב".

    Reporting these as `ambiguous` would present noise to the reader as a
    list of plausible addresses to choose between.
    """
    results = [
        {**_ROTHSCHILD, "text": "רחוב", "type": "settlement", "score": 269.1},
        {**_ROTHSCHILD, "text": "רחובות", "type": "settlement", "score": 241.0},
    ]
    tool = NirGeoResolver(client_factory=_stub(results=results))

    result = await tool.execute(GeoResolverInput(address="רחוב שלא נמצא 777 חיפה"))

    assert result.data is not None
    assert result.data.outcome == "not_found"


@pytest.mark.asyncio
async def test_a_street_level_query_with_no_house_number_still_resolves() -> None:
    """No number in the query means there is nothing to verify.

    "דיזנגוף תל אביב" is a legitimate street-level question, and a street
    match is the honest answer to it — the check must not reject it.
    """
    tool = NirGeoResolver(
        client_factory=_stub(
            results=[{**_ROTHSCHILD, "text": "דיזנגוף תל אביב", "type": "street"}]
        )
    )

    result = await tool.execute(GeoResolverInput(address="דיזנגוף תל אביב"))

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.resolution_quality == "approximate"


@pytest.mark.asyncio
async def test_the_prefixed_address_form_the_planner_produces_still_resolves() -> None:
    """The regression that a minimum-score floor caused, pinned as a test.

    `triage_planner` extracts "ברחוב רוטשילד 30 בתל אביב", not the clean
    "רוטשילד 30 תל אביב". govmap returns the SAME correct match for both,
    but scores it 236 instead of 4508 — below the junk match's 202-269
    range. A score floor rejected this real address end to end; the
    house-number check accepts it, because "30" is present in the match.
    """
    tool = NirGeoResolver(
        client_factory=_stub(results=[{**_ROTHSCHILD, "score": 236.72229}])
    )

    result = await tool.execute(
        GeoResolverInput(address="ברחוב רוטשילד 30 בתל אביב", city_hint="תל אביב")
    )

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.normalized_address == "רוטשילד 30 תל אביב"


# --- Requirement 3: an ambiguous address -----------------------------------


@pytest.mark.asyncio
async def test_ambiguous_address_lists_candidates() -> None:
    tool = NirGeoResolver(client_factory=_stub(results=_HERZL_AMBIGUOUS))

    result = await tool.execute(GeoResolverInput(address="הרצל תל אביב"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "ambiguous"
    assert len(result.data.candidates) == 3
    assert "הרצל תל-אביב" in result.data.candidates
    # No single answer, so nothing may be asserted about where it is.
    assert result.data.lat is None
    assert result.data.lon is None
    assert result.data.normalized_address is None


@pytest.mark.asyncio
async def test_identical_scores_across_cities_are_ambiguous() -> None:
    """Real "דיזנגוף" behaviour: six matches, all scoring exactly the same."""
    results = [
        {**_ROTHSCHILD, "text": f"דיזנגוף 1 {city}", "score": 1358.9}
        for city in ("NETANYA", "TLV", "HOLON", "HAIFA", "ASHDOD", "EILAT")
    ]
    tool = NirGeoResolver(client_factory=_stub(results=results))

    result = await tool.execute(GeoResolverInput(address="דיזנגוף"))

    assert result.data is not None
    assert result.data.outcome == "ambiguous"
    # Capped for readability rather than dumping all six.
    assert len(result.data.candidates) == 5


# --- city_hint -------------------------------------------------------------


@pytest.mark.asyncio
async def test_city_hint_is_folded_into_the_query() -> None:
    """govmap has no city field, so the hint must go into the search text."""
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="דיזנגוף", city_hint="תל אביב"))

    assert stub.queries[0] == "דיזנגוף תל אביב"


@pytest.mark.asyncio
async def test_city_hint_is_not_duplicated_when_already_present() -> None:
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב", city_hint="תל אביב"))

    assert stub.queries[0] == "רוטשילד 30 תל אביב"


# --- query normalization (`_FILLER_TOKENS`, city prefix) -------------------
#
# `triage_planner` extracts an address as a human wrote it. Both of these
# were measured against the live API to turn an ordinary listing into an
# unusable `ambiguous`, skipping every dependent tool.


@pytest.mark.asyncio
async def test_the_filler_word_street_is_dropped_from_the_query() -> None:
    """"רחוב" appears in no street NAME, and dilutes the match.

    Live: "ברחוב רוטשילד 30 בתל אביב" returned 10 matches at ratio 1.12 —
    including Rothschild 30 in Ashdod and Kfar Saba — while the stripped
    form returns one match and resolves.
    """
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(
        GeoResolverInput(address="ברחוב רוטשילד 30 בתל אביב", city_hint="תל אביב")
    )

    assert stub.queries[0] == "רוטשילד 30 תל אביב"


@pytest.mark.asyncio
async def test_the_city_prefix_is_removed_only_where_the_hint_identifies_it() -> None:
    """"בתל אביב" -> "תל אביב", using the hint rather than guessing."""
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="דיזנגוף 100 בתל אביב", city_hint="תל אביב"))

    assert stub.queries[0] == "דיזנגוף 100 תל אביב"


@pytest.mark.asyncio
async def test_a_street_name_beginning_with_the_prefix_letter_is_untouched() -> None:
    """Guard against over-eager prefix stripping.

    "ביאליק" starts with the same letter as the "ב" prefix. Removing it
    would search for a street that does not exist, so one-letter prefixes
    are only ever removed where `city_hint` identifies the exact string.
    """
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="ביאליק 25 רמת גן", city_hint="רמת גן"))

    assert stub.queries[0] == "ביאליק 25 רמת גן"


@pytest.mark.asyncio
async def test_an_address_that_is_only_filler_falls_back_to_the_original() -> None:
    """Stripping must never leave an empty query."""
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="רחוב"))

    assert stub.queries[0] == "רחוב"


# --- neighbourhood lookup is best-effort -----------------------------------


@pytest.mark.asyncio
async def test_resolution_survives_a_failing_neighborhood_lookup() -> None:
    """The coordinates are already correct; a missing neighbourhood must
    downgrade the answer, not destroy it."""

    class _Failing(_StubClient):
        results = [_ROTHSCHILD]

        async def polygons_by_radius(self, x, y, radius=300):
            raise RuntimeError("polygon service is down")

    tool = NirGeoResolver(client_factory=_Failing)

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.neighborhood is None
    assert any("neighbourhood" in limitation for limitation in result.data.limitations)


@pytest.mark.asyncio
async def test_neighborhood_lookup_can_be_disabled_to_save_calls() -> None:
    tool = NirGeoResolver(
        client_factory=_stub(results=[_ROTHSCHILD], deals=[_deal("לב העיר")]),
        resolve_neighborhood=False,
    )

    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.neighborhood is None


# --- Requirement 10: internal failures never escape ------------------------


@pytest.mark.asyncio
async def test_data_source_failure_returns_error_not_exception() -> None:
    tool = NirGeoResolver(
        client_factory=_stub(raise_on_autocomplete=RuntimeError("govmap is unreachable"))
    )

    # As much an assertion that this line does not raise as anything else.
    result = await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.status == "error"
    assert result.data is None
    assert result.error is not None


# --- Requirement 11: interface conformance ---------------------------------


def test_tool_name_matches_the_whitelisted_name() -> None:
    assert NirGeoResolver().name == "geo_resolver"


def test_tool_satisfies_the_base_tool_protocol() -> None:
    assert isinstance(NirGeoResolver(), BaseTool)


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry() -> None:
    registry = ToolRegistry()
    registry.register(NirGeoResolver(client_factory=_stub(results=[_ROTHSCHILD])))

    result = await registry.execute("geo_resolver", GeoResolverInput(address="רוטשילד 30 תל אביב"))

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "resolved"


# --- The street-level fallback when a house number is not indexed ----------
#
# All payloads below were captured live for "שמואל תמיר 2, תל אביב", the
# listing address that reported not_found end to end and skipped every
# dependent tool. govmap holds the street (score 4592.9) and holds numbers
# 1, 3, 9, 13, 15, 17, 23, 27, 29, 31, 33, 37, 45 and 47 on it — but not 2.

_SHMUEL_TAMIR_NUMBERS = [
    {"id": "address|ADDR|1|שמואל תמיר|23|תל אביב", "text": "שמואל תמיר 23 תל אביב",
     "type": "address", "score": 275.6, "shape": "POINT(3872000.0 3776000.0)"},
    {"id": "address|ADDR|2|שמואל תמיר|29|תל אביב", "text": "שמואל תמיר 29 תל-אביב",
     "type": "address", "score": 275.6, "shape": "POINT(3872100.0 3776100.0)"},
    {"id": "address|ADDR|3|שמואל תמיר|31|תל אביב", "text": "שמואל תמיר 31 תל-אביב",
     "type": "address", "score": 275.6, "shape": "POINT(3872200.0 3776200.0)"},
]
_SHMUEL_TAMIR_STREET = {
    "id": "street|STREET_MID_POINT|40122|שמואל תמיר|תל אביב",
    "text": "שמואל תמיר תל אביב",
    "type": "street",
    "score": 4592.9,
    "shape": "POINT(3872050.0 3776050.0)",
}


def _stub_by_query(mapping: dict[str, list[dict]], **attrs) -> type[_StubClient]:
    """A stub that answers differently per search text.

    The fallback issues a SECOND autocomplete, so a single fixed response
    cannot express these cases.
    """

    class _ByQuery(_StubClient):
        async def autocomplete(self, search_text: str) -> list[dict]:
            type(self).last_query = search_text
            type(self).queries = [*type(self).queries, search_text]
            return mapping.get(search_text, [])

    return type("_Stub", (_ByQuery,), {"queries": [], "last_query": None, **attrs})


@pytest.mark.asyncio
async def test_a_house_number_missing_from_the_index_falls_back_to_the_street() -> None:
    """The live regression: a real listing that resolved to nothing.

    Every match govmap returns is on the right street and none of them is
    number 2, so the house-number check rejects the best match. Reporting
    not_found there also skipped nearby_services and price_signal, leaving
    the whole listing unchecked — while the same query WITHOUT the number
    resolves cleanly.
    """
    tool = NirGeoResolver(
        client_factory=_stub_by_query(
            {
                # Live behaviour. The prefixed forms return numbered
                # addresses and no street at all; only dropping the "ב"
                # surfaces the street govmap actually holds.
                "בשמואל תמיר 2, תל אביב": _SHMUEL_TAMIR_NUMBERS,
                "בשמואל תמיר תל אביב": _SHMUEL_TAMIR_NUMBERS,
                "שמואל תמיר 2, תל אביב": _SHMUEL_TAMIR_NUMBERS,
                "שמואל תמיר תל אביב": [_SHMUEL_TAMIR_STREET],
            },
            deals=[_deal("רמת אביב")],
        )
    )

    result = await tool.execute(
        GeoResolverInput(address="בשמואל תמיר 2, תל אביב", city_hint="תל אביב")
    )

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.normalized_address == "שמואל תמיר תל אביב"
    # Never "exact": the building itself was not matched.
    assert result.data.resolution_quality == "approximate"
    assert result.data.lat is not None and result.data.lon is not None
    assert any("2" in limitation for limitation in result.data.limitations), (
        "the missing house number must be stated, not silently dropped"
    )


@pytest.mark.asyncio
async def test_the_fallback_removes_only_the_house_number() -> None:
    """The retries ask progressively less, and never more.

    The listing's own wording is always tried first, in full; the house
    number goes before the preposition does; and the preposition is only
    dropped once every faithful form has failed. No step ever adds
    anything the listing did not say.
    """
    stub = _stub_by_query(
        {
            "בשמואל תמיר 2, תל אביב": _SHMUEL_TAMIR_NUMBERS,
            "בשמואל תמיר תל אביב": _SHMUEL_TAMIR_NUMBERS,
            "שמואל תמיר 2, תל אביב": _SHMUEL_TAMIR_NUMBERS,
            "שמואל תמיר תל אביב": [_SHMUEL_TAMIR_STREET],
        }
    )
    tool = NirGeoResolver(client_factory=stub, resolve_neighborhood=False)

    await tool.execute(GeoResolverInput(address="בשמואל תמיר 2, תל אביב", city_hint="תל אביב"))

    assert stub.queries == [
        # What the listing said, unchanged - always asked first.
        "בשמואל תמיר 2, תל אביב",
        # Same wording, house number dropped: is the street itself there?
        "בשמואל תמיר תל אביב",
        # Only now is the "ב" dropped, and only because everything the
        # listing literally said has already failed.
        "שמואל תמיר 2, תל אביב",
        "שמואל תמיר תל אביב",
    ]


@pytest.mark.asyncio
async def test_the_fallback_does_not_resurrect_the_junk_match() -> None:
    """The hole the fallback must not re-open.

    "כתובת לא קיימת 999 תל אביב" fuzzy-matches the real street
    "הקרן קיימת" on the single shared word "קיימת". Dropping the house
    number and accepting whatever street comes back would hand that junk a
    confident answer again — so the name check rejects it.
    """
    junk_street = {
        "id": "street|STREET_MID_POINT|36187|הקרן קיימת|תל אביב",
        "text": "הקרן קיימת תל אביב",
        "type": "street",
        "score": 202.316,
        "shape": "POINT(3874629.383445255 3779145.1508724773)",
    }
    tool = NirGeoResolver(
        client_factory=_stub_by_query(
            {
                "כתובת לא קיימת 999 תל אביב": [junk_street],
                "כתובת לא קיימת תל אביב": [junk_street],
            }
        )
    )

    result = await tool.execute(
        GeoResolverInput(address="כתובת לא קיימת 999", city_hint="תל אביב")
    )

    assert result.data is not None
    assert result.data.outcome == "not_found"
    assert result.data.lat is None


@pytest.mark.asyncio
async def test_the_fallback_refuses_anything_coarser_than_a_street() -> None:
    """A settlement match means the street itself was never found."""
    settlement = {**_ROTHSCHILD, "text": "רחוב", "type": "settlement", "score": 269.1}
    tool = NirGeoResolver(
        client_factory=_stub_by_query(
            {"רחוב שלא נמצא 777 חיפה": [settlement], "רחוב שלא נמצא חיפה": [settlement]}
        )
    )

    result = await tool.execute(GeoResolverInput(address="רחוב שלא נמצא 777 חיפה"))

    assert result.data is not None
    assert result.data.outcome == "not_found"


@pytest.mark.asyncio
async def test_the_fallback_stays_unresolved_between_two_similar_streets() -> None:
    """Dominance still applies: two plausible streets are not an answer."""
    streets = [
        {**_SHMUEL_TAMIR_STREET, "text": "שמואל תמיר תל אביב", "score": 4592.9},
        {**_SHMUEL_TAMIR_STREET, "text": "שמואל תמיר רמת גן", "score": 4400.0},
    ]
    tool = NirGeoResolver(
        client_factory=_stub_by_query(
            {"שמואל תמיר 2": _SHMUEL_TAMIR_NUMBERS, "שמואל תמיר": streets}
        )
    )

    result = await tool.execute(GeoResolverInput(address="שמואל תמיר 2"))

    assert result.data is not None
    assert result.data.outcome == "not_found"


# --- The Hebrew preposition on the street name -----------------------------
#
# "ב" means "in/at" and is written joined to the next word, so a listing
# that says where a flat IS hands us a street name that does not exist.
# govmap scores on word overlap and is unforgiving about it - measured:
#   "בשמואל תמיר 2 תל אביב" -> the WRONG street ("תמיר"), top score 225.5
#   "שמואל תמיר 2 תל אביב"  -> the right street, top score 275.6


@pytest.mark.asyncio
async def test_a_prefixed_street_name_is_retried_without_the_prefix() -> None:
    """The faithful query is asked first, and its failure is not the end."""
    prefixed_miss = [
        {"id": "address|ADDR|9|תמיר|15|תל אביב", "text": "תמיר 15 תל אביב",
         "type": "address", "score": 225.5, "shape": "POINT(3880000.0 3780000.0)"},
        {"id": "address|ADDR|9|תמיר|3|תל אביב", "text": "תמיר 3 תל אביב",
         "type": "address", "score": 225.5, "shape": "POINT(3880100.0 3780100.0)"},
    ]
    correct = {**_ROTHSCHILD, "text": "שמואל תמיר 2 תל אביב", "score": 4500.0}
    stub = _stub_by_query(
        {
            "בשמואל תמיר 2 תל אביב": prefixed_miss,
            "שמואל תמיר 2 תל אביב": [correct],
        }
    )
    tool = NirGeoResolver(client_factory=stub, resolve_neighborhood=False)

    result = await tool.execute(
        GeoResolverInput(address="בשמואל תמיר 2 תל אביב", city_hint="תל אביב")
    )

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.normalized_address == "שמואל תמיר 2 תל אביב"
    assert stub.queries[0] == "בשמואל תמיר 2 תל אביב", "the listing's wording goes first"


@pytest.mark.asyncio
async def test_the_retry_must_still_name_the_place_the_listing_named() -> None:
    """Dropping a letter must not be allowed to change the answer.

    Stripping "ב" off a street that really begins with it ("ביאליק" ->
    "יאליק") searches for something that does not exist. Whatever govmap
    replies with then has to be checked back against the original wording,
    or the retry would quietly resolve a different street.
    """
    stub = _stub_by_query(
        {
            "ביאליק 25 רמת גן": [],
            # A plausible-looking but different street, as a fuzzy search
            # for a non-existent name would return.
            "יאליק 25 רמת גן": [{**_ROTHSCHILD, "text": "יהלום 25 רמת גן"}],
        }
    )
    tool = NirGeoResolver(client_factory=stub, resolve_neighborhood=False)

    result = await tool.execute(GeoResolverInput(address="ביאליק 25 רמת גן"))

    assert result.data is not None
    assert result.data.outcome == "not_found"
    assert result.data.lat is None


@pytest.mark.asyncio
async def test_a_street_that_really_begins_with_the_prefix_letter_still_resolves() -> None:
    """The retry is additive: it cannot cost a match the faithful query got."""
    bialik = {**_ROTHSCHILD, "text": "ביאליק 25 רמת גן"}
    stub = _stub_by_query({"ביאליק 25 רמת גן": [bialik]}, deals=[_deal("מרכז")])
    tool = NirGeoResolver(client_factory=stub)

    result = await tool.execute(GeoResolverInput(address="ביאליק 25 רמת גן"))

    assert result.data is not None
    assert result.data.outcome == "resolved"
    assert result.data.normalized_address == "ביאליק 25 רמת גן"
    # Resolved on the first ask, so the prefix-stripped form is never sent.
    assert stub.queries == ["ביאליק 25 רמת גן"]


@pytest.mark.asyncio
async def test_a_failed_retry_reports_the_faithful_query_outcome() -> None:
    """`ambiguous` beats the retry's `not_found`: it carries candidates.

    The reader can act on a list of places to choose between; they cannot
    act on silence produced by a question the listing never asked.
    """
    stub = _stub_by_query({"בהרצל תל אביב": _HERZL_AMBIGUOUS, "הרצל תל אביב": []})
    tool = NirGeoResolver(client_factory=stub, resolve_neighborhood=False)

    result = await tool.execute(GeoResolverInput(address="בהרצל תל אביב"))

    assert result.data is not None
    assert result.data.outcome == "ambiguous"
    assert result.data.candidates
