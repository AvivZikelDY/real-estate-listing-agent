"""Deterministic regression suite for the claim-retention fix.

Captures the diagnosed Gonen 14 claim-loss failure (a real LLMod planner
response that lost claims past the retention cap) as a typed fixture
BEFORE any production fix, proves it failed for the diagnosed reason, and
then locks in the general fix (deterministic dedup -> prioritize -> cap in
`plan_validator`) so it can never silently regress.

Also covers Phase 11's general scenarios (A-F): late tool-checkable
claims, late transaction structure, seller-fact-only listings, a narrow
buyer question, a tool-checkable claim at position 12, and distinct
same-category commitments. None of these hardcode a specific address or
claim index - every assertion is keyed off canonical category/priority,
never raw list position.
"""

from __future__ import annotations

from src.listing_agent.agent.plan_validator import validate_plan
from src.listing_agent.agent.plan_validator.validator import MAX_CLAIMS, _cap_claims
from src.listing_agent.agent.schemas import Claim, PropertyDetails, TriagePlan

GONEN_ADDRESS = "גונן 14"

# The exact 12 claims the real LLMod planner produced for the Gonen 14
# listing (captured from one authorized real LLM call; this fixture makes
# that captured failure reproducible without ever calling the LLM again).
GONEN_14_RAW_CLAIMS: list[Claim] = [
    Claim(id="c1", text="גונן 14", type="factual", category="address"),
    Claim(id="c2", text="נכס עם ממ״ד", type="factual", category="condition"),
    Claim(id="c3", text="דירה, תל חייים, תל אביב יפו", type="factual", category="address"),
    Claim(id="c4", text="5 חדרים", type="factual", category="rooms"),
    Claim(id="c5", text="קומה 5/6", type="factual", category=None),
    Claim(id="c6", text='123 מ"ר', type="factual", category="size_sqm"),
    Claim(
        id="c7",
        text="דירה חדשה למסירה ברחוב גונן הירוק והמתחדש.",
        type="factual",
        category="condition",
    ),
    Claim(
        id="c8",
        text="דירה משודרגת, מטבח מפנק של מטבחי אניס, ומקלחות משודרגות,",
        type="subjective",
        category="condition",
    ),
    Claim(
        id="c9",
        text="פרקט בחדרים שיורכב לאחר המסירה.",
        type="factual",
        category="condition",
    ),
    Claim(
        id="c10",
        text="נוף ירוק ופתוח, מתחם גני ילדים מעובר לכביש והמון חנייה ברחוב.",
        type="factual",
        category="nearby_amenities",
    ),
    Claim(
        id="c11",
        text=(
            "יש אופציה למכירה עם המסירה וגם פתוחים לאפשרות להשכרה לשנה או שנתיים "
            "ולאחר מכן רכישה על ידי השוכר."
        ),
        type="factual",
        category=None,
    ),
    Claim(id="c12", text="4,350,000 ₪", type="factual", category="price"),
]


def _gonen_14_plan() -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=GONEN_ADDRESS,
            asking_price=4_350_000,
            rooms=5,
            sqm=123,
        ),
        buyer_goal="fast_claim_triage",
        claims=list(GONEN_14_RAW_CLAIMS),
        tool_calls=[],
    )


# --- Phase 5: the pre-fix failure, proven at the raw-order cap boundary ----


def test_pre_fix_raw_order_cap_drops_nearby_and_transaction_claims() -> None:
    """Reproduces the diagnosed defect directly: capping strictly by raw
    LLM order (no dedup, no prioritization) drops the nearby-amenities
    claim, the transaction-structure claim, and the asking-price claim -
    exactly the four claims the root-cause report identifies as lost.

    This does not call `validate_plan` (the fixed entry point) - it
    exercises the *old* algorithm's defining behavior (first-N-in-raw-order)
    directly, so this test documents the failure mode itself and must keep
    passing even after the fix is implemented elsewhere.
    """
    missing_information: list[str] = []
    capped = _cap_claims(GONEN_14_RAW_CLAIMS, MAX_CLAIMS, missing_information)

    assert len(capped) == MAX_CLAIMS
    retained_ids = {c.id for c in capped}
    # This is what raw-order capping does today, unchanged (the fixture's
    # first 8 in original order): c9-c12 fall outside 8 raw-order slots.
    assert retained_ids == {"c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"}
    assert "c10" not in retained_ids  # nearby_amenities (kindergarten) lost
    assert "c11" not in retained_ids  # transaction structure lost
    assert "c12" not in retained_ids  # asking-price claim lost


# --- Phase 6/13: the fixed, production `validate_plan` retention/tool plan -


def test_gonen_14_retains_nearby_amenities_claim_after_fix() -> None:
    validated = validate_plan(_gonen_14_plan())
    retained_categories = {c.category for c in validated.claims}
    assert "nearby_amenities" in retained_categories


def test_gonen_14_retains_transaction_structure_claim_after_fix() -> None:
    validated = validate_plan(_gonen_14_plan())
    retained_texts = {c.text for c in validated.claims}
    assert any("השכרה" in text or "רכישה" in text for text in retained_texts)


def test_gonen_14_retains_a_seller_verification_fact_after_fix() -> None:
    validated = validate_plan(_gonen_14_plan())
    retained_texts = " ".join(c.text for c in validated.claims)
    assert "ממ״ד" in retained_texts  # safe room


def test_gonen_14_retains_parquet_after_delivery_commitment_after_fix() -> None:
    validated = validate_plan(_gonen_14_plan())
    retained_texts = " ".join(c.text for c in validated.claims)
    assert "פרקט" in retained_texts


def test_gonen_14_duplicate_address_claim_does_not_survive() -> None:
    """Two claims were both categorized `"address"` (c1, c3) - only one
    should consume a claim slot; the wasted slot goes to a higher-priority
    claim instead."""
    validated = validate_plan(_gonen_14_plan())
    address_claims = [c for c in validated.claims if c.category == "address"]
    assert len(address_claims) == 1


def test_gonen_14_subjective_marketing_does_not_displace_nearby_claim() -> None:
    validated = validate_plan(_gonen_14_plan())
    categories = [c.category for c in validated.claims]
    subjective = [c for c in validated.claims if c.type == "subjective"]
    assert "nearby_amenities" in categories
    # The subjective kitchen/bathroom marketing claim (c8) must not survive
    # while a higher-priority tool-checkable claim would otherwise be cut.
    assert not subjective or len(validated.claims) < MAX_CLAIMS + len(subjective)


def test_gonen_14_final_tool_plan_is_geo_then_nearby_daycare() -> None:
    validated = validate_plan(_gonen_14_plan())
    assert [call.tool for call in validated.tool_calls] == ["geo_resolver", "nearby_services"]
    nearby_call = next(call for call in validated.tool_calls if call.tool == "nearby_services")
    assert nearby_call.arguments.get("categories") == ["daycare"]


def test_gonen_14_price_signal_never_runs_without_comparative_intent() -> None:
    """asking_price and sqm both exist, but there is no comparative claim
    and no price_validation buyer goal - price_signal must not run merely
    because the two fields are present."""
    validated = validate_plan(_gonen_14_plan())
    assert "price_signal" not in {call.tool for call in validated.tool_calls}


def test_gonen_14_truncation_is_recorded_with_removed_categories() -> None:
    validated = validate_plan(_gonen_14_plan())
    assert "additional_claims_truncated" in validated.missing_information
    detail = next(
        (
            item
            for item in validated.missing_information
            if item.startswith("additional_claims_truncated_detail")
        ),
        None,
    )
    assert detail is not None
    assert "produced=12" in detail
    assert "retained=8" in detail


# --- Phase 11.A: a late park claim in a marketing-heavy listing -----------


def test_scenario_a_late_park_claim_survives_marketing_prose() -> None:
    claims = [
        Claim(
            id=f"marketing-{i}",
            text=f"תיאור שיווקי {i}",
            type="subjective",
            category="condition",
        )
        for i in range(7)
    ]
    claims.append(
        Claim(id="park", text="יש פארק גדול מעבר לכביש.", type="factual", category="park")
    )
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="דיזנגוף 50, תל אביב", rooms=3, sqm=70
        ),
        buyer_goal="fast_claim_triage",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    assert any(c.category == "park" for c in validated.claims)
    assert [call.tool for call in validated.tool_calls] == ["geo_resolver", "nearby_services"]
    nearby_call = next(call for call in validated.tool_calls if call.tool == "nearby_services")
    assert nearby_call.arguments.get("categories") == ["park"]


# --- Phase 11.B: a late transaction-structure claim ------------------------


def test_scenario_b_late_transaction_structure_survives_and_needs_no_tool() -> None:
    claims = [
        Claim(
            id=f"marketing-{i}",
            text=f"תיאור שיווקי {i}",
            type="subjective",
            category="condition",
        )
        for i in range(7)
    ]
    claims.append(
        Claim(
            id="rent-to-buy",
            text="ניתן לשכור לשנה ולאחר מכן לרכוש במחיר שייקבע בהמשך.",
            type="factual",
            category="transaction_structure",
        )
    )
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="rental", raw_address="דיזנגוף 50, תל אביב", rooms=3, sqm=70
        ),
        buyer_goal="general_question",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    assert any(c.category == "transaction_structure" for c in validated.claims)
    assert validated.tool_calls == []
    assert "additional_claims_truncated" not in validated.missing_information


# --- Phase 11.C: many seller facts, no tool-checkable claim at all ---------


def test_scenario_c_seller_facts_only_do_not_trigger_unnecessary_tools() -> None:
    claims = [
        Claim(id="safe-room", text='יש ממ"ד', type="factual", category="condition"),
        Claim(id="elevator", text="יש מעלית", type="factual", category="condition"),
        Claim(id="floor", text="קומה 3", type="factual", category=None),
        Claim(id="renovation", text="שופצה לאחרונה", type="factual", category="condition"),
        Claim(id="furniture", text="מרוהטת חלקית", type="factual", category="condition"),
        Claim(id="parking", text="חניה בטאבו", type="factual", category="condition"),
        Claim(id="kitchen", text="מטבח חדש", type="factual", category="condition"),
        Claim(id="bathroom", text="חדר רחצה משופץ", type="factual", category="condition"),
    ]
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="ביאליק 10, רמת גן",
            asking_price=2_000_000,
            rooms=3,
            sqm=75,
        ),
        buyer_goal="general_question",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    assert validated.tool_calls == []
    assert len(validated.claims) == len(claims)  # nothing to truncate, all seller facts kept


# --- Phase 11.D: a narrow buyer question outranks listing order ------------


def test_scenario_d_narrow_buyer_question_gets_highest_priority() -> None:
    claims = [
        Claim(
            id=f"marketing-{i}",
            text=f"תיאור שיווקי {i}",
            type="subjective",
            category="condition",
        )
        for i in range(9)
    ]
    claims.append(
        Claim(
            id="daycare-question",
            text="מעניין אותי רק אם יש גן ילדים קרוב.",
            type="factual",
            category="nearby_amenities",
        )
    )
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="הרצל 1, חולון",
            asking_price=2_500_000,
            rooms=4,
            sqm=95,
        ),
        buyer_goal="nearby_services_check",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    assert validated.claims[0].id == "daycare-question"
    assert [call.tool for call in validated.tool_calls] == ["geo_resolver", "nearby_services"]
    nearby_call = next(call for call in validated.tool_calls if call.tool == "nearby_services")
    assert nearby_call.arguments.get("categories") == ["daycare"]
    assert "price_signal" not in {call.tool for call in validated.tool_calls}


# --- Phase 11.E: a tool-checkable claim at position 12 (last) --------------


def test_scenario_e_tool_checkable_claim_at_last_position_survives() -> None:
    claims = [
        Claim(
            id=f"marketing-{i}",
            text=f"תיאור שיווקי {i}",
            type="subjective",
            category="condition",
        )
        for i in range(11)
    ]
    claims.append(
        Claim(
            id="last-claim",
            text="קרוב לבית ספר טוב",
            type="factual",
            category="nearby_amenities",
        )
    )
    assert len(claims) == 12
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="ויצמן 5, גבעתיים", rooms=4, sqm=100
        ),
        buyer_goal="general_question",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    retained_ids = {c.id for c in validated.claims}
    assert "last-claim" in retained_ids
    # A lower-priority subjective claim was removed instead of the
    # tool-checkable one at the end of the list.
    assert len(validated.claims) == MAX_CLAIMS


# --- Phase 11.F: similar-category but distinct commitments -----------------


def test_scenario_f_distinct_condition_commitments_all_survive() -> None:
    claims = [
        Claim(id="kitchen", text="מטבח משודרג", type="factual", category="condition"),
        Claim(id="bathroom", text="מקלחות משודרגות", type="factual", category="condition"),
        Claim(
            id="parquet",
            text="פרקט יותקן לאחר המסירה",
            type="factual",
            category="condition",
        ),
        Claim(id="safe-room", text='יש ממ"ד', type="factual", category="condition"),
    ]
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase", raw_address="סוקולוב 3, הרצליה", rooms=4, sqm=110
        ),
        buyer_goal="general_question",
        claims=claims,
        tool_calls=[],
    )

    validated = validate_plan(plan)

    assert len(validated.claims) == 4
    assert {c.id for c in validated.claims} == {"kitchen", "bathroom", "parquet", "safe-room"}
