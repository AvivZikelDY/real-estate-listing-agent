"""Regression tests for the Phase 18 claim-category correction.

These pin down the exact production bug a real `triage_planner` run
exposed: canonical categories the planner actually emits
(`nearby_amenities`, `price_comparison`, `rooms`, `condition`, `address`,
`size_sqm`, `price`) must classify and link correctly, `price_signal` must
support the comparative claim (not the plain price fact), and a single
shared `geo_resolver` call must serve both `nearby_services` and
`price_signal` dependants.
"""

from src.listing_agent.agent.claim_categories import classify_claim, normalize_category
from src.listing_agent.agent.evidence_mapper import find_relevant_claim_ids
from src.listing_agent.agent.evidence_mapper.claim_linking import relevant_tools_for_claim
from src.listing_agent.agent.schemas import Claim, ToolCall

# The exact claims a real LLMod planner run produced for the sample listing.
ROOMS = Claim(id="claim-1", text="דירת 4 חדרים", type="factual", category="rooms")
CONDITION = Claim(id="claim-2", text="דירה מהממת", type="subjective", category="condition")
ADDRESS = Claim(id="claim-3", text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address")
SIZE_SQM = Claim(id="claim-4", text='100 מ"ר', type="factual", category="size_sqm")
PRICE = Claim(id="claim-5", text='מחיר 4,700,000 ש"ח', type="factual", category="price")
NEARBY_AMENITIES = Claim(
    id="claim-6", text="קרובה לבתי ספר, גנים ופארקים", type="factual", category="nearby_amenities"
)
PRICE_COMPARISON = Claim(
    id="claim-7", text="מחיר מציאה מתחת למחיר השוק", type="factual", category="price_comparison"
)

ALL_CLAIMS = [ROOMS, CONDITION, ADDRESS, SIZE_SQM, PRICE, NEARBY_AMENITIES, PRICE_COMPARISON]


# --- 1: nearby_amenities maps to geo_resolver + nearby_services -----------------


def test_nearby_amenities_maps_to_geo_resolver_and_nearby_services() -> None:
    assert relevant_tools_for_claim(NEARBY_AMENITIES) == ["geo_resolver", "nearby_services"]
    assert find_relevant_claim_ids("nearby_services", ALL_CLAIMS) == ["claim-6"]
    assert "claim-6" in find_relevant_claim_ids("geo_resolver", ALL_CLAIMS)


# --- 2: price_comparison maps to geo_resolver + price_signal --------------------


def test_price_comparison_maps_to_geo_resolver_and_price_signal() -> None:
    assert relevant_tools_for_claim(PRICE_COMPARISON) == ["geo_resolver", "price_signal"]
    assert find_relevant_claim_ids("price_signal", ALL_CLAIMS) == ["claim-7"]
    assert "claim-7" in find_relevant_claim_ids("geo_resolver", ALL_CLAIMS)


# --- 3: "price" alone is a listing fact, not the below-market claim -------------


def test_price_alone_is_a_listing_fact_not_price_comparison() -> None:
    assert classify_claim(PRICE) == "listing_fact"
    assert normalize_category(PRICE.category) == "price"
    # When both "price" (fact) and "price_comparison" (claim) exist in the
    # same plan, price_signal must link to the comparison claim, not price.
    ids = find_relevant_claim_ids("price_signal", ALL_CLAIMS)
    assert ids == ["claim-7"]
    assert "claim-5" not in ids


# --- 4: size_sqm and rooms are listing facts, not unsupported -------------------


def test_size_sqm_and_rooms_are_listing_facts() -> None:
    assert classify_claim(SIZE_SQM) == "listing_fact"
    assert classify_claim(ROOMS) == "listing_fact"
    assert classify_claim(ROOMS) != "unsupported"
    assert classify_claim(SIZE_SQM) != "unsupported"


# --- 5: subjective condition wording remains unverifiable -----------------------


def test_subjective_condition_remains_unverifiable() -> None:
    assert classify_claim(CONDITION) == "subjective"
    assert relevant_tools_for_claim(CONDITION) == []


# --- 6: a single geo_resolver call is shared across nearby + price checks -------


def test_single_geo_resolver_call_shared_across_nearby_and_price() -> None:
    geo_claim_ids = find_relevant_claim_ids("geo_resolver", ALL_CLAIMS)
    assert "claim-6" in geo_claim_ids  # nearby_amenities dependency
    assert "claim-7" in geo_claim_ids  # price_comparison dependency
    # Only one geo_resolver *tool call* is needed in a real plan (asserted
    # at the plan-validator/dedup level in test_plan_validator.py); here we
    # confirm geo_resolver is linked to both dependants from one claim set.


# --- 7: address is a listing fact / location input, not an unsupported claim ---


def test_address_is_listing_fact_feeding_geo_resolver() -> None:
    assert classify_claim(ADDRESS) == "listing_fact"
    assert relevant_tools_for_claim(ADDRESS) == ["geo_resolver"]


# --- 8: price_signal's claim id is the price_comparison claim, explicitly ------


def test_price_signal_supports_price_comparison_claim_id_specifically() -> None:
    call = ToolCall(tool="price_signal", arguments={})
    ids = find_relevant_claim_ids("price_signal", ALL_CLAIMS, call)
    assert ids == ["claim-7"]


# --- 9: explicit tier-1 linkage wins over canonical/legacy heuristics -----------


def test_explicit_supports_claim_ids_takes_priority() -> None:
    call = ToolCall(tool="price_signal", arguments={}, supports_claim_ids=["claim-5"])
    # Even though claim-5 is "price" (a listing fact, not price_comparison),
    # an explicit, valid linkage always wins (forward-compatible hook).
    ids = find_relevant_claim_ids("price_signal", ALL_CLAIMS, call)
    assert ids == ["claim-5"]


# --- 10: legacy bare "price"/"school" categories still work (back-compat) ------


def test_legacy_bare_price_category_still_links_to_price_signal() -> None:
    legacy_claim = Claim(id="claim-x", text="below market price", type="factual", category="price")
    # No price_comparison claim anywhere in this plan -> legacy fallback applies.
    assert find_relevant_claim_ids("price_signal", [ROOMS, legacy_claim]) == ["claim-x"]


def test_legacy_bare_school_category_still_links_to_nearby_services() -> None:
    legacy_claim = Claim(id="claim-y", text="near schools", type="factual", category="school")
    assert find_relevant_claim_ids("nearby_services", [ROOMS, legacy_claim]) == ["claim-y"]


# --- 11: unknown categories remain safely unsupported ---------------------------


def test_unknown_category_is_unsupported_not_a_crash() -> None:
    unknown = Claim(id="claim-z", text="has a nice view", type="factual", category="view_quality")
    assert classify_claim(unknown) == "unsupported"
    assert relevant_tools_for_claim(unknown) == []
