"""Conversation context & evidence-reuse (stateful follow-up questions).

See `docs/follow_up_context_plan.md` for the design. Three modules:

- `context`: `ConversationContext`, `InMemoryContextStore`, cache-key
  builders.
- `hydration`: fill a follow-up turn's missing `PropertyDetails` from
  stored context; detect a listing change.
- `cache`: `collect_evidence_with_context` - the cache-aware replacement
  for `evidence_collector.collect_evidence`, used only when a
  `conversation_id` was supplied.
"""

from src.listing_agent.agent.conversation.cache import collect_evidence_with_context
from src.listing_agent.agent.conversation.context import (
    CachedNearbyCategory,
    ConversationContext,
    InMemoryContextStore,
    build_compact_follow_up_context,
    compute_turn_focus,
    get_context_store,
)
from src.listing_agent.agent.conversation.hydration import (
    hydrate_plan_from_context,
    is_listing_change,
)

__all__ = [
    "CachedNearbyCategory",
    "ConversationContext",
    "InMemoryContextStore",
    "build_compact_follow_up_context",
    "collect_evidence_with_context",
    "compute_turn_focus",
    "get_context_store",
    "hydrate_plan_from_context",
    "is_listing_change",
]
