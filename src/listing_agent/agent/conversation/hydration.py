"""Hydrate a follow-up turn's `PropertyDetails` from stored conversation
context, and detect when the buyer has moved on to a different listing.

Deliberately tiny and deterministic (Phase 4): a follow-up turn omitting a
field (e.g. "כמה רחוק?" says nothing about price/sqm/address) must reuse
what the conversation already knows; a turn that *explicitly* restates a
value always wins over stored context; a turn about a clearly different
address must never silently mix evidence from the previous listing.
"""

from src.listing_agent.agent.conversation.context import ConversationContext, listing_fingerprint
from src.listing_agent.agent.schemas import PropertyDetails, TriagePlan

# The only fields ever hydrated from stored context - exactly
# `PropertyDetails`'s own fields, nothing derived or invented.
_HYDRATABLE_FIELDS: tuple[str, ...] = ("raw_address", "asking_price", "rooms", "sqm")


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
    """
    if context is None:
        return plan, False

    if is_listing_change(plan.property_details, context.property_details):
        return plan, True

    updates: dict[str, object] = {}
    for field in _HYDRATABLE_FIELDS:
        if getattr(plan.property_details, field) is None:
            stored_value = getattr(context.property_details, field)
            if stored_value is not None:
                updates[field] = stored_value

    if plan.property_details.listing_type == "unknown" and (
        context.property_details.listing_type != "unknown"
    ):
        updates["listing_type"] = context.property_details.listing_type

    if not updates:
        return plan, False

    hydrated_details = plan.property_details.model_copy(update=updates)
    return plan.model_copy(update={"property_details": hydrated_details}), False


__all__ = ["hydrate_plan_from_context", "is_listing_change"]
