"""Unit tests for `agent.conversation.hydration` (Phase 4): filling a
follow-up turn's missing `PropertyDetails` from stored context, and
detecting a listing change."""

from src.listing_agent.agent.conversation.context import ConversationContext
from src.listing_agent.agent.conversation.hydration import (
    hydrate_plan_from_context,
    is_listing_change,
)
from src.listing_agent.agent.schemas import PropertyDetails, TriagePlan

_KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"


def _plan(**details: object) -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(**details),
        buyer_goal="general_question",
        tool_calls=[],
    )


def test_hydrate_fills_only_missing_fields_from_context() -> None:
    stored = ConversationContext(
        conversation_id="c1",
        property_details=PropertyDetails(
            raw_address=_KNOWN_ADDRESS, asking_price=3_000_000, rooms=4, sqm=90
        ),
    )
    plan = _plan(raw_address=None, asking_price=None, rooms=None, sqm=None)

    hydrated, listing_changed = hydrate_plan_from_context(plan, stored)

    assert listing_changed is False
    assert hydrated.property_details.raw_address == _KNOWN_ADDRESS
    assert hydrated.property_details.asking_price == 3_000_000
    assert hydrated.property_details.rooms == 4
    assert hydrated.property_details.sqm == 90


def test_hydrate_never_overwrites_an_explicit_current_value() -> None:
    stored = ConversationContext(
        conversation_id="c1",
        property_details=PropertyDetails(raw_address=_KNOWN_ADDRESS, asking_price=3_000_000),
    )
    plan = _plan(raw_address=_KNOWN_ADDRESS, asking_price=4_400_000)

    hydrated, listing_changed = hydrate_plan_from_context(plan, stored)

    assert listing_changed is False
    assert hydrated.property_details.asking_price == 4_400_000


def test_hydrate_with_no_stored_context_is_a_no_op() -> None:
    plan = _plan(raw_address=None)
    hydrated, listing_changed = hydrate_plan_from_context(plan, None)
    assert hydrated is plan
    assert listing_changed is False


def test_is_listing_change_true_for_a_different_address() -> None:
    current = PropertyDetails(raw_address="Derech Hashalom 1, Tel Aviv")
    stored = PropertyDetails(raw_address=_KNOWN_ADDRESS)
    assert is_listing_change(current, stored) is True


def test_is_listing_change_false_when_current_turn_omits_the_address() -> None:
    """"No value stated" must never be mistaken for "a different
    listing"."""
    current = PropertyDetails(raw_address=None)
    stored = PropertyDetails(raw_address=_KNOWN_ADDRESS)
    assert is_listing_change(current, stored) is False


def test_hydrate_detects_listing_change_and_returns_plan_unhydrated() -> None:
    stored = ConversationContext(
        conversation_id="c1",
        property_details=PropertyDetails(
            raw_address=_KNOWN_ADDRESS, asking_price=3_000_000, rooms=4, sqm=90
        ),
    )
    plan = _plan(raw_address="Derech Hashalom 1, Tel Aviv", asking_price=None)

    hydrated, listing_changed = hydrate_plan_from_context(plan, stored)

    assert listing_changed is True
    # Never leak the previous listing's asking_price into the new address.
    assert hydrated.property_details.asking_price is None
    assert hydrated.property_details.raw_address == "Derech Hashalom 1, Tel Aviv"
