"""Deterministic FakeLLMClient fixtures for `triage_planner` test cases.

Each fixture is a (prompt, expected_plan) pair covering one of the required
Phase 4 scenarios. Tests build a `FakeLLMClient`, configure it with the
expected plan for `triage_planner`, run the module, and assert the result.
"""

from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan

# 1. Purchase listing, family question.
PURCHASE_FAMILY_PROMPT = (
    "We're a family with two kids looking to buy this 4-room apartment at "
    "12 Ben Yehuda St, Tel Aviv, 90 sqm, asking price 3,200,000 ILS. Is this "
    "a good fit for a family, and are there schools and parks nearby?"
)
PURCHASE_FAMILY_PLAN = TriagePlan(
    property_details=PropertyDetails(
        listing_type="purchase",
        raw_address="12 Ben Yehuda St, Tel Aviv",
        asking_price=3_200_000,
        rooms=4,
        sqm=90,
    ),
    buyer_goal="family_suitability",
    claims=[
        Claim(text="4-room apartment, 90 sqm", type="factual", category="size"),
    ],
    missing_information=[],
    tool_calls=[
        ToolCall(tool="geo_resolver", arguments={"address": "12 Ben Yehuda St, Tel Aviv"}),
        ToolCall(tool="nearby_services", arguments={}),
    ],
)

# 2. Purchase listing, price question.
PURCHASE_PRICE_PROMPT = (
    "Buying this 3-room, 70 sqm apartment on HaYarkon St for 2,850,000 ILS "
    "- is this price reasonable for the area?"
)
PURCHASE_PRICE_PLAN = TriagePlan(
    property_details=PropertyDetails(
        listing_type="purchase",
        raw_address="HaYarkon St, Tel Aviv",
        asking_price=2_850_000,
        rooms=3,
        sqm=70,
    ),
    buyer_goal="price_validation",
    claims=[],
    missing_information=[],
    tool_calls=[
        ToolCall(tool="geo_resolver", arguments={"address": "HaYarkon St, Tel Aviv"}),
        ToolCall(tool="price_signal", arguments={}),
    ],
)

# 3. Purchase listing missing sqm.
PURCHASE_MISSING_SQM_PROMPT = (
    "Looking at a 3-room apartment for sale at 5 Dizengoff St for "
    "2,500,000 ILS, but the listing doesn't mention the size. Is the price "
    "fair?"
)
PURCHASE_MISSING_SQM_PLAN = TriagePlan(
    property_details=PropertyDetails(
        listing_type="purchase",
        raw_address="5 Dizengoff St, Tel Aviv",
        asking_price=2_500_000,
        rooms=3,
        sqm=None,
    ),
    buyer_goal="price_validation",
    claims=[],
    missing_information=["sqm"],
    tool_calls=[
        ToolCall(tool="geo_resolver", arguments={"address": "5 Dizengoff St, Tel Aviv"}),
    ],
)

# 4. Rental listing with price question.
RENTAL_PRICE_PROMPT = (
    "This 2-room rental apartment on Allenby St is 55 sqm and listed at "
    "6,500 ILS/month. Is that a fair rental price?"
)
RENTAL_PRICE_PLAN = TriagePlan(
    property_details=PropertyDetails(
        listing_type="rental",
        raw_address="Allenby St, Tel Aviv",
        asking_price=6_500,
        rooms=2,
        sqm=55,
    ),
    buyer_goal="price_validation",
    claims=[],
    missing_information=[],
    tool_calls=[
        ToolCall(tool="geo_resolver", arguments={"address": "Allenby St, Tel Aviv"}),
    ],
)

# 5. Listing containing only subjective claims.
SUBJECTIVE_ONLY_PROMPT = (
    "This stunning, beautiful, charming apartment is absolutely gorgeous "
    "and full of character."
)
SUBJECTIVE_ONLY_PLAN = TriagePlan(
    property_details=PropertyDetails(
        listing_type="unknown",
        raw_address=None,
        asking_price=None,
        rooms=None,
        sqm=None,
    ),
    buyer_goal="general_question",
    claims=[
        Claim(
            text="stunning, beautiful, charming, gorgeous, full of character",
            type="subjective",
            category="aesthetic",
        ),
    ],
    missing_information=["raw_address", "asking_price", "rooms", "sqm"],
    tool_calls=[],
)

ALL_CASES = [
    ("purchase_family", PURCHASE_FAMILY_PROMPT, PURCHASE_FAMILY_PLAN),
    ("purchase_price", PURCHASE_PRICE_PROMPT, PURCHASE_PRICE_PLAN),
    ("purchase_missing_sqm", PURCHASE_MISSING_SQM_PROMPT, PURCHASE_MISSING_SQM_PLAN),
    ("rental_price", RENTAL_PRICE_PROMPT, RENTAL_PRICE_PLAN),
    ("subjective_only", SUBJECTIVE_ONLY_PROMPT, SUBJECTIVE_ONLY_PLAN),
]
