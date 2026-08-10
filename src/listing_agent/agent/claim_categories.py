"""Canonical claim-category normalization and classification (Phase 18 fix).

`triage_planner` (the LLM) emits a free-text `Claim.category` string. Over
time/different runs it has emitted many different spellings for the same
underlying concept (e.g. `"school"`, `"family"`, `"nearby_services"`, and
`"nearby_amenities"` all mean "check what's nearby"). Treating each of
these as an independent, unrelated keyword — which is what
`evidence_mapper.claim_linking` did before this module existed — silently
drops real, checkable claims the moment the planner phrases its category
slightly differently, and worse, never distinguishes a *listing fact*
(e.g. "the asking price is 4,700,000") from an actual, independently
*verifiable claim* about that fact (e.g. "this is below market price").

This module is the single, centralized place that:

1. Normalizes any raw category string to one canonical
   `CanonicalClaimCategory`, so every consumer (claim linkage, the
   diagnostic runner, future tooling) agrees on what a category means.
2. Classifies a `Claim` into exactly one of: `"subjective"` (the claim's
   own `type`, never inferred from category), `"listing_fact"` (a fact
   about the listing itself - an input to a tool, never itself an
   independent claim needing verification), `"verifiable"` (an
   independently checkable claim a tool can produce real evidence for),
   or `"unsupported"` (a category no current tool covers at all).
3. Maps each *verifiable* canonical category to the tool(s) that can
   actually produce evidence for it, and each *listing_fact* category to
   the tool(s) that consume it as input (informational only - a
   listing-fact claim is never "sent to a tool" as a claim to check).

Pure, deterministic, LLM-free.
"""

from typing import Literal

from src.listing_agent.agent.schemas import Claim, ToolName

CanonicalClaimCategory = Literal[
    "rooms",
    "condition",
    "address",
    "size_sqm",
    "price",
    "nearby_amenities",
    "price_comparison",
    "transaction_structure",
]

ClaimClassification = Literal["subjective", "listing_fact", "verifiable", "unsupported"]

# The Pre-Tool Triage verification-decision vocabulary (see
# `docs/pre_tool_triage.md`). Distinct from - and richer than -
# `ClaimClassification` above (which only drives tool linkage): this is the
# buyer-facing semantic bucket every retained claim must fall into so the
# Pre-Tool Triage output can group claims by what would actually resolve
# them (a tool call vs. asking the seller vs. reading the contract), not
# merely whether a tool happens to exist today.
VerificationDecision = Literal[
    "LISTING_FACT",
    "CHECKABLE_WITH_TOOL",
    "REQUIRES_SELLER_OR_DOCUMENT",
    "REQUIRES_CONTRACT_REVIEW",
    "SUBJECTIVE",
    "OUT_OF_SCOPE",
]

# Every raw category spelling `triage_planner` has been observed to emit
# (across different runs/prompts), normalized to one canonical category.
# Extend this table - never add a second, independent mapping - whenever a
# new raw spelling is observed.
_CATEGORY_ALIASES: dict[str, CanonicalClaimCategory] = {
    # rooms
    "rooms": "rooms",
    "room_count": "rooms",
    # condition (always subjective wording; see `ClaimClassification`)
    "condition": "condition",
    "appearance": "condition",
    "aesthetic": "condition",
    # address / location fact
    "address": "address",
    "raw_address": "address",
    # size
    "size_sqm": "size_sqm",
    "sqm": "size_sqm",
    "size": "size_sqm",
    # price (the listing fact - NOT the same as a comparison claim)
    "price": "price",
    "asking_price": "price",
    # nearby amenities (verifiable claim category)
    "nearby_amenities": "nearby_amenities",
    "nearby_services": "nearby_amenities",
    "school": "nearby_amenities",
    "schools": "nearby_amenities",
    "kindergarten": "nearby_amenities",
    "daycare": "nearby_amenities",
    "park": "nearby_amenities",
    "parks": "nearby_amenities",
    "family": "nearby_amenities",
    "amenities": "nearby_amenities",
    "services": "nearby_amenities",
    "safety": "nearby_amenities",
    "transport": "nearby_amenities",
    # price comparison (verifiable claim category - distinct from "price")
    "price_comparison": "price_comparison",
    "bargain": "price_comparison",
    "value": "price_comparison",
    "market": "price_comparison",
    "cost": "price_comparison",
    # transaction structure (contractual/commercial terms - no current
    # tool verifies these; distinct from "condition", never merged with
    # it just because both are "factual, no matching tool" claims).
    "transaction_structure": "transaction_structure",
    "transaction": "transaction_structure",
    "rent_to_own": "transaction_structure",
    "rent_then_purchase": "transaction_structure",
    "purchase_option": "transaction_structure",
    "lease_option": "transaction_structure",
    "sale_terms": "transaction_structure",
}

# Canonical categories that are listing facts (seller-provided data used as
# tool *input*), never an independent claim a tool checks. `"condition"` is
# deliberately excluded: its verifiability is driven entirely by
# `Claim.type` (subjective wording is never treated as a fact to feed a
# tool), not by category alone.
_LISTING_FACT_CATEGORIES: frozenset[CanonicalClaimCategory] = frozenset(
    {"rooms", "address", "size_sqm", "price"}
)

# Which tool(s) actually produce evidence for a *verifiable* canonical
# category. `geo_resolver` is listed first for both: it is always the
# dependency, never the primary claim-answering call.
TOOLS_BY_VERIFIABLE_CATEGORY: dict[CanonicalClaimCategory, tuple[ToolName, ...]] = {
    "nearby_amenities": ("geo_resolver", "nearby_services"),
    "price_comparison": ("geo_resolver", "price_signal"),
}

# Which tool(s) a *listing-fact* category feeds as input (informational
# only - used for the diagnostic runner's "proposed_tools" display, never
# to mark a listing fact as an independent claim to check).
LISTING_FACT_INPUT_TOOLS: dict[CanonicalClaimCategory, tuple[ToolName, ...]] = {
    "address": ("geo_resolver",),
    "price": ("price_signal",),
    "size_sqm": ("price_signal",),
    "rooms": (),
}


def normalize_category(raw_category: str | None) -> CanonicalClaimCategory | None:
    """Normalize a free-text `Claim.category` to a canonical category.

    Returns `None` for an absent/blank category, or the *lowercased,
    stripped* raw string unchanged if it isn't a recognized alias (callers
    fall back to legacy keyword matching for those).
    """
    if not raw_category:
        return None
    key = raw_category.strip().lower()
    return _CATEGORY_ALIASES.get(key)


def classify_claim(claim: Claim) -> ClaimClassification:
    """Classify `claim` into one of `ClaimClassification`.

    `claim.type == "subjective"` always wins (subjective wording is never
    treated as a checkable fact or claim, regardless of category).
    """
    if claim.type == "subjective":
        return "subjective"

    canonical = normalize_category(claim.category)
    if canonical in TOOLS_BY_VERIFIABLE_CATEGORY:
        return "verifiable"
    if canonical in _LISTING_FACT_CATEGORIES:
        return "listing_fact"
    return "unsupported"


# Canonical categories that describe commercial/legal transaction terms -
# materially important to the buyer, but never routed through a current
# tool (geo/nearby/price). Kept separate from `_LISTING_FACT_CATEGORIES`
# and `TOOLS_BY_VERIFIABLE_CATEGORY` on purpose: neither a "tool consumes
# this as input" fact nor a "a tool can verify this" claim.
_CONTRACT_REVIEW_CATEGORIES: frozenset[CanonicalClaimCategory] = frozenset(
    {"transaction_structure"}
)


def verification_decision(claim: Claim) -> VerificationDecision:
    """Classify `claim` into the Pre-Tool Triage's `VerificationDecision`
    vocabulary (see `docs/pre_tool_triage.md` §4).

    Distinct from (and more granular than) `classify_claim`/
    `ClaimClassification`, which only distinguishes what feeds/answers a
    tool call. This additionally separates:

    - `"REQUIRES_CONTRACT_REVIEW"` - commercial/legal transaction terms
      (e.g. rent-then-purchase) that no current tool checks, but that
      must never be silently folded into a generic "unsupported" bucket;
    - `"REQUIRES_SELLER_OR_DOCUMENT"` - a physical/document fact (safe
      room, floor, kitchen spec, future installation commitment) with no
      current tool, but still independently verifiable by asking the
      seller or reading a document/plan;
    - `"OUT_OF_SCOPE"` - a factual claim with no canonical category and no
      seller/document verification path either (rare; a genuine catch-all).

    Never derived from raw listing text, only from `claim.type` and its
    already-normalized canonical category - deterministic and reused by
    every consumer (report, diagnostics, Pre-Tool Triage) instead of each
    reimplementing its own bucket logic.
    """
    if claim.type == "subjective":
        return "SUBJECTIVE"

    canonical = normalize_category(claim.category)
    if canonical in TOOLS_BY_VERIFIABLE_CATEGORY:
        return "CHECKABLE_WITH_TOOL"
    if canonical in _LISTING_FACT_CATEGORIES:
        return "LISTING_FACT"
    if canonical in _CONTRACT_REVIEW_CATEGORIES:
        return "REQUIRES_CONTRACT_REVIEW"
    if canonical == "condition" or canonical is None:
        # A factual claim with no matching tool (safe room, floor,
        # upgraded kitchen/bathroom, future-installation commitments) is
        # still a real, checkable-by-other-means fact - never treat it as
        # equivalent to "out of scope".
        return "REQUIRES_SELLER_OR_DOCUMENT"
    return "OUT_OF_SCOPE"


__all__ = [
    "CanonicalClaimCategory",
    "ClaimClassification",
    "LISTING_FACT_INPUT_TOOLS",
    "TOOLS_BY_VERIFIABLE_CATEGORY",
    "VerificationDecision",
    "classify_claim",
    "normalize_category",
    "verification_decision",
]
