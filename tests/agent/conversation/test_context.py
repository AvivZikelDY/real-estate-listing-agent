"""Unit tests for `agent.conversation.context`: cache-key/fingerprint
determinism, the in-memory store, and the compact follow-up context text
(Phase 1, 2, 3, 6)."""

from src.listing_agent.agent.conversation.context import (
    ConversationContext,
    InMemoryContextStore,
    build_compact_follow_up_context,
    compute_turn_focus,
    geo_cache_key,
    get_context_store,
    listing_fingerprint,
    nearby_cache_key,
    normalize_text,
    price_cache_key,
)
from src.listing_agent.agent.schemas import PropertyDetails, ToolCall
from src.listing_agent.tools.schemas import GeoResolverInput, PriceSignalInput


def test_normalize_text_is_case_and_whitespace_insensitive() -> None:
    assert normalize_text("  HaYarkon   120 ") == normalize_text("hayarkon 120")


def test_listing_fingerprint_depends_only_on_address() -> None:
    a = PropertyDetails(raw_address="HaYarkon 120, Tel Aviv", asking_price=3_000_000)
    b = PropertyDetails(raw_address="HaYarkon 120, Tel Aviv", asking_price=4_000_000)
    assert listing_fingerprint(a) == listing_fingerprint(b)


def test_listing_fingerprint_differs_for_a_different_address() -> None:
    a = PropertyDetails(raw_address="HaYarkon 120, Tel Aviv")
    b = PropertyDetails(raw_address="Derech Hashalom 1, Tel Aviv")
    assert listing_fingerprint(a) != listing_fingerprint(b)


def test_geo_cache_key_is_deterministic_for_the_same_input() -> None:
    input_a = GeoResolverInput(address="HaYarkon 120, Tel Aviv", city_hint="Tel Aviv")
    input_b = GeoResolverInput(address="HaYarkon 120, Tel Aviv", city_hint="Tel Aviv")
    assert geo_cache_key(input_a) == geo_cache_key(input_b)


def test_geo_cache_key_differs_for_a_different_address() -> None:
    input_a = GeoResolverInput(address="HaYarkon 120, Tel Aviv")
    input_b = GeoResolverInput(address="Derech Hashalom 1, Tel Aviv")
    assert geo_cache_key(input_a) != geo_cache_key(input_b)


def test_nearby_cache_key_is_per_category() -> None:
    park_key = nearby_cache_key(32.08, 34.77, 500, "park")
    school_key = nearby_cache_key(32.08, 34.77, 500, "school")
    assert park_key != school_key


def test_nearby_cache_key_deterministic_for_same_location_and_category() -> None:
    key_a = nearby_cache_key(32.08, 34.77, 500, "park")
    key_b = nearby_cache_key(32.08, 34.77, 500, "park")
    assert key_a == key_b


def test_price_cache_key_changes_with_asking_price() -> None:
    """Phase 6: a changed semantic field naturally produces a new key - no
    separate invalidation logic needed."""
    base = PriceSignalInput(
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        listing_type="purchase",
        asking_price=4_700_000,
        area_sqm=90,
    )
    changed = base.model_copy(update={"asking_price": 4_400_000})
    assert price_cache_key(base) != price_cache_key(changed)


def test_price_cache_key_stable_for_identical_input() -> None:
    input_a = PriceSignalInput(
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        listing_type="purchase",
        asking_price=4_700_000,
        area_sqm=90,
    )
    input_b = input_a.model_copy()
    assert price_cache_key(input_a) == price_cache_key(input_b)


def test_in_memory_context_store_get_save_clear() -> None:
    store = InMemoryContextStore()
    assert store.get("conv-1") is None

    context = ConversationContext(conversation_id="conv-1")
    store.save(context)
    assert store.get("conv-1") is context

    store.clear("conv-1")
    assert store.get("conv-1") is None


def test_different_conversation_ids_never_share_state() -> None:
    store = InMemoryContextStore()
    store.save(ConversationContext(conversation_id="conv-1", last_question="A"))
    store.save(ConversationContext(conversation_id="conv-2", last_question="B"))

    assert store.get("conv-1").last_question == "A"
    assert store.get("conv-2").last_question == "B"


def test_get_context_store_returns_a_singleton() -> None:
    assert get_context_store() is get_context_store()


def test_build_compact_follow_up_context_returns_none_for_a_fresh_conversation() -> None:
    context = ConversationContext(conversation_id="conv-1")
    assert build_compact_follow_up_context(context) is None


def test_build_compact_follow_up_context_includes_property_question_and_focus() -> None:
    context = ConversationContext(
        conversation_id="conv-1",
        property_details=PropertyDetails(
            raw_address="HaYarkon 120, Tel Aviv", asking_price=3_000_000
        ),
        last_question="יש פארק קרוב?",
        last_focus=["park"],
    )
    text = build_compact_follow_up_context(context)
    assert text is not None
    assert "HaYarkon 120, Tel Aviv" in text
    assert "יש פארק קרוב?" in text
    assert "park" in text
    # Never the full transcript - just these three sections.
    assert text.count("PREVIOUS BUYER QUESTION") == 1


def test_compute_turn_focus_labels_nearby_categories() -> None:
    calls = [ToolCall(tool="nearby_services", arguments={"categories": ["park", "school"]})]
    assert compute_turn_focus(calls) == ["park", "school"]


def test_compute_turn_focus_labels_price() -> None:
    calls = [ToolCall(tool="price_signal", arguments={})]
    assert compute_turn_focus(calls) == ["price"]


def test_compute_turn_focus_ignores_geo_resolver_alone() -> None:
    calls = [ToolCall(tool="geo_resolver", arguments={})]
    assert compute_turn_focus(calls) == []
