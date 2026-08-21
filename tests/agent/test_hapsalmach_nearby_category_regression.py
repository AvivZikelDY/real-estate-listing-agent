"""Deterministic regression for the Hapalmach 21 nearby-category fan-out.

Captures a real-listing failure diagnosed from ONE authorized real LLMod
planner call for the Hapalmach 21 listing, reproduced here WITHOUT any LLM
call from the captured raw planner claims.

Diagnosed deterministic point of loss (now fixed)
-------------------------------------------------
`plan_validator._ensure_nearby_services_for_known_claims` used to synthesize
a `nearby_services` call whenever any retained `nearby_amenities` claim
existed. For this listing the planner labelled two claims `nearby_amenities`
- "חניה רובוטית ..." (robotic parking) and
"קרוב למרכז קהילתי בית ציון ומוסדות חינוך." (near the Bat-Zion community
centre and educational institutions) - but *neither* claim's text resolves
to any frozen `nearby_services` category via the production
`resolve_nearby_categories` (no alias covers "מרכז קהילתי",
"מוסדות חינוך", or robotic parking). The synthesized call therefore carried
empty `arguments={}`, which `build_tool_input` then expanded to the *full*
default category set (all seven categories).

The general fix: synthesis now requires at least one supported frozen
category to resolve from the linked claims' text. When zero supported
categories resolve, NO `nearby_services` call is synthesized (and no
`geo_resolver` is added solely for it), so the tool never fans out to every
category to "support" a claim no supported category actually covers. The
empty-category default-to-all behavior remains reserved for a genuinely
broad, *planner-proposed* "what's nearby" request in `build_tool_input`.

This file asserts the fixed invariant plus general A-F coverage; it changes
no production code.
"""

from __future__ import annotations

from src.listing_agent.agent.plan_validator import validate_plan
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.tool_input_builder import (
    _linked_nearby_claim_text,
    build_tool_input,
    extract_nearby_categories_argument,
    extract_nearby_query_argument,
    resolve_nearby_categories,
)
from src.listing_agent.tools.schemas import GeoResolverOutput, NearbyServicesInput

_HAPSALMACH_ADDRESS = 'הפלמ"ח 21'


def _plan(*, raw_address: str | None = _HAPSALMACH_ADDRESS, claims: list[Claim]) -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=raw_address,
            asking_price=4_290_000.0,
            rooms=4.0,
            sqm=112.0,
        ),
        buyer_goal="fast_claim_triage",
        claims=claims,
        tool_calls=[],
    )


def _hapsalmach_raw_plan() -> TriagePlan:
    return _plan(
        claims=[
            Claim(
                text="חניה רובוטית וחניון משותף עם שער חשמלי",
                type="factual",
                category="nearby_amenities",
            ),
            Claim(
                text="קרוב למרכז קהילתי בית ציון ומוסדות חינוך.",
                type="factual",
                category="nearby_amenities",
            ),
            Claim(text="נכס עם ממד", type="factual", category="condition"),
        ]
    )


def _resolved_categories_for(call: ToolCall, claims: list[Claim]) -> list[str]:
    """Exactly the categories the production pipeline would dispatch `call`
    with from its own arguments + linked claim text (before any empty-list
    fallback to the full default set)."""
    return list(
        resolve_nearby_categories(
            categories=extract_nearby_categories_argument(call.arguments),
            query=extract_nearby_query_argument(call.arguments),
            claim_text=_linked_nearby_claim_text(call, claims),
        )
    )


def test_unsupported_nearby_claims_do_not_synthesize_nearby_services() -> None:
    """The captured Hapalmach 21 invariant: two `nearby_amenities` claims
    (robotic parking + community centre / educational institutions) resolve
    to no supported frozen category, so NO `nearby_services` is synthesized
    and NO `geo_resolver` is added solely for them."""
    validated = validate_plan(_hapsalmach_raw_plan())

    tools = [call.tool for call in validated.tool_calls]
    assert "nearby_services" not in tools
    # geo_resolver was only ever needed as nearby_services' dependency here.
    assert "geo_resolver" not in tools


# --- General A-F coverage ---------------------------------------------------


def test_a_unsupported_nearby_only() -> None:
    """A. A lone unsupported nearby claim (community centre) -> no
    nearby_services, no geo_resolver added solely for it."""
    validated = validate_plan(
        _plan(
            claims=[
                Claim(text="מרכז קהילתי ליד הבית", type="factual", category="nearby_amenities"),
            ]
        )
    )
    tools = [call.tool for call in validated.tool_calls]
    assert "nearby_services" not in tools
    assert "geo_resolver" not in tools


def test_b_generic_ambiguous_education_wording() -> None:
    """B. Generic "מוסדות חינוך" has no frozen alias under the current
    contract -> no supported category -> no nearby_services."""
    validated = validate_plan(
        _plan(
            claims=[
                Claim(text="מוסדות חינוך בקרבת מקום", type="factual", category="nearby_amenities"),
            ]
        )
    )
    tools = [call.tool for call in validated.tool_calls]
    assert "nearby_services" not in tools
    assert "geo_resolver" not in tools


def test_c_unsupported_plus_supported_keeps_only_supported() -> None:
    """C. Unsupported content must not prevent the valid supported category
    from running; only the supported category is dispatched."""
    validated = validate_plan(
        _plan(
            claims=[
                Claim(
                    text="מרכז קהילתי ופארק קרובים", type="factual", category="nearby_amenities"
                ),
            ]
        )
    )
    nearby = [call for call in validated.tool_calls if call.tool == "nearby_services"]
    assert len(nearby) == 1
    assert _resolved_categories_for(nearby[0], validated.claims) == ["park"]


def test_d_several_supported_categories_no_fanout() -> None:
    """D. Several supported categories resolve to exactly those, with no
    fan-out to unrelated categories."""
    validated = validate_plan(
        _plan(
            claims=[
                Claim(
                    text="בתי ספר, גני ילדים ופארק",
                    type="factual",
                    category="nearby_amenities",
                ),
            ]
        )
    )
    nearby = [call for call in validated.tool_calls if call.tool == "nearby_services"]
    assert len(nearby) == 1
    assert _resolved_categories_for(nearby[0], validated.claims) == ["school", "daycare", "park"]


def test_e_robotic_parking_labelled_nearby_produces_no_tool() -> None:
    """E. Even if the planner mislabels in-building robotic parking as
    `nearby_amenities`, it resolves to no category -> no nearby tool. Guards
    against planner classification variance."""
    validated = validate_plan(
        _plan(
            claims=[
                Claim(text="חניה רובוטית בבניין", type="factual", category="nearby_amenities"),
            ]
        )
    )
    tools = [call.tool for call in validated.tool_calls]
    assert "nearby_services" not in tools
    assert "geo_resolver" not in tools


def test_f_planner_proposed_broad_nearby_still_defaults_to_all_categories() -> None:
    """F. The intentional broad "what's nearby" feature is preserved: a
    *planner-proposed* `nearby_services` call with no categories still falls
    back to the full default category set (via `build_tool_input`). This is
    distinct from the validator synthesizing a call for an unsupported
    claim, and must not be removed to fix the fan-out regression."""
    plan = _plan(claims=[]).model_copy(
        update={"tool_calls": [ToolCall(tool="nearby_services", arguments={})]}
    )

    validated = validate_plan(plan)

    nearby = [call for call in validated.tool_calls if call.tool == "nearby_services"]
    assert len(nearby) == 1
    # No categories resolve from arguments/claims, so build_tool_input widens
    # to the full default set for this deliberately broad request.
    assert _resolved_categories_for(nearby[0], validated.claims) == []
    geo_output = GeoResolverOutput(
        outcome="resolved",
        normalized_address='הפלמ"ח 21, תל אביב',
        lat=32.07,
        lon=34.78,
        resolution_quality="exact",
        source="mock",
    )
    built = build_tool_input(nearby[0], validated.property_details, geo_output, validated.claims)
    default_categories = NearbyServicesInput.model_fields["categories"].default_factory()  # type: ignore[misc]
    assert built.categories == default_categories
