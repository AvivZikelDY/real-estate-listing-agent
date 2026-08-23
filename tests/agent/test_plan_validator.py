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
    unchecked. This is the default/full-listing (`fast_claim_triage`)
    scenario Phase 19 synthesis exists for."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
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


def test_nearby_services_synthesis_does_not_fire_for_price_validation_goal() -> None:
    """Stage 1.5/1.6 regression (real Listing A / A3 scenario): when the
    buyer's explicit goal this turn is `"price_validation"`, a retained
    `nearby_amenities` claim (e.g. "קרובה לפארק") must NOT cause
    `nearby_services` to be synthesized behind the planner's back, even
    though the planner itself omitted the call on purpose. Only
    `geo_resolver`/`price_signal` (the planner's own selection) should
    survive."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "price_validation",
            "claims": [
                Claim(text="קרובה לפארק", type="factual", category="nearby_amenities"),
                Claim(text="מחיר מציאה!", type="factual", category="price_comparison"),
            ],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        }
    )

    validated = validate_plan(plan, buyer_prompt="האם המחיר הגיוני ביחס לאזור?")

    tool_names = {call.tool for call in validated.tool_calls}
    assert tool_names == {"geo_resolver", "price_signal"}
    assert "nearby_services" not in tool_names


def test_nearby_services_synthesis_still_fires_for_family_suitability_goal() -> None:
    """Guards against over-fixing: a narrowed `"family_suitability"` goal is
    exactly the case where a known `nearby_amenities` claim the planner
    omitted must still be repaired - only `"price_validation"` and
    `"general_question"` are excluded (Stage 1.6 / Stage 4.4)."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "family_suitability",
            "claims": [
                Claim(text="קרובה לפארק", type="factual", category="nearby_amenities"),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    assert any(call.tool == "nearby_services" for call in validated.tool_calls)


def test_nearby_services_synthesis_still_fires_for_fast_claim_triage_goal() -> None:
    """Guards against over-fixing: a plain listing with no buyer question
    (`"fast_claim_triage"`) is the original Phase 19 use case this synthesis
    exists for - a retained `nearby_amenities` claim the planner omitted a
    tool call for must still be repaired."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(text="קרובה לפארק", type="factual", category="nearby_amenities"),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    assert any(call.tool == "nearby_services" for call in validated.tool_calls)


def test_nearby_services_synthesis_does_not_fire_for_general_question_goal() -> None:
    """Stage 4.3/4.4 regression (real "האם מתאימה לסטודנטים?" scenario):
    when the buyer's explicit goal this turn is `"general_question"` and the
    planner itself proposed no tools (`tool_calls=[]`), a merely-retained,
    unrelated `nearby_amenities` listing claim (e.g. "מול פארק ירוק ופתוח")
    must NOT cause `nearby_services`/`geo_resolver` to be synthesized behind
    the planner's back - `general_question` selects a tool only when the
    current question itself asks for what that tool provides."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "general_question",
            "claims": [
                Claim(text="מול פארק ירוק ופתוח", type="factual", category="nearby_amenities"),
            ],
            "tool_calls": [],
        }
    )

    validated = validate_plan(plan, buyer_prompt="האם מתאימה לסטודנטים?")

    assert validated.tool_calls == []


# --- Phase 19: deriving a price_comparison claim/linkage (Deviation 2) ------


def test_price_signal_dropped_without_comparative_provenance() -> None:
    """Phase 0 - price provenance guard: `buyer_goal == "price_validation"`
    with `price_signal` planned, no real `price_comparison`-categorized
    claim, and no explicit comparative-price intent in the buyer's prompt
    must NOT be enough to keep `price_signal` - and no derived claim may be
    synthesized to self-justify it either."""
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

    assert "price_signal" not in {call.tool for call in validated.tool_calls}
    assert not any(claim.category == "price_comparison" for claim in validated.claims)


def test_price_signal_kept_when_buyer_prompt_has_comparative_intent() -> None:
    """Justification A: an explicit comparative-price question in the
    current turn's raw buyer prompt keeps `price_signal` and still derives
    a clearly-marked comparative claim to link it to."""
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

    validated = validate_plan(plan, buyer_prompt="Is the asking price fair?")

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


def test_price_signal_kept_for_colloquial_excellent_price_phrasing() -> None:
    """Colloquial price-praise phrasing ("מחיר מצוין" - excellent price) is
    comparative-price intent too, not just "מציאה" (bargain): the raw
    listing text itself is the current turn's buyer_prompt for a plain
    fast_claim_triage submission, so this phrasing must independently
    justify `price_signal` via `prompt_has_comparative_price_intent`, the
    same as an explicit "fair"/"bargain" question does."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=2_400_000,
            rooms=3.5,
            sqm=84,
        ),
        buyer_goal="fast_claim_triage",
        claims=[Claim(text="מחיר מצוין!", type="factual", category="price")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan, buyer_prompt="דירה למכירה, מחיר מצוין!")

    assert "price_signal" in {call.tool for call in validated.tool_calls}


def test_price_signal_kept_for_hebrew_good_price_follow_up() -> None:
    """"האם זה מחיר טוב?" ("is this a good price?") is comparative-price
    intent, exactly as its English equivalent "is this a good price?" has
    always been. This is the plainest way a Hebrew speaker asks the
    question, and the guard used to drop `price_signal` for it - a
    follow-up turn states no price/size of its own, so the plan's own
    claims cannot justify the call either."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="בוקי בן יגלי 4, תל אביב",
            asking_price=5_000_000,
            sqm=90,
        ),
        buyer_goal="price_validation",
        claims=[],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan, buyer_prompt="האם זה מחיר טוב?")

    assert "price_signal" in {call.tool for call in validated.tool_calls}


def test_generic_hebrew_good_claim_without_price_reference_does_not_justify_price_signal() -> None:
    """"טוב" on its own is as generic as "מצוין" - "מיקום טוב" (good
    location) must not smuggle `price_signal` in without a price/cost word
    somewhere in the same prompt."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=2_400_000,
            rooms=3.5,
            sqm=84,
        ),
        buyer_goal="fast_claim_triage",
        claims=[Claim(text="מיקום טוב", type="subjective", category="condition")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan, buyer_prompt="דירה למכירה, מיקום טוב")

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_generic_excellent_claim_without_price_reference_does_not_justify_price_signal() -> None:
    """"מצוין"/"מעולה" alone (no accompanying "מחיר"/price reference) must
    NOT count as comparative-price intent - it is a generic adjective used
    for anything ("מיזוג אוויר מצוין" - excellent AC), and must not smuggle
    price_signal in via an unrelated claim."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=2_400_000,
            rooms=3.5,
            sqm=84,
        ),
        buyer_goal="fast_claim_triage",
        claims=[Claim(text="מיזוג אוויר מצוין", type="factual", category="condition")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(plan, buyer_prompt="דירה למכירה, מיזוג אוויר מצוין")

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


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

    # Justified via Phase 0's buyer-prompt signal, so price_signal survives
    # long enough for this explicit-linkage assertion to be meaningful.
    validated = validate_plan(plan, buyer_prompt="Is this a fair price?")

    price_call = next(call for call in validated.tool_calls if call.tool == "price_signal")
    assert price_call.supports_claim_ids == ["claim-0"]


# --- Deterministic regression: "Is the asking price reasonable compared
# to the area?" must keep price_signal, even with no price_comparison
# claim and an LLM-invented buyer_goal (invariant A/B/C from the bug
# report) --------------------------------------------------------------


def test_invariant_a_explicit_comparative_question_keeps_price_signal_without_claim() -> None:
    """A: an explicit comparative-price buyer question + purchase + the
    required price inputs (asking_price, sqm) must keep `price_signal`,
    even when the planner emitted zero claims (no `price_comparison`
    claim at all) - this is the exact real-LLM regression: "Is the asking
    price reasonable compared to the area?" contains no literal fixed
    marker phrase, but is unambiguously a comparative-price question."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=4_000_000,
            sqm=80,
        ),
        buyer_goal="price_validation",
        claims=[],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    validated = validate_plan(
        plan, buyer_prompt="Is the asking price reasonable compared to the area?"
    )

    assert "price_signal" in {call.tool for call in validated.tool_calls}


def test_invariant_b_explicit_comparative_listing_claim_allows_price_signal() -> None:
    """B: an explicit, non-derived comparative-price listing claim (no
    matching buyer-prompt phrasing needed) is independently sufficient to
    allow `price_signal`."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "price_validation",
            "claims": [
                Claim(text="מחיר מתחת למחיר השוק", type="factual", category="price_comparison"),
            ],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        }
    )

    validated = validate_plan(plan)

    assert "price_signal" in {call.tool for call in validated.tool_calls}


def test_invariant_c_invented_buyer_goal_alone_never_keeps_price_signal() -> None:
    """C: a plain listing with no comparative buyer question and no
    comparative listing claim must still reject `price_signal`, even when
    the LLM merely invents `buyer_goal="price_validation"` -
    `buyer_goal` alone is never trusted."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "price_validation",
            "claims": [],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        }
    )

    validated = validate_plan(plan, buyer_prompt="Tell me about this listing.")

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_price_signal_synthesized_when_planner_omits_it_despite_genuine_claim() -> None:
    """Stabilization fix: a `fast_claim_triage` plan for a `purchase` listing
    with `asking_price`/`sqm` present, and a genuine (non-derived)
    `price_comparison` claim, must have `price_signal` (+ `geo_resolver`)
    added back even when the planner's own `tool_calls` never proposed it -
    this is the exact real-LLM regression scenario ("מחיר מצוין מתחת למחיר
    השוק!" with a fast_claim_triage plan omitting price_signal)."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(
                    text="מחיר מצוין מתחת למחיר השוק!",
                    type="factual",
                    category="price_comparison",
                ),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    tool_names = {call.tool for call in validated.tool_calls}
    assert "price_signal" in tool_names
    assert "geo_resolver" in tool_names


def test_price_signal_synthesized_for_explicit_price_validation_question() -> None:
    """The documented README case: an explicit comparative-price buyer
    question (no listing price_comparison claim needed) must also cause
    `price_signal` to be added back when the planner omitted it."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "price_validation",
            "claims": [],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan, buyer_prompt="האם המחיר הגיוני ביחס לאזור?")

    assert "price_signal" in {call.tool for call in validated.tool_calls}


def test_price_signal_not_synthesized_without_comparison_claim_or_intent() -> None:
    """Negative case: the same property facts (purchase, asking_price, sqm)
    but no genuine price_comparison claim and no comparative-price intent
    in the buyer prompt must NOT synthesize `price_signal` - synthesis must
    never fire merely because price + sqm exist."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(text='מחיר 3,000,000 ש"ח', type="factual", category="price"),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan, buyer_prompt="ספר לי על הדירה הזו.")

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_price_signal_not_synthesized_for_rentals() -> None:
    """Negative case: rentals must never have `price_signal` synthesized,
    even with a genuine comparative-price claim present."""
    plan = _plan(listing_type="rental")
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(text="מחיר מתחת למחיר השוק", type="factual", category="price_comparison"),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_price_signal_not_synthesized_when_sqm_missing() -> None:
    """Negative case: a genuine comparative-price claim is not enough on its
    own - `asking_price` and `sqm` must both still be present."""
    plan = _plan(sqm=None)
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(text="מחיר מתחת למחיר השוק", type="factual", category="price_comparison"),
            ],
            "tool_calls": [ToolCall(tool="geo_resolver", arguments={})],
        }
    )

    validated = validate_plan(plan)

    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_price_signal_synthesis_never_overrides_a_planner_proposed_call() -> None:
    """A planner-proposed `price_signal` call is never duplicated by
    synthesis."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "fast_claim_triage",
            "claims": [
                Claim(text="מחיר מתחת למחיר השוק", type="factual", category="price_comparison"),
            ],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={"note": "planner-proposed"}),
            ],
        }
    )

    validated = validate_plan(plan)

    price_calls = [call for call in validated.tool_calls if call.tool == "price_signal"]
    assert len(price_calls) == 1
    assert price_calls[0].arguments == {"note": "planner-proposed"}


def test_price_signal_not_synthesized_when_goal_narrowed_to_nearby_only() -> None:
    """Negative case: a buyer question that explicitly narrows the plan to
    `nearby_services_check` must NOT have `price_signal` synthesized back
    in, even though the listing itself still carries a genuine
    price_comparison claim - the narrowed goal already reflects the
    planner's deliberate choice to omit it this turn."""
    plan = _plan()
    plan = plan.model_copy(
        update={
            "buyer_goal": "nearby_services_check",
            "claims": [
                Claim(
                    text="מחיר מציאה מתחת למחיר השוק",
                    type="factual",
                    category="price_comparison",
                ),
                Claim(
                    text="מעניין אותי רק אם יש פארק קרוב.",
                    type="factual",
                    category="nearby_amenities",
                ),
            ],
            "tool_calls": [
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
            ],
        }
    )

    validated = validate_plan(plan)

    assert "price_signal" not in {call.tool for call in validated.tool_calls}
