"""The deterministic `plan_validator` module.

Validates and normalizes the `TriagePlan` produced by `triage_planner`
before any tool is executed. Pure/deterministic: no LLM or network calls.
"""

import math
import re

from src.listing_agent.agent.claim_categories import (
    TOOLS_BY_VERIFIABLE_CATEGORY,
    normalize_category,
)
from src.listing_agent.agent.schemas import (
    BuyerGoal,
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

# Canonical categories carrying a structured `PropertyDetails` equivalent -
# a claim in one of these categories is redundant with a typed field
# already available to `report_writer`/tools; a *second* claim in the same
# category never needs to consume another claim slot (Phase: claim
# retention fix). Deliberately excludes `"condition"`: distinct condition
# claims (safe room vs. parquet vs. kitchen) are never redundant with each
# other just because they share a category.
_REDUNDANT_WITH_PROPERTY_DETAILS: frozenset[str] = frozenset(
    {"address", "price", "rooms", "size_sqm"}
)

# Of the categories above, these carry a single *numeric* `PropertyDetails`
# field that a claim's own text can be checked against (Phase: multi-entity
# dedup fix). `"address"` is deliberately excluded: it has no comparable
# numeric value, and free-text address wording (e.g. a neighborhood name
# vs. a street+number) can never be reliably compared for "is this the same
# fact" beyond "it is still *a* address claim about the current listing" -
# that coarser, category-only rule is unchanged from before this fix.
_NUMERIC_PROPERTY_DETAIL_FIELDS: dict[str, str] = {
    "price": "asking_price",
    "rooms": "rooms",
    "size_sqm": "sqm",
}

# Matches a plain number in either LTR or Hebrew-preceding-punctuation
# text, with optional thousands separators (","/".") and an optional
# decimal part - e.g. "82", "4,350,000", "12.5".
_NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _extract_numbers(text: str) -> list[float]:
    """Extract every plain number literally present in `text`.

    Deliberately dumb/general (no unit-word parsing, no listing-specific
    keyword matching): just finds digit runs and normalizes thousands
    separators, so it works identically regardless of language, address,
    or listing.
    """
    numbers: list[float] = []
    for match in _NUMBER_PATTERN.finditer(text):
        raw = match.group().replace(",", "")
        try:
            numbers.append(float(raw))
        except ValueError:
            continue
    return numbers


def _claim_restates_property_detail_value(
    canonical: str, claim_text: str, property_details: PropertyDetails
) -> bool:
    """Whether `claim_text` actually restates the *same* numeric fact
    already captured by the matching `PropertyDetails` field for
    `canonical` (Phase: multi-entity dedup fix).

    A claim sharing a "listing-fact" category with `PropertyDetails` is
    only truly redundant when it names the *same value* - e.g. both say
    "60 sqm". A second claim in the same category naming a *different*
    value (a future replacement unit's own 82 sqm, its 12 sqm balcony, a
    comparison unit's price, ...) describes a distinct fact/entity and
    must never be dropped merely for sharing a category with the current
    property's structured field.

    Returns `False` (never redundant by this rule) whenever there is no
    comparable `PropertyDetails` value to check against, or the category
    has no numeric field at all (e.g. `"address"` - handled separately,
    unchanged, by category alone).
    """
    field_name = _NUMERIC_PROPERTY_DETAIL_FIELDS.get(canonical)
    if field_name is None:
        return False
    target = getattr(property_details, field_name, None)
    if target is None:
        return False
    return any(
        math.isclose(number, target, rel_tol=1e-9, abs_tol=1e-6)
        for number in _extract_numbers(claim_text)
    )

# Which canonical claim categories are "directly relevant" to a given
# `BuyerGoal`, for claim-retention prioritization only (priority 1 below).
# `None` means the goal does not narrow relevance - the buyer asked
# something broad/generic enough that ranking falls through to the general
# priority order (2-6) instead of a blanket "everything is priority 1".
# This mirrors (but is independently defined from) the diagnostic-only
# explanatory mapping in `scripts/inspect_listing_pipeline.py`, which only
# explains question-answerability text and never decides claim retention.
_GOAL_RELEVANT_CATEGORIES: dict[str, frozenset[str] | None] = {
    "family_suitability": frozenset({"nearby_amenities", "address", "rooms", "size_sqm"}),
    "nearby_services_check": frozenset({"nearby_amenities", "address"}),
    "price_validation": frozenset({"price_comparison", "price", "size_sqm", "address"}),
    "general_question": None,
    "fast_claim_triage": None,
}


def _claim_priority(claim: Claim, buyer_goal: BuyerGoal) -> int:
    """Deterministic claim-retention priority (lower survives the cap first).

    1. Claims whose canonical category is directly relevant to `buyer_goal`.
    2. Claims checkable by a current tool (`nearby_amenities`,
       `price_comparison`).
    3. Material transaction-structure / future-commitment claims.
    4. Factual claims requiring seller/document/physical verification
       (including a claim with no canonical category, e.g. floor).
    5. Unique listing facts already represented by a typed
       `PropertyDetails` field (address/price/rooms/size_sqm) - a
       redundant *duplicate* claim in the same category is removed
       entirely by `_dedupe_claims` before this ranking ever runs, so
       only the single surviving representative claim reaches here.
    6. Subjective marketing claims.

    Never depends on raw LLM list position - only on `claim.type` and its
    already-normalized canonical category, so ties are broken purely by
    stable sort order (the claims' order after deduplication).

    Note (multi-entity dedup fix): for a numeric category
    (price/rooms/size_sqm), `_dedupe_claims` now only removes a claim that
    restates the *same value* as the `PropertyDetails` field - a
    distinct-value claim describing another entity/state (e.g. a future
    replacement unit's own size, or its balcony) survives dedup and can
    also reach priority 5 alongside the single current-property
    representative. This is intentional: both are still "a listing fact
    of this shape", just not necessarily about the current property.
    """
    if claim.type == "subjective":
        return 6

    canonical = normalize_category(claim.category)
    relevant = _GOAL_RELEVANT_CATEGORIES.get(buyer_goal)
    if relevant is not None and canonical in relevant:
        return 1
    if canonical in TOOLS_BY_VERIFIABLE_CATEGORY:
        return 2
    if canonical == "transaction_structure":
        return 3
    if canonical in _REDUNDANT_WITH_PROPERTY_DETAILS:
        return 5
    # condition, or no canonical category at all (e.g. floor) - a
    # factual claim requiring seller/document/physical verification.
    return 4


def _normalized_claim_text(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def _dedupe_claims(claims: list[Claim], property_details: PropertyDetails) -> list[Claim]:
    """Remove claims that would waste claim-cap capacity without losing
    any distinct fact (Phase: claim retention fix; refined by the
    multi-entity dedup fix).

    Two independent rules, applied in order:

    1. Exact-duplicate normalized text (whitespace/case-insensitive) -
       always redundant regardless of category.
    2. A *second or later* claim whose canonical category is already
       represented by a typed `PropertyDetails` field
       (`_REDUNDANT_WITH_PROPERTY_DETAILS`: address/price/rooms/size_sqm):

       - `"address"` has no comparable numeric value, so it keeps the
         original, coarser behavior - the first address claim is kept,
         later ones are dropped purely by category (still correct for a
         single-property listing's own alternate address wordings).
       - `"price"`/`"rooms"`/`"size_sqm"` are only dropped as duplicates
         when the claim's own text actually restates the *same value* as
         `PropertyDetails` (`_claim_restates_property_detail_value`). A
         claim naming a *different* value in the same category (a future
         replacement unit's own size, its balcony, a second entity's
         price, ...) is never a redundant restatement of the current
         property's structured field and must survive - dropping it would
         silently lose a distinct fact about a distinct entity/state.

    Never deduplicates by category alone for any other category:
    `"condition"` claims (safe room, parquet, kitchen, bathroom, ...) are
    always distinct commitments and are never merged just because they
    share a broad legacy category.
    """
    seen_text: set[str] = set()
    seen_property_detail_categories: set[str] = set()
    deduped: list[Claim] = []
    for claim in claims:
        normalized_text = _normalized_claim_text(claim.text)
        if normalized_text and normalized_text in seen_text:
            continue

        canonical = normalize_category(claim.category)
        if canonical in _REDUNDANT_WITH_PROPERTY_DETAILS:
            if canonical in _NUMERIC_PROPERTY_DETAIL_FIELDS:
                if _claim_restates_property_detail_value(
                    canonical, claim.text, property_details
                ):
                    if canonical in seen_property_detail_categories:
                        continue
                    seen_property_detail_categories.add(canonical)
                # else: a distinct-value claim for this numeric category -
                # never deduped by this rule, regardless of how many other
                # claims already matched the property-details value.
            else:
                # "address" (or any future non-numeric category added
                # here): unchanged, category-only dedup.
                if canonical in seen_property_detail_categories:
                    continue
                seen_property_detail_categories.add(canonical)

        if normalized_text:
            seen_text.add(normalized_text)
        deduped.append(claim)
    return deduped


def _prioritize_claims(claims: list[Claim], buyer_goal: BuyerGoal) -> list[Claim]:
    """Stable-sort `claims` by `_claim_priority` only - ties preserve the
    incoming (post-dedup) order, never raw LLM list position alone."""
    return sorted(claims, key=lambda claim: _claim_priority(claim, buyer_goal))


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


# Deterministic, fixed vocabulary (Hebrew + English) of explicit
# comparative-price phrasing (Phase 0 - price provenance guard). Checked as
# a case-insensitive substring of the current turn's raw buyer prompt -
# never a heuristic guess beyond these literal markers, and never an LLM
# judgment. Mirrors the style of `tool_input_builder`'s alias tables.
_COMPARATIVE_PRICE_MARKERS: tuple[str, ...] = (
    "fair price",
    "fair value",
    "is the price fair",
    "is it fair",
    "market price",
    "market value",
    "below market",
    "above market",
    "overpriced",
    "under priced",
    "underpriced",
    "over priced",
    "good price",
    "good deal",
    "bargain",
    "worth the price",
    "worth it",
    "is it worth",
    "fair",
    "מחיר הוגן",
    "מחיר השוק",
    "מתחת למחיר",
    "מעל למחיר",
    "מעל מחיר",
    "מתחת מחיר",
    "הוגן",
    "מציאה",
    "יקר מדי",
    "זול מדי",
    "כדאי",
    "שווה את המחיר",
    "שווה את הכסף",
)

# General rule (not a fixed phrase list): an explicit reference to the
# price/cost, combined *anywhere in the same prompt* with an explicit
# comparative/evaluative judgment word, also counts as comparative-price
# intent - this generalizes beyond `_COMPARATIVE_PRICE_MARKERS`'s literal
# phrases to catch real rephrasings such as "Is the asking price reasonable
# compared to the area?" (no single fixed marker above matches that
# sentence, but it unambiguously asks for a price-vs-market comparison).
_PRICE_REFERENCE_MARKERS: tuple[str, ...] = (
    "price",
    "asking price",
    "cost",
    "מחיר",
)
_COMPARATIVE_JUDGMENT_MARKERS: tuple[str, ...] = (
    "reasonable",
    "fair",
    "worth",
    "bargain",
    "overpriced",
    "underpriced",
    "under priced",
    "over priced",
    "good deal",
    "good price",
    "market",
    "compare",
    "compared",
    "comparison",
    "cheap",
    "expensive",
    "too high",
    "too low",
    "versus",
    "הוגן",
    "השוואה",
    "משתלם",
    "כדאי",
    "יקר",
    "זול",
    "לעומת",
    "ביחס",
    "מציאה",
)


def prompt_has_comparative_price_intent(buyer_prompt: str) -> bool:
    """Deterministically detect explicit comparative-price intent in the
    current turn's raw buyer prompt (Phase 0).

    Two independent, purely deterministic checks - never an LLM judgment,
    never trusting `buyer_goal` alone:
    1. A literal fixed phrase from `_COMPARATIVE_PRICE_MARKERS`, or
    2. The general rule: an explicit price/cost reference
       (`_PRICE_REFERENCE_MARKERS`) *and* an explicit comparative/evaluative
       judgment word (`_COMPARATIVE_JUDGMENT_MARKERS`) both present
       somewhere in the prompt, in any order/phrasing.

    Used only as one of two ways to *justify* a `price_signal` call - the
    other being an explicit, non-derived `price_comparison` claim already
    present in the plan (see `_drop_unjustified_price_signal`). Never a
    substitute for the LLM's own `buyer_goal` classification, which is not
    itself sufficient justification.
    """
    if not buyer_prompt:
        return False
    lowered = buyer_prompt.strip().lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in _COMPARATIVE_PRICE_MARKERS):
        return True
    has_price_reference = any(marker in lowered for marker in _PRICE_REFERENCE_MARKERS)
    has_comparative_judgment = any(
        marker in lowered for marker in _COMPARATIVE_JUDGMENT_MARKERS
    )
    return has_price_reference and has_comparative_judgment


def _has_comparative_price_provenance(claims: list[Claim]) -> bool:
    """Whether the plan already carries a *real* (non-derived) comparative
    price claim - i.e. an actual explicit comparative-price assertion the
    listing itself made, not a claim the system synthesized to justify
    itself (Phase 0: "A DERIVED price-comparison claim may be created for
    linkage only AFTER real comparative provenance exists. It must never
    justify itself.")."""
    return any(
        normalize_category(claim.category) == "price_comparison"
        and claim.source != ClaimSource.DERIVED
        for claim in claims
    )


def _drop_unjustified_price_signal(
    tool_calls: list[ToolCall], claims: list[Claim], buyer_prompt: str
) -> list[ToolCall]:
    """Phase 0 price-provenance guard.

    `price_signal` is allowed only when:
    A. the current turn's raw buyer prompt contains explicit
       comparative-price intent (`prompt_has_comparative_price_intent`), or
    B. the listing/plan already contains an explicit, non-derived
       comparative-price claim (`_has_comparative_price_provenance`).

    `buyer_goal == "price_validation"` alone (an LLM classification with no
    corroborating evidence) is never sufficient - this is the deterministic
    fix for the known bug where a plain listing with just an asking price
    and size could otherwise trigger `price_signal` merely because the
    planner invented that goal.
    """
    if _has_comparative_price_provenance(claims) or prompt_has_comparative_price_intent(
        buyer_prompt
    ):
        return tool_calls
    return [call for call in tool_calls if call.tool != "price_signal"]


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
    buyer_goal: str,
) -> list[ToolCall]:
    """Synthesize a missing `nearby_services` call for a known, verifiable
    `nearby_amenities` claim (Phase 19 - closes the "full listing loses
    nearby_services" deviation) - but only when the current turn's
    `buyer_goal` hasn't already explicitly narrowed the plan away from
    nearby-amenities checking (Stage 1.5/1.6 fix).

    `triage_planner` sometimes correctly classifies a claim's canonical
    category as `"nearby_amenities"` (see `agent.claim_categories`) but
    does not propose the matching `ToolCall` at all. Since the claim
    classification itself is already trusted deterministic behavior (not
    an LLM decision this function second-guesses), a known, checkable
    claim must not silently go unchecked just because the LLM forgot to
    propose its tool call - this applies to `fast_claim_triage`,
    `family_suitability`, and `nearby_services_check`, which all still get
    this default/general-triage behavior.

    Exception: when `buyer_goal` is `"price_validation"` or
    `"general_question"`, the buyer has explicitly narrowed this turn away
    from a general/default triage - either to a comparative-price question,
    or (for `"general_question"`) to some other specific ask the planner
    judged didn't itself call for any tool - and the planner already omitted
    `nearby_services` on purpose. Synthesizing an unrelated nearby-amenities
    check behind the planner's back in either case would silently
    reintroduce a check the explicit narrowed goal does not call for, so
    this function does not fire for `"price_validation"` or
    `"general_question"` (Stage 4.4 fix: a `general_question` turn must
    select a tool only when the current question itself asks for what that
    tool provides - a merely-retained, unrelated nearby_amenities listing
    claim must not silently cause a tool check).

    Never overrides a planner-proposed `nearby_services` call (including
    one with imperfect argument naming - that is normalized separately by
    `resolve_nearby_categories`), and never fires without a usable address
    (an unresolvable/missing address is handled by
    `_ensure_geo_resolver_dependency`, which drops location-dependent tools
    entirely rather than calling them blind).

    Only synthesizes a call when at least one supported frozen category is
    grounded in the linked claims' text. If a `nearby_amenities` claim
    resolves to no supported category, no call is synthesized - the tool
    must not fan out to every default category to "support" a claim no
    supported category actually covers.
    """
    if any(call.tool == "nearby_services" for call in tool_calls):
        return tool_calls
    if buyer_goal in ("price_validation", "general_question"):
        return tool_calls
    if not property_details.raw_address:
        return tool_calls

    nearby_claims = [
        claim
        for claim in claims
        if claim.id
        and claim.type != "subjective"
        and normalize_category(claim.category) == "nearby_amenities"
    ]
    if not nearby_claims:
        return tool_calls

    # Combine every matching claim's text (not just the first) when
    # resolving categories: an atomized planner output may split "green
    # view, kindergarten complex, street parking" into three separate
    # `nearby_amenities` claims, and the *first* one alone (e.g. "green
    # view") may carry no recognizable category keyword at all - only the
    # combined text reliably recovers "daycare" from whichever claim
    # actually mentions it.
    combined_text = " ".join(claim.text for claim in nearby_claims)
    categories = resolve_nearby_categories(claim_text=combined_text)
    # Only synthesize a call when at least one *supported* frozen category is
    # grounded in the linked claims' own text. A `nearby_amenities` claim whose
    # text resolves to no supported category (e.g. robotic parking, a community
    # centre, generic "educational institutions") must NOT synthesize a
    # `nearby_services` call: an empty-category synthesized call would fan out
    # to the full default category set downstream and fabricate evidence
    # categories no retained claim actually supports. The empty-category
    # default-to-all behavior is reserved for a genuinely broad, *planner
    # proposed* "what's nearby" request (handled in `build_tool_input`), never
    # for synthesis the validator adds on the planner's behalf.
    if not categories:
        return tool_calls
    return [
        *tool_calls,
        ToolCall(
            tool="nearby_services",
            arguments={"categories": categories},
            supports_claim_ids=[claim.id for claim in nearby_claims],
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
    claims: list[Claim],
    max_claims: int,
    missing_information: list[str],
    *,
    produced_count: int | None = None,
    deduped_count: int | None = None,
) -> list[Claim]:
    """Cap the (already deduplicated and priority-sorted) claim list at
    `max_claims`, keeping the first `max_claims` entries.

    Deliberately dumb: by the time this runs, `claims` has already been
    deduplicated (`_dedupe_claims`) and reordered by semantic priority
    (`_prioritize_claims`), so "keep the first N" now means "keep the N
    highest-priority, non-redundant claims" - never "keep the first N in
    raw LLM order" (the original claim-loss defect).

    Records two things in `missing_information` so the drop is never
    silent:

    1. the exact, stable marker `"additional_claims_truncated"` (kept
       byte-for-byte unchanged - existing callers/tests/reports that check
       for this exact string must keep working);
    2. Phase (truncation observability): a richer, still-compact
       diagnostic string with produced/deduplicated/retained/removed
       counts and the *category* (never the raw text) of what was
       removed, via `_truncation_summary`.
    """
    if len(claims) <= max_claims:
        return claims
    if "additional_claims_truncated" not in missing_information:
        missing_information.append("additional_claims_truncated")

    retained = claims[:max_claims]
    removed = claims[max_claims:]
    summary = _truncation_summary(
        produced_count=produced_count if produced_count is not None else len(claims),
        deduped_count=deduped_count if deduped_count is not None else len(claims),
        retained_count=len(retained),
        removed_claims=removed,
    )
    if summary and summary not in missing_information:
        missing_information.append(summary)
    return retained


def _truncation_summary(
    produced_count: int, deduped_count: int, retained_count: int, removed_claims: list[Claim]
) -> str | None:
    """Build a compact, human-readable diagnostic string describing what a
    claims-cap truncation actually removed (Phase: truncation
    observability).

    Never includes full raw claim text (avoids bloating
    `missing_information` with large discarded payloads) - only counts and
    a deduplicated, sorted list of the removed claims' canonical
    categories (or `"uncategorized"`), so a buyer-facing report or a
    reviewer can tell *what kind* of information was trimmed without a
    separate, larger diagnostic payload.
    """
    if not removed_claims:
        return None
    categories = sorted(
        {normalize_category(claim.category) or "uncategorized" for claim in removed_claims}
    )
    return (
        f"additional_claims_truncated_detail: produced={produced_count} "
        f"deduplicated={deduped_count} retained={retained_count} "
        f"removed={len(removed_claims)} removed_categories={categories}"
    )


def validate_plan(
    plan: TriagePlan,
    *,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_claims: int = MAX_CLAIMS,
    buyer_prompt: str = "",
) -> TriagePlan:
    """Validate and normalize `plan`, returning a cleaned-up `TriagePlan`.

    Rules applied, in order:
    1. Reject any tool call outside the whitelist (raises `PlanValidationError`).
    2. Deduplicate claims: exact-duplicate normalized text, and a second+
       claim redundant with an already-captured `PropertyDetails` field
       (address/price/rooms/size_sqm) - never claims that merely share a
       broader category (e.g. two distinct `"condition"` commitments).
    3. Deterministically prioritize the deduplicated claims (buyer-goal
       relevance > tool-checkable > transaction-structure > seller/document
       verification > redundant-with-PropertyDetails > subjective), with a
       stable sort so equal-priority claims keep their post-dedup order.
    4. Cap the prioritized plan at `max_claims` claims (default 8),
       recording what was produced/deduplicated/retained/removed.
    5. Assign a stable id to any claim that doesn't already have one (done
       before tool-call synthesis below, so a synthesized call can link to
       a stable claim id).
    6. Remove duplicate calls to the same tool.
    7. Drop `price_signal` calls forbidden for rentals or missing price/sqm.
    8. Synthesize a missing `nearby_services` call for a known, verifiable
       `nearby_amenities` claim the planner did not propose a tool call for
       (Phase 19), unless `buyer_goal` is `"price_validation"` or
       `"general_question"` - an explicit narrowed price-only goal, or a
       general question the planner judged didn't itself call for any tool,
       must not have an unrelated nearby check synthesized behind the
       planner (Stage 1.5/1.6 fix; Stage 4.4 fix extends this to
       `general_question`).
    9. Ensure `geo_resolver` runs first whenever a location-dependent tool
       is selected (dropping those tools if no address is available).
    10. Drop `price_signal` unless justified by an explicit comparative-price
        claim or explicit comparative-price intent in `buyer_prompt` (Phase
        0 - price provenance guard); `buyer_goal` alone is never enough.
    11. Ensure a `price_signal` call is linked to a genuine comparative
        claim rather than the literal `"price"` listing fact, synthesizing
        one deterministic, explicitly-marked derived claim when the buyer's
        goal is `"price_validation"` and no such claim already exists
        (Phase 19). Only ever fires once step 10 has already justified
        keeping `price_signal`.
    12. Order `geo_resolver` first (dependency order).
    13. Cap the plan at `max_tool_calls` tool calls (default 3).

    Args:
        max_tool_calls: Configurable cap (Phase 17), normally sourced from
            `Settings.max_tool_calls`.
        max_claims: Configurable cap (Phase 17), normally sourced from
            `Settings.max_claims`.
        buyer_prompt: The current turn's raw buyer prompt (Phase 0), used
            only to deterministically detect explicit comparative-price
            intent (see `prompt_has_comparative_price_intent`). Defaults to
            `""` (no signal) for backward compatibility with existing
            direct callers/tests.
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

    produced_count = len(plan.claims)
    deduped_claims = _dedupe_claims(plan.claims, plan.property_details)
    prioritized_claims = _prioritize_claims(deduped_claims, plan.buyer_goal)
    claims = _cap_claims(
        prioritized_claims,
        max_claims,
        missing_information,
        produced_count=produced_count,
        deduped_count=len(deduped_claims),
    )
    claims = _assign_stable_claim_ids(claims)

    tool_calls = _dedupe_by_tool(plan.tool_calls)
    tool_calls = _drop_forbidden_price_signal(tool_calls, plan.property_details)
    tool_calls = _ensure_nearby_services_for_known_claims(
        tool_calls, claims, plan.property_details, plan.buyer_goal
    )
    tool_calls = _ensure_geo_resolver_dependency(
        tool_calls, plan.property_details, missing_information
    )
    tool_calls = _drop_unjustified_price_signal(tool_calls, claims, buyer_prompt)
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
