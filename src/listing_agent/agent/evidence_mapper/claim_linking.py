"""Tool-call <-> claim linkage (Phase 15, corrected in Phase 18).

There is no explicit "this tool call is for this claim" field produced by
`triage_planner` today (adding one would mean redesigning the planner's
frozen output schema). Linkage is therefore deterministic and layered, in
priority order:

1. **Explicit** (`ToolCall.supports_claim_ids`): if a `ToolCall` already
   carries an explicit, valid claim linkage, use it as-is. Not currently
   populated by `triage_planner`, but a forward-compatible hook that
   always wins over the heuristics below when present.
2. **Canonical category** (`agent.claim_categories`): the claim's
   `category` is normalized to one canonical category, and only
   *verifiable* canonical categories (e.g. `"nearby_amenities"`,
   `"price_comparison"`) are matched to the tool(s) that can actually
   produce evidence for them. This is what fixes real planner output using
   category names the legacy keyword table below never covered, and what
   keeps `price_signal` linked to an actual comparison claim
   (`"price_comparison"`) instead of the plain listing fact (`"price"`)
   when both exist in the same plan.
3. **Legacy keyword fallback**: only used when neither of the above
   produced a match, for backward compatibility with older
   fixtures/tests/plans whose categories predate the canonical vocabulary
   (e.g. a bare `"price"`/`"school"`/`"family"` category with no
   `"price_comparison"`/`"nearby_amenities"` claim anywhere in the plan).

This is still a best-effort heuristic, not a certainty: many valid plans
have no claim with a matching category at all, and `find_relevant_claim_id`
simply returns `None` in that case - evidence is never forced onto an
unrelated claim just to have a non-null `claim_id`.
"""

from src.listing_agent.agent.claim_categories import (
    LISTING_FACT_INPUT_TOOLS,
    TOOLS_BY_VERIFIABLE_CATEGORY,
    normalize_category,
)
from src.listing_agent.agent.schemas import Claim, ToolCall, ToolName

_CATEGORY_KEYWORDS_BY_TOOL: dict[ToolName, frozenset[str]] = {
    "geo_resolver": frozenset({"location", "address", "neighborhood", "area"}),
    "nearby_services": frozenset(
        {
            "location",
            "neighborhood",
            "area",
            "family",
            "school",
            "services",
            "transport",
            "safety",
            "amenities",
            "daycare",
            "park",
        }
    ),
    "price_signal": frozenset({"price", "value", "cost", "market", "bargain"}),
}


def relevant_tools_for_claim(claim: Claim) -> list[ToolName]:
    """Return every tool relevant to `claim`, in the layered priority order
    described in this module's docstring (tiers 2-3; tier 1 is per-call,
    not per-claim, and only applies inside `find_relevant_claim_ids`).

    Includes both tools that could produce *verifiable evidence* for this
    claim (canonical "verifiable" categories) and tools that merely
    *consume* this claim as input when it is a listing fact (e.g.
    `"address"` -> `geo_resolver`) - used by diagnostic tooling to explain
    what each identified claim relates to, independent of whether the
    current plan actually calls one of them.
    """
    canonical = normalize_category(claim.category)
    if canonical in TOOLS_BY_VERIFIABLE_CATEGORY:
        return list(TOOLS_BY_VERIFIABLE_CATEGORY[canonical])
    if canonical in LISTING_FACT_INPUT_TOOLS:
        return list(LISTING_FACT_INPUT_TOOLS[canonical])

    if not claim.category:
        return []
    category = claim.category.strip().lower()
    return [tool for tool, keywords in _CATEGORY_KEYWORDS_BY_TOOL.items() if category in keywords]


def find_relevant_claim_id(
    tool: ToolName, claims: list[Claim], call: ToolCall | None = None
) -> str | None:
    """Return the id of the first claim relevant to `tool`.

    Returns `None` when no claim in `claims` is relevant to `tool`
    (including when `claims` is empty). Kept for backward compatibility;
    prefer `find_relevant_claim_ids` when a tool result may legitimately
    support more than one claim (Phase 17).
    """
    ids = find_relevant_claim_ids(tool, claims, call)
    return ids[0] if ids else None


def find_relevant_claim_ids(
    tool: ToolName, claims: list[Claim], call: ToolCall | None = None
) -> list[str]:
    """Return the ids of *every* claim relevant to `tool`, using the
    layered priority order described in this module's docstring.

    Args:
        call: The `ToolCall` actually being linked, if available. When
            `call.supports_claim_ids` is set and references at least one
            real id in `claims`, it is used as-is (tier 1) - the
            deterministic heuristics below never override an explicit,
            valid linkage.
    """
    if call is not None and call.supports_claim_ids:
        known_ids = {claim.id for claim in claims if claim.id}
        explicit = [claim_id for claim_id in call.supports_claim_ids if claim_id in known_ids]
        if explicit:
            return explicit

    canonical_matches = [
        claim.id
        for claim in claims
        if claim.id
        and tool in TOOLS_BY_VERIFIABLE_CATEGORY.get(normalize_category(claim.category), ())
    ]
    if canonical_matches:
        return canonical_matches

    keywords = _CATEGORY_KEYWORDS_BY_TOOL[tool]
    return [
        claim.id
        for claim in claims
        if claim.id and claim.category and claim.category.strip().lower() in keywords
    ]
