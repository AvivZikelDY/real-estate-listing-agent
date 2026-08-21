"""Regression: `_dedupe_claims` silently drops a second, semantically
DISTINCT same-category claim as if it duplicated the typed
`PropertyDetails` field, whenever a listing describes more than one
property/entity (e.g. an urban-renewal listing's CURRENT apartment vs. its
CLAIMED FUTURE replacement apartment).

Diagnosed while evaluating the "דרך השלום" real listing (see
`artifacts/regressions/derech-hashalom/evaluation-summary.md`): the real
LLMod planner produced two distinct, non-duplicate `"size_sqm"`-category
claims - the future replacement apartment's own area, and its balcony
area - neither of which duplicates the *current* apartment's structurally
captured `PropertyDetails.sqm`. `_dedupe_claims`'s
`_REDUNDANT_WITH_PROPERTY_DETAILS` rule assumes any second claim sharing a
"listing-fact" category (`address`, `price`, `rooms`, `size_sqm`) is
necessarily a redundant restatement of the single structured field for
*the* property the request is about - true for a single-property listing,
but false the moment a listing legitimately describes a second entity
(a future replacement unit, a comparison unit, etc.) with its own
distinct size/price/room facts.

This test deliberately avoids the real listing's address, Hebrew wording,
and claim indices/ids: it reproduces the general invariant with
placeholder English text so the regression can never be "fixed" by
special-casing this one listing.
"""

from __future__ import annotations

from src.listing_agent.agent.plan_validator import validate_plan
from src.listing_agent.agent.schemas import Claim, PropertyDetails, TriagePlan


def _plan_with_two_distinct_size_sqm_claims() -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="Some Street 1",
            asking_price=1_000_000,
            rooms=3,
            sqm=60,
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(
                text="A second, different unit offered as future compensation is 82 sqm",
                type="factual",
                category="size_sqm",
            ),
            Claim(
                text="That same future compensation unit also includes a 12 sqm balcony",
                type="factual",
                category="size_sqm",
            ),
        ],
        tool_calls=[],
    )


def test_second_distinct_size_sqm_claim_is_not_dropped_as_a_duplicate() -> None:
    """Two claims sharing the `"size_sqm"` canonical category, but with
    completely distinct text describing a second entity's own area and
    balcony, must both survive `validate_plan` - neither one duplicates
    the single current-property `PropertyDetails.sqm` value, so neither is
    a redundant restatement of it.

    Currently FAILS: `_dedupe_claims`'s `_REDUNDANT_WITH_PROPERTY_DETAILS`
    rule treats the *second* `"size_sqm"`-category claim as a duplicate of
    the first purely by category, regardless of whether its text describes
    the same fact at all, and silently drops it (no `missing_information`
    marker, no priority demotion - it simply never reaches
    `validated_plan.claims`).
    """
    validated = validate_plan(_plan_with_two_distinct_size_sqm_claims())
    retained_texts = {claim.text for claim in validated.claims}

    assert "A second, different unit offered as future compensation is 82 sqm" in retained_texts
    assert (
        "That same future compensation unit also includes a 12 sqm balcony" in retained_texts
    ), (
        "the balcony claim describes a distinct fact about a second entity, not a "
        "duplicate of PropertyDetails.sqm, and must not be dropped by category-based "
        "dedup alone"
    )


# --- Step 7.A: current vs. future area (address-independent) ---------------


def test_case_a_current_vs_future_area_both_survive() -> None:
    """`current: 70 sqm` / `future replacement: 95 sqm` - two distinct
    `size_sqm` values must both survive; neither is a restatement of the
    other."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="Placeholder Street 9", rooms=3, sqm=70
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(
                text="The future replacement unit is 95 sqm",
                type="factual",
                category="size_sqm",
            ),
        ],
        tool_calls=[],
    )
    validated = validate_plan(plan)
    retained_texts = {claim.text for claim in validated.claims}
    assert "The future replacement unit is 95 sqm" in retained_texts


# --- Step 7.B: current-area duplicate is still deduplicated -----------------


def test_case_b_true_current_area_duplicates_are_deduplicated() -> None:
    """Two differently-worded claims that both restate the SAME current
    `PropertyDetails.sqm` value must not both consume claim-cap capacity -
    only one representative should survive as a `size_sqm` claim."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="Placeholder Street 9", rooms=3, sqm=70
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(text="70 sqm", type="factual", category="size_sqm"),
            Claim(text="The apartment is sized at 70 sqm", type="factual", category="size_sqm"),
        ],
        tool_calls=[],
    )
    validated = validate_plan(plan)
    size_claims = [c for c in validated.claims if c.category == "size_sqm"]
    assert len(size_claims) == 1


# --- Step 7.C: future balcony is not the current property's own area -------


def test_case_c_future_balcony_is_not_treated_as_current_area() -> None:
    """current: 60 sqm / future replacement: 82 sqm / future balcony: 12
    sqm - all three are distinct facts; the balcony is not deduplicated
    against `PropertyDetails.sqm` (60) or against the 82 sqm future unit."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="Placeholder Street 9", rooms=2.5, sqm=60
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(
                text="The future replacement unit itself is 82 sqm",
                type="factual",
                category="size_sqm",
            ),
            Claim(
                text="Plus a 12 sqm balcony on the future replacement unit",
                type="factual",
                category="size_sqm",
            ),
        ],
        tool_calls=[],
    )
    validated = validate_plan(plan)
    retained_texts = {claim.text for claim in validated.claims}
    assert "The future replacement unit itself is 82 sqm" in retained_texts
    assert "Plus a 12 sqm balcony on the future replacement unit" in retained_texts


# --- Step 7.D: current vs. future floor -------------------------------------


def test_case_d_floor_is_not_part_of_property_details_dedup_path() -> None:
    """`PropertyDetails` has no `floor` field (matches the pre-existing
    Gonen 14 precedent - floor is only ever a claim, never structural), so
    a floor claim is never normalized to a canonical category in
    `_REDUNDANT_WITH_PROPERTY_DETAILS` in the first place: it never enters
    the property-details-redundancy dedup path at all, and two distinct
    floor claims (current floor 3 vs. a future replacement's floor 5) are
    both retained unconditionally by `_dedupe_claims` (only exact-duplicate
    text would ever be removed). Documented here rather than adding a new
    `PropertyDetails.floor` field/dedup rule, since floor is out of scope
    for the captured multi-entity `size_sqm` defect this fix targets."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="Placeholder Street 9", rooms=3, sqm=70
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(text="Current floor is 3", type="factual", category=None),
            Claim(text="Future replacement floor is 5", type="factual", category=None),
        ],
        tool_calls=[],
    )
    validated = validate_plan(plan)
    retained_texts = {claim.text for claim in validated.claims}
    assert "Current floor is 3" in retained_texts
    assert "Future replacement floor is 5" in retained_texts


# --- Step 7.E: previous Gonen claim-retention behavior is unchanged ---------


def test_case_e_gonen_style_duplicate_address_claims_still_deduplicated() -> None:
    """Two differently-worded `"address"` claims about the SAME current
    listing (the pre-existing Gonen 14 fix behavior) must still collapse
    to a single representative - `"address"` has no numeric value to
    compare, so it keeps its original, coarser category-only dedup rule,
    unaffected by the numeric-value-aware rule added for
    price/rooms/size_sqm."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="Placeholder Ave 14", rooms=5, sqm=123
        ),
        buyer_goal="fast_claim_triage",
        claims=[
            Claim(text="Placeholder Ave 14", type="factual", category="address"),
            Claim(text="Apartment, Placeholder City", type="factual", category="address"),
        ],
        tool_calls=[],
    )
    validated = validate_plan(plan)
    address_claims = [c for c in validated.claims if c.category == "address"]
    assert len(address_claims) == 1
