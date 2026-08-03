"""Centralized scenario definitions for `scripts/inspect_listing_pipeline.py`.

Each `Scenario` pairs a raw listing (+ optional user question) with:

* a hand-built `TriagePlan` fixture, used only in `--fake-llm` mode (never a
  substitute for real planner behavior - see the real-LLM verification
  section of the corresponding completion report);
* the *expected* high-level behavior (goal, relevant categories, tools
  that should/should not run, answerability) used by the offline test
  suite to assert real production behavior stays correct.

These fixtures are diagnostic-only test data - never a second
implementation of `triage_planner`, `plan_validator`, or any other
production module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan


@dataclass(frozen=True)
class Scenario:
    id: int
    title: str
    listing: str
    question: str | None
    expected_goal: str
    expected_categories: tuple[str, ...]
    expected_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    expected_answerability: str
    plan: TriagePlan
    invariants: tuple[str, ...] = field(default_factory=tuple)


SCENARIOS: dict[int, Scenario] = {
    1: Scenario(
        id=1,
        title="Full listing, no question",
        listing=(
            'דירת 4 חדרים מהממת ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח.\n'
            "קרובה לבתי ספר, גנים ופארקים.\nמחיר מציאה מתחת למחיר השוק."
        ),
        question=None,
        expected_goal="general_listing_triage",
        expected_categories=(
            "rooms",
            "condition",
            "address",
            "size_sqm",
            "price",
            "nearby_amenities",
            "price_comparison",
        ),
        expected_tools=("geo_resolver", "nearby_services", "price_signal"),
        forbidden_tools=(),
        expected_answerability="not_applicable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=4_700_000,
                sqm=100,
                rooms=4,
            ),
            buyer_goal="price_validation",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text='100 מ"ר', type="factual", category="size_sqm"),
                Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text="דירה מהממת", type="subjective", category="condition"),
                Claim(
                    text="קרובה לבתי ספר, גנים ופארקים",
                    type="factual",
                    category="nearby_amenities",
                ),
                Claim(
                    text="מחיר מציאה מתחת למחיר השוק",
                    type="factual",
                    category="price_comparison",
                ),
            ],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
                ToolCall(tool="nearby_services", arguments={"categories": ["kindergarten"]}),
                ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        invariants=(
            "subjective wording is not verified",
            "nearby evidence is linked to the nearby claim",
            "price evidence is linked to the price-comparison claim",
            "price and area remain listing inputs",
            "no unsupported definitive conclusion",
        ),
    ),
    2: Scenario(
        id=2,
        title="Family suitability question",
        listing=(
            'דירת 4 חדרים ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, קרובה לבתי ספר, גנים ופארקים.'
        ),
        question="האם הדירה מתאימה למשפחה עם שני ילדים קטנים?",
        expected_goal="family_suitability",
        expected_categories=("rooms", "address", "size_sqm", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services"),
        forbidden_tools=("price_signal",),
        expected_answerability="partially_verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=None,
                sqm=100,
                rooms=4,
            ),
            buyer_goal="family_suitability",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text='100 מ"ר', type="factual", category="size_sqm"),
                Claim(
                    text="קרובה לבתי ספר, גנים ופארקים",
                    type="factual",
                    category="nearby_amenities",
                ),
                Claim(
                    text="האם הדירה מתאימה למשפחה עם שני ילדים קטנים?",
                    type="factual",
                    category="family_suitability",
                ),
            ],
            missing_information=["asking_price"],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(
                    tool="nearby_services",
                    arguments={"categories": ["school", "kindergarten", "park"]},
                ),
            ],
        ),
        invariants=(
            "proximity does not prove complete family suitability",
            "school quality is not checked",
            "registration zones are not checked",
            "noise, safety, elevator, and room layout are not checked",
        ),
    ),
    3: Scenario(
        id=3,
        title="Direct market-price question",
        listing=('דירת 4 חדרים ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח.'),
        question="האם המחיר נמוך ממחיר השוק?",
        expected_goal="price_validation",
        expected_categories=("rooms", "address", "size_sqm", "price", "price_comparison"),
        expected_tools=("geo_resolver", "price_signal"),
        forbidden_tools=("nearby_services",),
        expected_answerability="verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=4_700_000,
                sqm=100,
                rooms=4,
            ),
            buyer_goal="price_validation",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text='100 מ"ר', type="factual", category="size_sqm"),
                Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
                Claim(
                    text="האם המחיר נמוך ממחיר השוק?",
                    type="factual",
                    category="price_comparison",
                ),
            ],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        invariants=(
            "price per square meter is not a full appraisal",
            "missing floor, condition, parking, and building attributes remain limitations",
        ),
    ),
    4: Scenario(
        id=4,
        title="Question-derived nearby claim",
        listing="דירת 3 חדרים ברחוב אבן גבירול 80 בתל אביב.",
        question="האם יש פארק קרוב לדירה?",
        expected_goal="nearby_services_check",
        expected_categories=("rooms", "address", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services"),
        forbidden_tools=("price_signal",),
        expected_answerability="verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="אבן גבירול 80, תל אביב",
                asking_price=None,
                sqm=None,
                rooms=3,
            ),
            buyer_goal="nearby_services_check",
            claims=[
                Claim(text="דירת 3 חדרים", type="factual", category="rooms"),
                Claim(text="ברחוב אבן גבירול 80 בתל אביב", type="factual", category="address"),
                Claim(
                    text="האם יש פארק קרוב לדירה?",
                    type="factual",
                    category="nearby_amenities",
                ),
            ],
            missing_information=["asking_price", "sqm"],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "אבן גבירול 80, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
            ],
        ),
        invariants=(
            "claim source is user_question",
            "evidence purpose is answering the question",
            "no price tool",
        ),
    ),
    5: Scenario(
        id=5,
        title="Walking-time question beyond tool capability",
        listing="דירת 3 חדרים ברחוב דיזנגוף 100 בתל אביב.\nקרובה לבתי ספר.",
        question="האם יש בית ספר במרחק של עד 10 דקות הליכה?",
        expected_goal="nearby_services_check",
        expected_categories=("rooms", "address", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services"),
        forbidden_tools=("price_signal",),
        expected_answerability="partially_verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=None,
                sqm=None,
                rooms=3,
            ),
            buyer_goal="nearby_services_check",
            claims=[
                Claim(text="דירת 3 חדרים", type="factual", category="rooms"),
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text="קרובה לבתי ספר", type="factual", category="nearby_amenities"),
                Claim(
                    text="האם יש בית ספר במרחק של עד 10 דקות הליכה?",
                    type="factual",
                    category="nearby_amenities",
                ),
            ],
            missing_information=["asking_price", "sqm"],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
            ],
        ),
        invariants=(
            "no walking route checked",
            "distance in meters is not walking duration",
            "exact ten-minute claim remains unverified",
        ),
    ),
    6: Scenario(
        id=6,
        title="School quality unsupported",
        listing="דירה ברחוב דיזנגוף 100 בתל אביב, קרובה לבתי ספר.",
        question="האם בתי הספר באזור טובים?",
        expected_goal="nearby_services_check",
        expected_categories=("address", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services"),
        forbidden_tools=("price_signal",),
        expected_answerability="partially_verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=None,
                sqm=None,
                rooms=None,
            ),
            buyer_goal="nearby_services_check",
            claims=[
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text="קרובה לבתי ספר", type="factual", category="nearby_amenities"),
                Claim(
                    text="האם בתי הספר באזור טובים?",
                    type="factual",
                    category="nearby_amenities",
                ),
            ],
            missing_information=["asking_price", "sqm", "rooms"],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
            ],
        ),
        invariants=(
            "nearby schools may be listed",
            "no school-quality conclusion",
            "quality remains unanswered",
            "no invented ratings",
        ),
    ),
    7: Scenario(
        id=7,
        title="Unsupported building attributes",
        listing="דירת 4 חדרים במרכז תל אביב.",
        question="האם יש בבניין מעלית וחניה פרטית?",
        expected_goal="general_question",
        expected_categories=("rooms", "address"),
        expected_tools=(),
        forbidden_tools=("geo_resolver", "nearby_services", "price_signal"),
        expected_answerability="unsupported",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="מרכז תל אביב",
                asking_price=None,
                sqm=None,
                rooms=4,
            ),
            buyer_goal="general_question",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text="במרכז תל אביב", type="factual", category="address"),
                Claim(
                    text="האם יש בבניין מעלית וחניה פרטית?",
                    type="factual",
                    category=None,
                ),
            ],
            missing_information=["asking_price", "sqm", "elevator", "parking"],
            tool_calls=[],
        ),
        invariants=(
            "no external evidence",
            "explain that the information is absent",
            "recommend asking the seller or broker",
        ),
    ),
    8: Scenario(
        id=8,
        title="Price question with missing information",
        listing="דירת 4 חדרים בצפון תל אביב.\nמחיר מציאה.",
        question="האם המחיר באמת משתלם?",
        expected_goal="price_validation",
        expected_categories=("rooms", "address", "price_comparison"),
        expected_tools=(),
        forbidden_tools=("geo_resolver", "nearby_services", "price_signal"),
        expected_answerability="missing_information",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="צפון תל אביב",
                asking_price=None,
                sqm=None,
                rooms=4,
            ),
            buyer_goal="price_validation",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text="בצפון תל אביב", type="factual", category="address"),
                Claim(text="מחיר מציאה", type="factual", category="price_comparison"),
                Claim(
                    text="האם המחיר באמת משתלם?",
                    type="factual",
                    category="price_comparison",
                ),
            ],
            missing_information=["asking_price", "sqm"],
            # Deliberately proposes only `price_signal` (dropped by
            # `plan_validator` for missing asking_price/sqm) - never
            # `geo_resolver` directly, since no *other* location-dependent
            # tool is selected for this question, so `geo_resolver` is
            # never auto-added for a broad, unusable area.
            tool_calls=[
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        invariants=(
            "insufficient information",
            "missing fields are explicit",
            "follow-up questions request price, area, and precise address",
        ),
    ),
    9: Scenario(
        id=9,
        title="User question narrows the tool plan",
        listing=(
            'דירת 4 חדרים בדיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח.\n'
            "קרובה לבתי ספר, גנים ופארקים.\nמחיר מציאה מתחת למחיר השוק."
        ),
        question="מעניין אותי רק אם יש פארק קרוב.",
        expected_goal="nearby_services_check",
        expected_categories=("rooms", "size_sqm", "price", "address", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services"),
        forbidden_tools=("price_signal",),
        expected_answerability="verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=4_700_000,
                sqm=100,
                rooms=4,
            ),
            buyer_goal="nearby_services_check",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text='100 מ"ר', type="factual", category="size_sqm"),
                Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
                Claim(text="בדיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(
                    text="קרובה לבתי ספר, גנים ופארקים",
                    type="factual",
                    category="nearby_amenities",
                ),
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
            # The question narrows the plan: only `park` is proposed (the
            # planner itself, given the narrowed goal, never proposes
            # `price_signal` here) - `plan_validator` never needs to drop
            # anything extra for this scenario.
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
            ],
        ),
        invariants=(
            "focuses on park evidence",
            "price claim marked as not checked because it is irrelevant to the question",
        ),
    ),
    10: Scenario(
        id=10,
        title="General opinion question",
        listing=(
            'דירת 4 חדרים ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח, קרובה לפארקים.'
        ),
        question="מה דעתך על הדירה?",
        expected_goal="general_question",
        expected_categories=("rooms", "size_sqm", "price", "address", "nearby_amenities"),
        expected_tools=("geo_resolver", "nearby_services", "price_signal"),
        forbidden_tools=(),
        expected_answerability="verifiable",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="דיזנגוף 100, תל אביב",
                asking_price=4_700_000,
                sqm=100,
                rooms=4,
            ),
            buyer_goal="general_question",
            claims=[
                Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
                Claim(text='100 מ"ר', type="factual", category="size_sqm"),
                Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
                Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
                Claim(text="קרובה לפארקים", type="factual", category="nearby_amenities"),
            ],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
                ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        invariants=(
            "objective strengths",
            "unclear aspects",
            "limitations",
            "suggested follow-up questions",
            "no definitive purchase recommendation",
        ),
    ),
}


__all__ = ["Scenario", "SCENARIOS"]
