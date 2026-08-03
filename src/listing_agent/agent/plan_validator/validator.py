"""The deterministic `plan_validator` module.

Validates and normalizes the `TriagePlan` produced by `triage_planner`
before any tool is executed. Pure/deterministic: no LLM or network calls.
"""

from src.listing_agent.agent.claim_categories import normalize_category
from src.listing_agent.agent.schemas import (
    Claim,
    ClaimSource,
    PropertyDetails,
    ToolCall,
    ToolName,
    TriagePlan,
)
from src.listing_agent.agent.tool_input_builder import resolve_nearby_categories

ALLOWED_TOOLS: frozenset[str] = frozenset({"geo_resolver", "nearby_services", "price_signal"})
MAX_TOOL_CALLS = 3
MAX_CLAIMS = 8
LOCATION_DEPENDENT_TOOLS: frozenset[str] = frozenset({"nearby_services", "price_signal"})


class PlanValidationError(Exception):
    """Raised when a plan cannot be validated (e.g. an unknown tool name)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "Plan validation failed.")


def _dedupe_by_tool(tool_calls: list[ToolCall]) -> list[ToolCall]:
    """Remove duplicate calls to the same tool, keeping the first occurrence.

    For `nearby_services` specifically, merges every duplicate call's
    `arguments["categories"]` into the kept call (order-preserving,
    deduplicated) instead of silently discarding the extra categories -
    two separately-proposed `nearby_services` calls (e.g. one for
    "schools", one for "parks") become one call that still checks both.
    """
    seen: set[str] = set()
    deduped: list[ToolCall] = []
    merged_categories_by_tool: dict[str, list[str]] = {}
    for call in tool_calls:
        categories = call.arguments.get("categories")
        if call.tool == "nearby_services" and isinstance(categories, list):
            bucket = merged_categories_by_tool.setdefault(call.tool, [])
            for category in categories:
                if category not in bucket:
                    bucket.append(category)

        if call.tool in seen:
            continue
        seen.add(call.tool)
        deduped.append(call)

    return [
        (
            call.model_copy(
                update={
                    "arguments": {
                        **call.arguments,
                        "categories": merged_categories_by_tool[call.tool],
                    }
                }
            )
            if call.tool in merged_categories_by_tool
            else call
        )
        for call in deduped
    ]


def _drop_forbidden_price_signal(
    tool_calls: list[ToolCall], property_details: PropertyDetails
) -> list[ToolCall]:
    """price_signal is forbidden for rentals, or when asking_price/sqm is missing."""
    kept: list[ToolCall] = []
    for call in tool_calls:
        if call.tool == "price_signal" and (
            property_details.listing_type == "rental"
            or property_details.asking_price is None
            or property_details.sqm is None
        ):
            continue
        kept.append(call)
    return kept


def _ensure_geo_resolver_dependency(
    tool_calls: list[ToolCall],
    property_details: PropertyDetails,
    missing_information: list[str],
) -> list[ToolCall]:
    """Ensure geo_resolver runs first whenever a location-dependent tool needs coordinates.

    If no address is available to resolve, the location-dependent tools are
    dropped instead of being called without the coordinates they need.
    """
    tool_names = {call.tool for call in tool_calls}
    needs_location = bool(tool_names & LOCATION_DEPENDENT_TOOLS)

    if needs_location and not property_details.raw_address:
        tool_calls = [call for call in tool_calls if call.tool not in LOCATION_DEPENDENT_TOOLS]
        tool_names = {call.tool for call in tool_calls}
        if "raw_address" not in missing_information:
            missing_information.append("raw_address")

    if needs_location and property_details.raw_address and "geo_resolver" not in tool_names:
        tool_calls = [
            ToolCall(tool="geo_resolver", arguments={"address": property_details.raw_address}),
            *tool_calls,
        ]

    # geo_resolver with no address to resolve is useless; drop it defensively.
    if "geo_resolver" in {call.tool for call in tool_calls} and not property_details.raw_address:
        tool_calls = [call for call in tool_calls if call.tool != "geo_resolver"]
        if "raw_address" not in missing_information:
            missing_information.append("raw_address")

    return tool_calls


def _ensure_nearby_services_for_known_claims(
    tool_calls: list[ToolCall],
    claims: list[Claim],
    property_details: PropertyDetails,
) -> list[ToolCall]:
    """Synthesize a missing `nearby_services` call for a known, verifiable
    `nearby_amenities` claim (Phase 19 - closes the "full listing loses
    nearby_services" deviation).

    `triage_planner` sometimes correctly classifies a claim's canonical
    category as `"nearby_amenities"` (see `agent.claim_categories`) but
    does not propose the matching `ToolCall` at all. Since the claim
    classification itself is already trusted deterministic behavior (not
    an LLM decision this function second-guesses), a known, checkable
    claim must not silently go unchecked just because the LLM forgot to
    propose its tool call.

    Never overrides a planner-proposed `nearby_services` call (including
    one with imperfect argument naming - that is normalized separately by
    `resolve_nearby_categories`), and never fires without a usable address
    (an unresolvable/missing address is handled by
    `_ensure_geo_resolver_dependency`, which drops location-dependent tools
    entirely rather than calling them blind).
    """
    if any(call.tool == "nearby_services" for call in tool_calls):
        return tool_calls
    if not property_details.raw_address:
        return tool_calls

    nearby_claim = next(
        (
            claim
            for claim in claims
            if claim.id
            and claim.type != "subjective"
            and normalize_category(claim.category) == "nearby_amenities"
        ),
        None,
    )
    if nearby_claim is None:
        return tool_calls

    categories = resolve_nearby_categories(claim_text=nearby_claim.text)
    arguments: dict[str, object] = {"categories": categories} if categories else {}
    return [
        *tool_calls,
        ToolCall(
            tool="nearby_services",
            arguments=arguments,
            supports_claim_ids=[nearby_claim.id],
        ),
    ]


def _ensure_price_comparison_claim_linkage(
    tool_calls: list[ToolCall],
    claims: list[Claim],
    buyer_goal: str,
) -> tuple[list[ToolCall], list[Claim]]:
    """Ensure a `price_signal` call is linked to a genuine comparative claim,
    never merely the literal `"price"` listing fact (Phase 19 - closes the
    "price evidence not clearly linked to the comparative question"
    deviation).

    When the buyer's goal is `"price_validation"` (a comparative
    price question) and `price_signal` is planned, but the plan contains
    no claim whose canonical category is `"price_comparison"`, the legacy
    keyword fallback in `evidence_mapper.claim_linking` would otherwise
    link `price_signal`'s evidence to the plain `"price"` listing-fact
    claim (a literal seller-stated number, not the comparative assertion
    being evaluated). This synthesizes one deterministic, clearly-marked
    derived claim (`Claim.source == ClaimSource.DERIVED`, so it is never
    confused with something the seller actually said) representing the
    buyer's comparative question, and sets the `price_signal` call's
    explicit `supports_claim_ids` (tier 1 - always wins over the
    heuristics below it) to that claim.

    Never overrides an already-explicit `supports_claim_ids` a planner
    call may have set, and never fires when a real comparative claim
    already exists.
    """
    if buyer_goal != "price_validation":
        return tool_calls, claims
    if not any(call.tool == "price_signal" for call in tool_calls):
        return tool_calls, claims
    if any(normalize_category(claim.category) == "price_comparison" for claim in claims):
        return tool_calls, claims

    existing_ids = {claim.id for claim in claims if claim.id}
    candidate_id = "claim-derived-price-comparison"
    suffix = 0
    while candidate_id in existing_ids:
        suffix += 1
        candidate_id = f"claim-derived-price-comparison-{suffix}"

    derived_claim = Claim(
        id=candidate_id,
        text=(
            "Buyer question: is the asking price below or at market price for this "
            "listing? (Derived from the price_validation goal - not a seller claim.)"
        ),
        type="factual",
        category="price_comparison",
        source=ClaimSource.DERIVED,
    )
    new_claims = [*claims, derived_claim]
    new_tool_calls = [
        (
            call.model_copy(update={"supports_claim_ids": [derived_claim.id]})
            if call.tool == "price_signal" and not call.supports_claim_ids
            else call
        )
        for call in tool_calls
    ]
    return new_tool_calls, new_claims


def _assign_stable_claim_ids(claims: list[Claim]) -> list[Claim]:
    """Assign a stable `"claim-{index}"` id to any claim missing one.

    `triage_planner` (the LLM) does not need to invent ids itself; this
    deterministic step guarantees every claim in a validated plan has a
    stable id `evidence_mapper` can later link evidence to.
    """
    return [
        claim if claim.id else claim.model_copy(update={"id": f"claim-{i}"})
        for i, claim in enumerate(claims)
    ]


def _cap_claims(
    claims: list[Claim], max_claims: int, missing_information: list[str]
) -> list[Claim]:
    """Cap the number of claims carried forward, keeping the first `max_claims`.

    Extra claims are dropped deterministically (never by asking the LLM to
    re-plan): this bounds the size of the Evidence Bundle and the compact
    payload eventually sent to `report_writer`, protecting cost and
    latency. The drop is recorded in `missing_information` so it is never
    silent.
    """
    if len(claims) <= max_claims:
        return claims
    if "additional_claims_truncated" not in missing_information:
        missing_information.append("additional_claims_truncated")
    return claims[:max_claims]


def validate_plan(
    plan: TriagePlan,
    *,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_claims: int = MAX_CLAIMS,
) -> TriagePlan:
    """Validate and normalize `plan`, returning a cleaned-up `TriagePlan`.

    Rules applied, in order:
    1. Reject any tool call outside the whitelist (raises `PlanValidationError`).
    2. Cap the plan at `max_claims` claims (default 8), recording the drop.
    3. Assign a stable id to any claim that doesn't already have one (done
       before tool-call synthesis below, so a synthesized call can link to
       a stable claim id).
    4. Remove duplicate calls to the same tool.
    5. Drop `price_signal` calls forbidden for rentals or missing price/sqm.
    6. Synthesize a missing `nearby_services` call for a known, verifiable
       `nearby_amenities` claim the planner did not propose a tool call for
       (Phase 19).
    7. Ensure `geo_resolver` runs first whenever a location-dependent tool
       is selected (dropping those tools if no address is available).
    8. Ensure a `price_signal` call is linked to a genuine comparative
       claim rather than the literal `"price"` listing fact, synthesizing
       one deterministic, explicitly-marked derived claim when the buyer's
       goal is `"price_validation"` and no such claim already exists
       (Phase 19).
    9. Order `geo_resolver` first (dependency order).
    10. Cap the plan at `max_tool_calls` tool calls (default 3).

    Args:
        max_tool_calls: Configurable cap (Phase 17), normally sourced from
            `Settings.max_tool_calls`.
        max_claims: Configurable cap (Phase 17), normally sourced from
            `Settings.max_claims`.
    """
    unknown_tool_errors = [
        f"Unknown tool '{call.tool}' is not in the allowed tool whitelist "
        f"({', '.join(sorted(ALLOWED_TOOLS))})."
        for call in plan.tool_calls
        if call.tool not in ALLOWED_TOOLS
    ]
    if unknown_tool_errors:
        raise PlanValidationError(unknown_tool_errors)

    missing_information = list(plan.missing_information)

    claims = _cap_claims(plan.claims, max_claims, missing_information)
    claims = _assign_stable_claim_ids(claims)

    tool_calls = _dedupe_by_tool(plan.tool_calls)
    tool_calls = _drop_forbidden_price_signal(tool_calls, plan.property_details)
    tool_calls = _ensure_nearby_services_for_known_claims(
        tool_calls, claims, plan.property_details
    )
    tool_calls = _ensure_geo_resolver_dependency(
        tool_calls, plan.property_details, missing_information
    )
    tool_calls, claims = _ensure_price_comparison_claim_linkage(
        tool_calls, claims, plan.buyer_goal
    )

    def _sort_key(call: ToolCall) -> int:
        return 0 if call.tool == "geo_resolver" else 1

    tool_calls = sorted(tool_calls, key=_sort_key)[:max_tool_calls]

    return plan.model_copy(
        update={
            "tool_calls": tool_calls,
            "missing_information": missing_information,
            "claims": claims,
        }
    )


__all__ = [
    "ALLOWED_TOOLS",
    "LOCATION_DEPENDENT_TOOLS",
    "MAX_TOOL_CALLS",
    "MAX_CLAIMS",
    "PlanValidationError",
    "validate_plan",
    "ToolName",
]
