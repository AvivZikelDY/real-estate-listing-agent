"""Minimal conversation state for stateful follow-up questions.

This is the *entire* state kept between turns of the same conversation:
the listing's `PropertyDetails`, the resolved `geo_resolver` output, and a
small set of already-collected tool results, keyed by a canonical cache
key built from the tool's own typed input (see `cache_key_for_*` below).

Deliberately NOT stored (see `docs/follow_up_context_plan.md`): the full
LLM transcript, the report text, prompts, raw provider payloads, any
secret, any user identity. Nothing here requires a database - this is
in-memory only and lives for the life of the current process (Phase 11:
no TTL, no persistence across a restart - an accepted, documented
limitation for this university project).
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.listing_agent.agent.schemas import PropertyDetails
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyPlace,
    NearbyServiceCategory,
    PriceSignalInput,
    PriceSignalOutput,
    ServiceCategorySummary,
)


def normalize_text(value: str | None) -> str:
    """Deterministic, case/whitespace-insensitive normalization used to
    build every fingerprint/cache key in this module. No LLM, no guessing."""
    if not value:
        return ""
    return " ".join(value.strip().casefold().split())


def listing_fingerprint(property_details: PropertyDetails) -> str:
    """The listing's identity, for detecting "the buyer is now asking about
    a different property" (Phase 4). Address is the only field that
    identifies *which* listing this is - price/size/rooms are attributes of
    that listing, not its identity."""
    return normalize_text(property_details.raw_address)


def _canonical_json(data: dict[str, Any]) -> str:
    """Stable, order-independent JSON rendering of a typed tool input's
    semantic fields - the actual cache-key payload (Phase 6). Never
    includes operational fields (request ids, timestamps): callers only
    ever pass the already-built typed `*Input` model's own `model_dump`."""
    return json.dumps(data, sort_keys=True, ensure_ascii=True)


def geo_cache_key(input_model: GeoResolverInput) -> str:
    """Canonical key for a `geo_resolver` call: every semantic field of
    `GeoResolverInput` (`address`, `city_hint`) - nothing else."""
    return f"geo_resolver:{_canonical_json(input_model.model_dump(mode='json'))}"


def nearby_cache_key(lat: float, lon: float, radius_m: int, category: str) -> str:
    """Canonical key for one *category* of a `nearby_services` call.

    One record per category (Phase 8 - partial reuse): `NearbyServicesInput`
    is otherwise identical across categories in the same call
    (`lat`/`lon`/`radius_m`), so those are the only other semantic fields.
    """
    payload = {"lat": lat, "lon": lon, "radius_m": radius_m}
    return f"nearby_services:{category}:{_canonical_json(payload)}"


def price_cache_key(input_model: PriceSignalInput) -> str:
    """Canonical key for a `price_signal` call: every semantic field of
    `PriceSignalInput`. A changed `asking_price`/`area_sqm`/etc. naturally
    produces a different key - no separate invalidation logic needed."""
    return f"price_signal:{_canonical_json(input_model.model_dump(mode='json'))}"


def build_compact_follow_up_context(context: "ConversationContext") -> str | None:
    """Build the small, structured "compact follow-up context" (Phase 3)
    shown to `triage_planner` *before* it plans the current turn, so a
    pronoun-only follow-up ("How far?") can be resolved deterministically
    to whichever listing/topic the conversation was actually about.

    Deliberately NOT the full transcript: only the current known
    `PropertyDetails`, the single previous buyer question, and the
    previous turn's topic/focus labels (e.g. `["park"]`, `["price"]`).
    Returns `None` when there is nothing yet to show (a brand-new
    conversation with no prior turn).
    """
    if context.last_question is None:
        return None

    details = context.property_details
    fact_parts = []
    if details.raw_address:
        fact_parts.append(f"address: {details.raw_address}")
    if details.asking_price is not None:
        fact_parts.append(f"asking_price: {details.asking_price}")
    if details.rooms is not None:
        fact_parts.append(f"rooms: {details.rooms}")
    if details.sqm is not None:
        fact_parts.append(f"sqm: {details.sqm}")
    if details.listing_type != "unknown":
        fact_parts.append(f"listing_type: {details.listing_type}")

    lines = ["KNOWN PROPERTY:", ", ".join(fact_parts) if fact_parts else "(no facts known yet)"]
    lines += ["", "PREVIOUS BUYER QUESTION:", context.last_question]
    if context.last_focus:
        lines += ["", "PREVIOUS RELEVANT FOCUS:", ", ".join(context.last_focus)]
    return "\n".join(lines)


def compute_turn_focus(tool_calls: list[Any]) -> list[str]:
    """Deterministic, best-effort "topic" labels for this turn (Phase 3),
    stored as `ConversationContext.last_focus` for the *next* turn's
    compact context. Reuses `tool_input_builder.resolve_nearby_categories`
    rather than re-implementing category detection."""
    # Imported lazily to avoid a hard import-time cycle between
    # `agent.conversation` and `agent.tool_input_builder`.
    from src.listing_agent.agent.tool_input_builder import resolve_nearby_categories

    focus: list[str] = []
    for call in tool_calls:
        if call.tool == "nearby_services":
            categories = resolve_nearby_categories(
                categories=call.arguments.get("categories"),
                query=call.arguments.get("query"),
            )
            focus.extend(categories or ["nearby"])
        elif call.tool == "price_signal":
            focus.append("price")
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    deduped = []
    for item in focus:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


class CachedNearbyCategory(BaseModel):
    """One reusable `nearby_services` category result."""

    category: NearbyServiceCategory
    places: list[NearbyPlace] = Field(default_factory=list)
    summary: ServiceCategorySummary | None = None
    evidence_quality: Literal["low", "medium", "high"] = "low"
    limitations: list[str] = Field(default_factory=list)


class ConversationContext(BaseModel):
    """All state kept for one conversation, across turns.

    Every `cached_*` entry is guaranteed (by `cache.py`, the only writer)
    to represent a genuinely reusable, successful result - see
    `docs/follow_up_context_plan.md` "What to cache".
    """

    conversation_id: str
    property_details: PropertyDetails = Field(default_factory=PropertyDetails)
    resolved_geo: GeoResolverOutput | None = None
    cached_geo: dict[str, GeoResolverOutput] = Field(default_factory=dict)
    cached_nearby: dict[str, CachedNearbyCategory] = Field(default_factory=dict)
    cached_price: dict[str, PriceSignalOutput] = Field(default_factory=dict)
    last_question: str | None = None
    last_focus: list[str] = Field(default_factory=list)
    turn_count: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InMemoryContextStore:
    """The simplest possible `ConversationContext` store: a process-local
    dict. No Redis, no persistence, no TTL, no distributed locking - see
    module docstring."""

    def __init__(self) -> None:
        self._contexts: dict[str, ConversationContext] = {}

    def get(self, conversation_id: str) -> ConversationContext | None:
        """Return the stored context, or `None` for an unknown/new id -
        never an error (no authentication, no ownership)."""
        return self._contexts.get(conversation_id)

    def save(self, context: ConversationContext) -> None:
        self._contexts[context.conversation_id] = context

    def clear(self, conversation_id: str) -> None:
        self._contexts.pop(conversation_id, None)


# Module-level singleton (FastAPI-overridable via dependency injection),
# matching the pattern already used by `tools/dependency.py` and
# `llm/client/dependency.py`.
_default_store = InMemoryContextStore()


def get_context_store() -> InMemoryContextStore:
    """Return the process-wide `InMemoryContextStore` singleton."""
    return _default_store


__all__ = [
    "CachedNearbyCategory",
    "ConversationContext",
    "InMemoryContextStore",
    "build_compact_follow_up_context",
    "compute_turn_focus",
    "geo_cache_key",
    "get_context_store",
    "listing_fingerprint",
    "nearby_cache_key",
    "normalize_text",
    "price_cache_key",
]
