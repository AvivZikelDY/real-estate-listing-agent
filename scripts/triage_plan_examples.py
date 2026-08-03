"""Fixed example listings + hand-built `TriagePlan` fixtures for
`scripts/inspect_triage_plan.py --fake-llm`.

`FakeLLMClient` only replays pre-configured responses (see
`src/listing_agent/llm/client/fake.py`) - it cannot turn arbitrary listing
text into a plan the way a real LLM does. So `--fake-llm` mode can only
work for this fixed, small set of example listings, each paired with a
hand-built `TriagePlan` that mirrors what `triage_planner` would plausibly
produce for it. This mirrors the same pattern already used for
`GET /api/agent_info`'s illustrative examples
(`src/listing_agent/api/agent_info_examples.py`).

Three examples, matching the three cases requested for the diagnostic
runner:

1. Multiple verifiable claims (one subjective, two factual - one of which
   is deliberately duplicated across two proposed `nearby_services` calls,
   to demonstrate `plan_validator`'s deduplication-by-tool-name).
2. Missing information (`asking_price`/`sqm` absent -> `price_signal` is
   dropped by `plan_validator`).
3. Subjective-only listing (no address, no tool calls at all).
"""

from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan

EXAMPLE_LISTINGS: dict[int, str] = {
    1: (
        "דירת 4 חדרים מהממת ברחוב דיזנגוף 100 בתל אביב.\n"
        '100 מ"ר, מחיר 4,700,000 ש"ח.\n'
        "קרובה לבתי ספר, גנים ופארקים.\n"
        "מחיר מציאה מתחת למחיר השוק."
    ),
    2: ("דירת 4 חדרים בצפון תל אביב.\nמחיר מציאה וקרובה לבתי ספר."),
    3: ("דירה נדירה, מהממת ומושלמת למשפחה בלב תל אביב."),
}

EXAMPLE_PLANS: dict[int, TriagePlan] = {
    1: TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="דיזנגוף 100, תל אביב",
            asking_price=4_700_000,
            sqm=100,
            rooms=4,
        ),
        buyer_goal="price_validation",
        claims=[
            # Listing facts (canonical "listing_fact" categories) - inputs
            # to a tool, never independent claims to check.
            Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
            Claim(text='100 מ"ר', type="factual", category="size_sqm"),
            Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
            Claim(text="ברחוב דיזנגוף 100 בתל אביב", type="factual", category="address"),
            # Subjective marketing wording - never sent to a tool.
            Claim(text="דירה מהממת", type="subjective", category="condition"),
            # Verifiable claims (canonical categories matching real
            # triage_planner output - see the corrected `claim_categories`
            # module).
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
            # Three proposed `nearby_services` calls (school/kindergarten/
            # park) for the same claim - `plan_validator` dedupes these
            # down to one merged call (categories unioned), which the
            # diagnostic script's final tool plan flags as
            # `merged_from_duplicates`.
            ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
            ToolCall(tool="nearby_services", arguments={"categories": ["kindergarten"]}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    ),
    2: TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="צפון תל אביב",
            asking_price=None,
            sqm=None,
            rooms=4,
        ),
        buyer_goal="price_validation",
        claims=[
            Claim(text="מחיר מציאה", type="factual", category="price_comparison"),
            Claim(text="קרובה לבתי ספר", type="factual", category="nearby_amenities"),
        ],
        missing_information=["asking_price", "sqm"],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": "צפון תל אביב"}),
            ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
            # Proposed, but will be dropped by `plan_validator`: asking_price
            # and sqm are both missing, and `price_signal` requires both.
            ToolCall(tool="price_signal", arguments={}),
        ],
    ),
    3: TriagePlan(
        property_details=PropertyDetails(listing_type="purchase", raw_address=None),
        buyer_goal="general_question",
        claims=[
            Claim(text="דירה נדירה", type="subjective", category="condition"),
            Claim(
                text="מהממת ומושלמת למשפחה",
                type="subjective",
                category="condition",
            ),
        ],
        missing_information=["raw_address", "asking_price", "sqm"],
        tool_calls=[],
    ),
}

__all__ = ["EXAMPLE_LISTINGS", "EXAMPLE_PLANS"]
