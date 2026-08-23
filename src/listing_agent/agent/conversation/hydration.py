"""Hydrate a follow-up turn's `PropertyDetails` from stored conversation
context, and detect when the buyer has moved on to a different listing.

Deliberately tiny and deterministic (Phase 4): a follow-up turn omitting a
field (e.g. "כמה רחוק?" says nothing about price/sqm/address) must reuse
what the conversation already knows; a turn that *explicitly* restates a
value always wins over stored context; a turn about a clearly different
address must never silently mix evidence from the previous listing.
"""

import re

from src.listing_agent.agent.claim_categories import normalize_category
from src.listing_agent.agent.conversation.context import ConversationContext, listing_fingerprint
from src.listing_agent.agent.schemas import Claim, PropertyDetails, TriagePlan

# The only fields ever hydrated from stored context - exactly
# `PropertyDetails`'s own fields, nothing derived or invented.
_HYDRATABLE_FIELDS: tuple[str, ...] = ("raw_address", "asking_price", "rooms", "sqm")

# Narrow, deterministic pattern for a claim that explicitly supplies a
# house number in a follow-up turn (e.g. "מספר הבית 120", "מספר בית 120",
# "בית מספר 120"). Never used to invent a number - only to read one the
# buyer already typed out as an "address"-category claim.
_HOUSE_NUMBER_CLAIM_PATTERN = re.compile(r"(?:מספר\s*(?:ה)?בית|בית\s*מספר)\s*(\d+)")


def is_listing_change(current: PropertyDetails, stored: PropertyDetails) -> bool:
    """Whether `current` (this turn's freshly-planned `PropertyDetails`)
    describes a different listing than `stored` (the conversation's known
    listing).

    A turn that says nothing about the address at all (`raw_address is
    None`) is never treated as a listing change - "no value stated" must
    never be mistaken for "a different listing" (the same rule Phase 7
    applies to price/size hydration).
    """
    if not stored.raw_address or not current.raw_address:
        return False
    return listing_fingerprint(current) != listing_fingerprint(stored)


def _is_house_number_completion(current: PropertyDetails, stored: PropertyDetails) -> bool:
    """Whether `current.raw_address`'s street segment is exactly
    `stored.raw_address`'s own (house-number-less) street with a house
    number appended - the current turn resolved the full address itself
    (e.g. the planner wrote out "<street> <number>, <city>" directly
    instead of leaving `raw_address` null), not a switch to a different
    listing.

    This is a purely structural check on the address text - it does not
    require an accompanying claim, because real `triage_planner` output
    for a data-completion turn does not reliably emit one (observed during
    live validation: the same house-number turn sometimes produces an
    "address"-category claim and sometimes produces none at all, while
    still resolving `raw_address` itself). Matching the exact stored
    street name plus only a trailing house number is deliberately narrow
    and safe: a genuinely different listing has a different street name,
    not the same name with digits appended (follow-up property-merge
    fix).
    """
    if not current.raw_address or not stored.raw_address:
        return False
    stored_street = stored.raw_address.split(",", 1)[0].strip()
    if not stored_street or re.search(r"\d", stored_street):
        return False
    current_street = current.raw_address.split(",", 1)[0].strip()
    return bool(re.fullmatch(rf"{re.escape(stored_street)}\s+\d+", current_street))


def _extract_house_number_from_claims(claims: list[Claim]) -> str | None:
    """Read an explicit house number the CURRENT turn supplied as an
    "address"-category claim (e.g. "מספר הבית 120"). Returns `None` when no
    such claim exists - never guessed, never derived from any other field."""
    for claim in claims:
        if normalize_category(claim.category) != "address":
            continue
        match = _HOUSE_NUMBER_CLAIM_PATTERN.search(claim.text)
        if match:
            return match.group(1)
    return None


def _complete_address_with_house_number(address: str, house_number: str) -> str | None:
    """Deterministically insert a newly-supplied house number into a
    previously-known "<street>, <city>" address that did not include one.

    Returns `None` (never fabricates a completed address) when the street
    segment already contains a digit (a house number may already be
    present - do not risk mangling it) or when the stored address has no
    usable street text at all. Preserves the existing "<street> <number>,
    <city>" convention used elsewhere in this codebase (e.g. address
    precedence in `triage_planner`) - never a blind string concatenation.
    """
    if not address or not address.strip():
        return None
    parts = address.split(",", 1)
    street = parts[0].strip()
    if not street or re.search(r"\d", street):
        return None
    if len(parts) > 1:
        rest = parts[1].strip()
        return f"{street} {house_number}, {rest}" if rest else f"{street} {house_number}"
    return f"{street} {house_number}"


def hydrate_plan_from_context(
    plan: TriagePlan, context: ConversationContext | None
) -> tuple[TriagePlan, bool]:
    """Fill this turn's `None` `PropertyDetails` fields from `context`.

    Returns `(possibly-hydrated plan, listing_changed)`. When
    `listing_changed` is `True`, the plan is returned completely
    unhydrated (a different listing's stored facts must never leak into
    this turn) and the caller is expected to start a fresh
    `ConversationContext` for the rest of this turn (see
    `agent.conversation.cache.apply_conversation_context`).

    When this turn hydrates `raw_address` purely from stored context (the
    current turn said nothing about the address at all) and also contains
    an explicit house-number claim (e.g. "מספר הבית 120" following up on a
    previously-incomplete street-only address), the hydrated address is
    deterministically completed with that house number rather than left
    exactly as it was before (follow-up property-merge fix) - current-turn
    explicit information always wins, it is simply merged into the known
    address instead of requiring the buyer to restate the whole thing.
    """
    if context is None:
        return plan, False

    if is_listing_change(
        plan.property_details, context.property_details
    ) and not _is_house_number_completion(plan.property_details, context.property_details):
        return plan, True

    updates: dict[str, object] = {}
    for field in _HYDRATABLE_FIELDS:
        if getattr(plan.property_details, field) is None:
            stored_value = getattr(context.property_details, field)
            if stored_value is not None:
                updates[field] = stored_value

    if "raw_address" in updates:
        house_number = _extract_house_number_from_claims(plan.claims)
        if house_number:
            completed = _complete_address_with_house_number(
                str(updates["raw_address"]), house_number
            )
            if completed:
                updates["raw_address"] = completed

    if plan.property_details.listing_type == "unknown" and (
        context.property_details.listing_type != "unknown"
    ):
        updates["listing_type"] = context.property_details.listing_type

    if not updates:
        return plan, False

    hydrated_details = plan.property_details.model_copy(update=updates)
    hydrated_missing_information = [
        field for field in plan.missing_information if field not in updates
    ]
    return (
        plan.model_copy(
            update={
                "property_details": hydrated_details,
                "missing_information": hydrated_missing_information,
            }
        ),
        False,
    )


__all__ = [
    "hydrate_plan_from_context",
    "is_listing_change",
]
