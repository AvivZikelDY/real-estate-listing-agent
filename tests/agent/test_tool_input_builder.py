"""Tests for `tool_input_builder`'s city-hint extraction and nearby-category
normalization (Phase 18 claim-linkage correction), and for the shared,
single-implementation contract itself.
"""

import pytest

import scripts.inspect_triage_plan as script
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall
from src.listing_agent.agent.tool_input_builder import (
    MissingDependencyError,
    build_tool_input,
    extract_city_hint,
    extract_nearby_categories_argument,
    extract_nearby_query_argument,
    normalize_nearby_categories,
    resolve_nearby_categories,
)
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyServicesInput,
)


def test_extract_city_hint_finds_known_city() -> None:
    assert extract_city_hint("דיזנגוף 100, תל אביב") == "תל אביב"
    assert extract_city_hint("HaYarkon 120, Tel Aviv") == "Tel Aviv"


def test_extract_city_hint_returns_none_when_no_known_city() -> None:
    assert extract_city_hint("Some unknown street 5") is None
    assert extract_city_hint(None) is None


def test_normalize_nearby_categories_merges_and_maps_synonyms() -> None:
    result = normalize_nearby_categories(["school", "kindergarten", "park"])
    assert result == ["school", "daycare", "park"]


def test_normalize_nearby_categories_deduplicates() -> None:
    result = normalize_nearby_categories(["school", "בתי ספר", "schools"])
    assert result == ["school"]


def test_normalize_nearby_categories_drops_unrecognized_and_non_list() -> None:
    assert normalize_nearby_categories(["not_a_category"]) == []
    assert normalize_nearby_categories(None) == []
    assert normalize_nearby_categories("school") == []


def test_geo_resolver_input_includes_extracted_city_hint() -> None:
    call = ToolCall(tool="geo_resolver", arguments={})
    details = PropertyDetails(listing_type="purchase", raw_address="דיזנגוף 100, תל אביב")
    result = build_tool_input(call, details, geo_output=None)
    assert result.address == "דיזנגוף 100, תל אביב"
    assert result.city_hint == "תל אביב"


def test_nearby_services_input_uses_normalized_merged_categories() -> None:
    call = ToolCall(tool="nearby_services", arguments={"categories": ["school", "kindergarten"]})
    details = PropertyDetails(listing_type="purchase", raw_address="דיזנגוף 100, תל אביב")
    geo_output = GeoResolverOutput(
        outcome="resolved",
        normalized_address="Dizengoff 100, Tel Aviv",
        lat=32.08,
        lon=34.78,
        resolution_quality="exact",
        source="mock",
    )
    result = build_tool_input(call, details, geo_output=geo_output)
    assert isinstance(result, NearbyServicesInput)
    assert result.categories == ["school", "daycare"]


def test_nearby_services_input_falls_back_to_default_when_no_recognized_categories() -> None:
    call = ToolCall(tool="nearby_services", arguments={})
    details = PropertyDetails(listing_type="purchase", raw_address="דיזנגוף 100, תל אביב")
    geo_output = GeoResolverOutput(
        outcome="resolved",
        normalized_address="Dizengoff 100, Tel Aviv",
        lat=32.08,
        lon=34.78,
        resolution_quality="exact",
        source="mock",
    )
    result = build_tool_input(call, details, geo_output=geo_output)
    default_categories = NearbyServicesInput.model_fields["categories"].default_factory()  # type: ignore[misc]
    assert result.categories == default_categories


def test_diagnostic_script_reuses_same_build_tool_input() -> None:
    """The diagnostic runner must use the exact same shared implementation
    as the real execution path - never a duplicated/parallel one."""
    assert script.build_tool_input is build_tool_input


# --- Phase 19: `resolve_nearby_categories` priority chain -------------------


def test_resolve_nearby_categories_prefers_explicit_categories() -> None:
    result = resolve_nearby_categories(
        categories=["park"], query="school", claim_text="near schools and parks"
    )
    assert result == ["park"]


def test_resolve_nearby_categories_query_normalizes_to_single_category() -> None:
    """Deviation 3: a planner `query="פארק"` argument (instead of a proper
    `categories` list) must normalize to `["park"]`, never fall back to
    every default category."""
    result = resolve_nearby_categories(query="פארק")
    assert result == ["park"]


def test_resolve_nearby_categories_query_english() -> None:
    assert resolve_nearby_categories(query="park") == ["park"]


def test_resolve_nearby_categories_claim_text_extracts_all_mentioned() -> None:
    result = resolve_nearby_categories(
        claim_text="קרובה לבתי ספר, גנים ופארקים",
    )
    assert result == ["school", "daycare", "park"]


def test_resolve_nearby_categories_unknown_query_does_not_expand_to_all() -> None:
    """An unrecognized `query` value must not silently become every
    default category - it should fall through to `[]` (caller's own
    default) unless a later tier (claim text) produces something."""
    result = resolve_nearby_categories(query="something unrelated")
    assert result == []


def test_resolve_nearby_categories_no_input_returns_empty() -> None:
    assert resolve_nearby_categories() == []


# --- Phase 19: `build_tool_input` claim-text fallback for nearby_services ---


def _resolved_geo_output() -> GeoResolverOutput:
    return GeoResolverOutput(
        outcome="resolved",
        normalized_address="Even Gvirol 80, Tel Aviv",
        lat=32.08,
        lon=34.78,
        resolution_quality="exact",
        source="mock",
    )


def test_build_tool_input_uses_query_argument_for_nearby_services() -> None:
    call = ToolCall(tool="nearby_services", arguments={"query": "פארק"})
    details = PropertyDetails(listing_type="unknown", raw_address="אבן גבירול 80, תל אביב")
    result = build_tool_input(call, details, geo_output=_resolved_geo_output())
    assert isinstance(result, NearbyServicesInput)
    assert result.categories == ["park"]


def test_build_tool_input_uses_linked_claim_text_for_nearby_services() -> None:
    claim = Claim(
        id="claim-1",
        text="קרובה לבתי ספר, גנים ופארקים",
        type="factual",
        category="nearby_amenities",
    )
    call = ToolCall(tool="nearby_services", arguments={}, supports_claim_ids=["claim-1"])
    details = PropertyDetails(listing_type="purchase", raw_address="דיזנגוף 100, תל אביב")
    result = build_tool_input(call, details, geo_output=_resolved_geo_output(), claims=[claim])
    assert isinstance(result, NearbyServicesInput)
    assert result.categories == ["school", "daycare", "park"]


def test_build_tool_input_explicit_categories_win_over_query_and_claim_text() -> None:
    claim = Claim(
        id="claim-1",
        text="קרובה לבתי ספר, גנים ופארקים",
        type="factual",
        category="nearby_amenities",
    )
    call = ToolCall(
        tool="nearby_services",
        arguments={"categories": ["park"], "query": "school"},
        supports_claim_ids=["claim-1"],
    )
    details = PropertyDetails(listing_type="purchase", raw_address="דיזנגוף 100, תל אביב")
    result = build_tool_input(call, details, geo_output=_resolved_geo_output(), claims=[claim])
    assert result.categories == ["park"]


# --- Phase 19 follow-up: real-LLM verification observed the planner using
# `service_type` (not `query`) as a free-text alias for what nearby
# category it wants. ---


def test_extract_nearby_query_argument_finds_service_type_alias() -> None:
    assert extract_nearby_query_argument({"service_type": "park"}) == "park"


def test_extract_nearby_query_argument_prefers_query_over_service_type() -> None:
    args = {"query": "school", "service_type": "park"}
    assert extract_nearby_query_argument(args) == "school"


def test_extract_nearby_query_argument_returns_none_when_no_alias_present() -> None:
    assert extract_nearby_query_argument({"radius_m": 500}) is None


def test_build_tool_input_uses_service_type_argument_for_nearby_services() -> None:
    call = ToolCall(
        tool="nearby_services",
        arguments={"location_raw": "אבן גבירול 80, תל אביב", "service_type": "park"},
    )
    details = PropertyDetails(listing_type="unknown", raw_address="אבן גבירול 80, תל אביב")
    result = build_tool_input(call, details, geo_output=_resolved_geo_output())
    assert isinstance(result, NearbyServicesInput)
    assert result.categories == ["park"]


def test_extract_nearby_categories_argument_finds_services_alias() -> None:
    assert extract_nearby_categories_argument({"services": ["park", "school"]}) == [
        "park",
        "school",
    ]


def test_extract_nearby_categories_argument_prefers_categories_over_services() -> None:
    args = {"categories": ["park"], "services": ["school"]}
    assert extract_nearby_categories_argument(args) == ["park"]


def test_build_tool_input_uses_services_argument_for_nearby_services() -> None:
    call = ToolCall(
        tool="nearby_services",
        arguments={"services": ["park", "school"], "search_radius_m": 1000},
    )
    details = PropertyDetails(listing_type="unknown", raw_address="דיזנגוף 100, תל אביב")
    result = build_tool_input(call, details, geo_output=_resolved_geo_output())
    assert isinstance(result, NearbyServicesInput)
    assert result.categories == ["park", "school"]


# --- An address that names no city stops the run ---------------------------
#
# Live behaviour that motivated this: "בשמואל תמיר 2" with no city returned
# a single best match in BE'ER SHEVA, plus three matches in other towns.
# govmap searches the whole country when it has nothing to narrow by, so a
# city-less address is not a weaker question - it is a different one.


def test_an_address_naming_no_city_refuses_to_run_geo_resolver() -> None:
    """No city, no run: the tool is never given a country-wide guess to make."""
    call = ToolCall(tool="geo_resolver", arguments={})
    details = PropertyDetails(listing_type="purchase", raw_address="בשמואל תמיר 2")

    with pytest.raises(MissingDependencyError) as excinfo:
        build_tool_input(call, details, geo_output=None)

    assert excinfo.value.tool == "geo_resolver"
    # The reason has to name the address, because it becomes the buyer-facing
    # "skipped because ..." line in the evidence bundle.
    assert "בשמואל תמיר 2" in excinfo.value.reason


def test_the_refusal_is_lifted_as_soon_as_the_address_names_a_city() -> None:
    call = ToolCall(tool="geo_resolver", arguments={})
    details = PropertyDetails(listing_type="purchase", raw_address="בשמואל תמיר 2, תל אביב")

    result = build_tool_input(call, details, geo_output=None)

    assert isinstance(result, GeoResolverInput)
    assert result.city_hint == "תל אביב"


def test_city_matching_is_case_insensitive_and_returns_canonical_spelling() -> None:
    """"TEL AVIV" is the same city as "Tel Aviv" and must not stop a run."""
    assert extract_city_hint("120 HaYarkon St, TEL AVIV") == "Tel Aviv"


def test_a_city_name_containing_another_one_wins() -> None:
    """"אור יהודה" contains "יהוד"; the longer, more specific name must win."""
    assert extract_city_hint("הרצל 5, אור יהודה") == "אור יהודה"


def test_gush_dan_cities_beyond_tel_aviv_are_recognised() -> None:
    assert extract_city_hint("ז'בוטינסקי 10, פתח תקווה") == "פתח תקווה"
    assert extract_city_hint("ביאליק 25, רמת גן") == "רמת גן"
    assert extract_city_hint("שמואל 3, גבעת שמואל") == "גבעת שמואל"


def test_azor_is_not_matched_as_a_city() -> None:
    """"אזור" is also the everyday word for "area".

    Matching it would turn "באזור מוקף גינות" - ordinary listing prose - into
    a confident city hint, and send the search to the wrong town.
    """
    assert extract_city_hint("רחוב כלשהו 5, באזור מוקף גינות") is None
