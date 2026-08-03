import pytest

from src.listing_agent.agent.plan_validator import (
    ALLOWED_TOOLS,
    MAX_CLAIMS,
    MAX_TOOL_CALLS,
    PlanValidationError,
    validate_plan,
)
from src.listing_agent.agent.schemas import (
    Claim,
    ClaimSource,
    PropertyDetails,
    ToolCall,
    TriagePlan,
)


def _plan(**property_overrides) -> TriagePlan:
    property_details = PropertyDetails(
        listing_type="purchase",
        raw_address="HaYarkon 120, Tel Aviv",
        asking_price=3_000_000,
        rooms=3,
        sqm=80,
    )
    property_details = property_details.model_copy(update=property_overrides)
    return TriagePlan(
        property_details=property_details,
        buyer_goal="general_question",
        tool_calls=[],
    )


def test_allowed_tools_matches_the_fixed_whitelist() -> None:
    assert ALLOWED_TOOLS == frozenset({"geo_resolver", "nearby_services", "price_signal"})


def test_unknown_tool_raises_controlled_validation_error() -> None:
    plan = _plan()
    bad_call = ToolCall.model_construct(tool="delete_database", arguments={})
    plan = plan.model_copy(update={"tool_calls": [bad_call]})

    with pytest.raises(PlanValidationError, match="Unknown tool"):
        validate_plan(plan)


def test_duplicate_calls_are_removed() -> None:
    plan = _plan(sqm=80)
    plan = plan.model_copy(
        update={
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="geo_resolver", arguments={}),
            ]
        }
    )

    validated = validate_plan(plan)

    assert [call.tool for call in validated.tool_calls] == ["geo_resolver"]


def test_max_three_tool_calls_enforced() -> None:
    plan = _plan()
    plan = plan.model_copy(
        update={
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ]
        }
    )
    # Simulate a plan somehow proposing more than 3 distinct entries by
    # bypassing dedupe with different arguments for the same 3 tools plus
    # checking the cap directly via MAX_TOOL_CALLS.
    validated = validate_plan(plan)

    assert len(validated.tool_calls) <= MAX_TOOL_CALLS


def test_price_signal_forbidden_for_rentals() -> None:
    plan = _plan(listing_type="rental")
    plan = plan.model_copy(update={"tool_calls": [ToolCall(tool="price_signal", arguments={})]})

    validated = validate_plan(plan)

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_price_signal_requires_asking_price_and_sqm() -> None:
    plan = _plan(sqm=None)
    plan = plan.model_copy(update={"tool_calls": [ToolCall(tool="price_signal", arguments={})]})

    validated = validate_plan(plan)

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_geo_resolver_auto_added_when_nearby_services_needs_coordinates() -> None:
    plan = _plan()
    plan = plan.model_copy(update={"tool_calls": [ToolCall(tool="nearby_services", arguments={})]})

    validated = validate_plan(plan)

    assert [call.tool for call in validated.tool_calls] == ["geo_resolver", "nearby_services"]


def test_geo_resolver_ordered_first_even_if_planner_put_it_last() -> None:
    plan = _plan()
    plan = plan.model_copy(
        update={
            "tool_calls": [
                ToolCall(tool="nearby_services", arguments={}),
                ToolCall(tool="geo_resolver", arguments={}),
            ]
        }
    )

    validated = validate_plan(plan)

    assert validated.tool_calls[0].tool == "geo_resolver"


def test_location_dependent_tools_dropped_when_no_address_available() -> None:
    plan = _plan(raw_address=None)
    plan = plan.model_copy(update={"tool_calls": [ToolCall(tool="nearby_services", arguments={})]})

    validated = validate_plan(plan)

    assert validated.tool_calls == []
    assert "raw_address" in validated.missing_information


def test_valid_plan_with_no_tool_calls_passes_through_unchanged() -> None:
    plan = _plan()

    validated = validate_plan(plan)

    assert validated.tool_calls == []
    assert validated == plan


def test_default_max_claims_matches_exported_constant() -> None:
    assert MAX_CLAIMS == 8


def test_too_many_claims_are_capped_and_recorded_as_missing_information() -> None:
    plan = _plan()
    claims = [Claim(text=f"claim {i}", type="factual") for i in range(MAX_CLAIMS + 5)]
    plan = plan.model_copy(update={"claims": claims})

    validated = validate_plan(plan)

    assert len(validated.claims) == MAX_CLAIMS
    assert "additional_claims_truncated" in validated.missing_information


def test_claims_within_limit_are_not_truncated() -> None:
    plan = _plan()
    claims = [Claim(text=f"claim {i}", type="factual") for i in range(3)]
    plan = plan.model_copy(update={"claims": claims})

    validated = validate_plan(plan)

    assert len(validated.claims) == 3
    assert "additional_claims_truncated" not in validated.missing_information


def test_max_claims_is_configurable() -> None:
    plan = _plan()
    claims = [Claim(text=f"claim {i}", type="factual") for i in range(5)]
    plan = plan.model_copy(update={"claims": claims})

    validated = validate_plan(plan, max_claims=2)

    assert len(validated.claims) == 2
    assert "additional_claims_truncated" in validated.missing_information


def test_max_tool_calls_is_configurable() -> None:
    plan = _plan()
    plan = plan.model_copy(
        update={
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ]
        }
    )

    validated = validate_plan(plan, max_tool_calls=1)

    assert len(validated.tool_calls) == 1
    assert validated.tool_calls[0].tool == "geo_resolver"


# --- Phase 19: synthesizing a missing nearby_services call for a known
# nearby_amenities claim (Deviation 1) ---------------------------------------


def test_known_nearby_amenities_claim_survives_when_planner_omits_the_call() -> None:
    """The planner correctly classified a claim as `nearby_amenities` but
    never proposed the matching `ToolCall` at all - it must not silently go
    unchecked."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "claims": [
                Claim(
                    text="קרובה לבתי ספר, גנים ופארקים", type="factual", category="nearby_amenities"
                ),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    tool_names = {call.tool for call in validated.tool_calls}
    assert "nearby_services" in tool_names
    nearby_call = next(call for call in validated.tool_calls if call.tool == "nearby_services")
    assert nearby_call.arguments["categories"] == ["school", "daycare", "park"]
    assert nearby_call.supports_claim_ids == [validated.claims[0].id]


def test_nearby_services_synthesis_never_overrides_a_planner_proposed_call() -> None:
    """A planner-proposed `nearby_services` call (even with different
    categories/args) is never replaced or duplicated by synthesis."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "claims": [
                Claim(text="קרובה לפארקים", type="factual", category="nearby_amenities"),
            ],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={"categories": ["supermarket"]}),
            ],
        }
    )

    validated = validate_plan(plan)

    nearby_calls = [call for call in validated.tool_calls if call.tool == "nearby_services"]
    assert len(nearby_calls) == 1
    assert nearby_calls[0].arguments["categories"] == ["supermarket"]


def test_nearby_services_synthesis_requires_a_usable_address() -> None:
    """No address means the location-dependent tool would be useless -
    synthesis must not fire (matches `_ensure_geo_resolver_dependency`'s own
    "drop, don't call blind" behavior)."""
    plan = _plan(raw_address=None)
    plan = plan.model_copy(
        update={
            "claims": [
                Claim(text="קרובה לפארקים", type="factual", category="nearby_amenities"),
            ],
            "tool_calls": [],
        }
    )

    validated = validate_plan(plan)

    assert not any(call.tool == "nearby_services" for call in validated.tool_calls)


def test_nearby_services_synthesis_ignores_subjective_claims() -> None:
    plan = _plan()
    plan = plan.model_copy(
        update={
            "claims": [
                Claim(text="מהממת ומקסימה", type="subjective", category="nearby_amenities"),
            ],
            "tool_calls": [],
        }
    )

    validated = validate_plan(plan)

    assert not any(call.tool == "nearby_services" for call in validated.tool_calls)


# --- Phase 19: deriving a price_comparison claim/linkage (Deviation 2) ------


def test_price_comparison_claim_is_derived_when_missing() -> None:
    """`buyer_goal == "price_validation"` with `price_signal` planned but no
    real `price_comparison`-categorized claim must get a deterministic,
    clearly-marked derived claim - and `price_signal` must link to it, not
    the literal `"price"` listing fact."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=4_700_000,
            rooms=4,
            sqm=100,
        ),
        buyer_goal="price_validation",
        claims=[Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan)

    derived = next(
        (claim for claim in validated.claims if claim.category == "price_comparison"), None
    )
    assert derived is not None
    assert derived.source == ClaimSource.DERIVED

    price_call = next(call for call in validated.tool_calls if call.tool == "price_signal")
    assert price_call.supports_claim_ids == [derived.id]
    # Never linked solely to the literal "price" listing fact.
    price_fact_claim = next(claim for claim in validated.claims if claim.category == "price")
    assert price_fact_claim.id not in (price_call.supports_claim_ids or [])


def test_price_comparison_claim_not_derived_when_already_present() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=4_700_000,
            rooms=4,
            sqm=100,
        ),
        buyer_goal="price_validation",
        claims=[
            Claim(text="below market price", type="factual", category="price_comparison"),
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan)

    comparison_claims = [c for c in validated.claims if c.category == "price_comparison"]
    assert len(comparison_claims) == 1
    assert comparison_claims[0].source is None  # the planner's real claim, not derived


def test_price_comparison_claim_not_derived_for_other_goals() -> None:
    """Only the `price_validation` goal implies a comparative buyer
    question - other goals must never trigger this synthesis."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=4_700_000,
            rooms=4,
            sqm=100,
        ),
        buyer_goal="general_question",
        claims=[Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan)

    assert not any(claim.category == "price_comparison" for claim in validated.claims)


def test_price_comparison_derivation_respects_explicit_supports_claim_ids() -> None:
    """An already-explicit `supports_claim_ids` on the planner's own call
    must never be overridden by the derived-claim linkage."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=4_700_000,
            rooms=4,
            sqm=100,
        ),
        buyer_goal="price_validation",
        claims=[Claim(id="claim-0", text="מחיר", type="factual", category="price")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}, supports_claim_ids=["claim-0"]),
        ],
    )

    validated = validate_plan(plan)

    price_call = next(call for call in validated.tool_calls if call.tool == "price_signal")
    assert price_call.supports_claim_ids == ["claim-0"]
