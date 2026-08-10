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
    polygons: list[str] = ["7442-22"]
    deals: list[ComparableDeal] = []
    raise_on_autocomplete: Exception | None = None

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def autocomplete(self, search_text: str) -> list[dict]:
        type(self).last_query = search_text
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
        {**_ROTHSCHILD, "text": "הרצל תל-אביב", "type": "street", "score": 300.0},
        {**_ROTHSCHILD, "text": "הרצל תל-אביב", "type": "street", "score": 299.0},
        {**_ROTHSCHILD, "text": "גבעת הרצל תל-אביב", "type": "street", "score": 298.0},
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
    """
    noise = [{**_ROTHSCHILD, "text": f"noise {i}", "score": 515.0} for i in range(3)]
    tool = NirGeoResolver(client_factory=_stub(results=[_ROTHSCHILD, *noise]))

    result = await tool.execute(GeoResolverInput(address="שבזי 20 תל אביב"))

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

    assert stub.last_query == "דיזנגוף תל אביב"


@pytest.mark.asyncio
async def test_city_hint_is_not_duplicated_when_already_present() -> None:
    stub = _stub(results=[_ROTHSCHILD])
    tool = NirGeoResolver(client_factory=stub)

    await tool.execute(GeoResolverInput(address="רוטשילד 30 תל אביב", city_hint="תל אביב"))

    assert stub.last_query == "רוטשילד 30 תל אביב"


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
