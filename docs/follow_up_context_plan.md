# Stateful Follow-Up Context & Evidence Reuse — Implemented Design

Status: **implemented** (simplified from the original 11-module framework
proposed in an earlier draft of this document). Baseline at the time this
feature was implemented: `ruff check .` clean, `pytest -q` → 508 passed, 1
skipped (before this feature); 546 passed, 1 skipped after.

Goal: let a buyer ask follow-up questions about the **same listing**
without repeating tool calls whose answers the agent already has, while
adding exactly zero new LLM calls and no new architecture layer.

---

## 1. Simplification: what this document originally proposed vs. what was built

An earlier draft of this plan proposed an 11-module caching framework
(`EvidenceRequirement`, `EvidenceReuseResolver`, `RelevantEvidenceSelector`,
a claim-relinking service, a general invalidation engine, a
`runtime_checkable` `ContextStore` Protocol, LRU infrastructure, etc.).
None of that was built. Instead, the feature is exactly **3 new modules**
under `src/listing_agent/agent/conversation/`:

| Module | Responsibility |
|---|---|
| `context.py` | `ConversationContext` (the entire per-conversation state), `InMemoryContextStore` (get/save/clear), all cache-key/fingerprint builders, and the compact follow-up context text builder. |
| `hydration.py` | Fill a follow-up turn's missing `PropertyDetails` fields from stored context; detect a listing change. |
| `cache.py` | `collect_evidence_with_context` — the cache-aware replacement for `evidence_collector.collect_evidence`, used only when a `conversation_id` was supplied. |

Deliberately **not** built (and why it wasn't needed):

- **`EvidenceRequirement`/`RelevantEvidenceSelector` frameworks** — a
  current turn's Evidence Bundle is naturally scoped to only the current
  turn's (reduced) tool calls, because `collect_evidence_with_context`
  only ever maps cached/fresh results for calls the current plan actually
  contains. No separate "what's relevant" service was needed.
- **A claim-relinking service** — a cached/reused typed `ToolExecutionResult`
  is simply re-run through the *existing*
  `evidence_mapper.map_tool_evidence(call, result, claims)` against the
  *current* turn's claims. Nothing about a cached result's claim linkage
  is ever persisted or needs "relinking".
- **A general invalidation engine** — the cache key is the tool name plus
  a canonical JSON rendering of the tool's own typed input's semantic
  fields (Phase 6). A changed `asking_price`/`area_sqm`/etc. produces a
  different key on its own; there is nothing to invalidate.
- **A `runtime_checkable` `ContextStore` Protocol** — `InMemoryContextStore`
  is a single concrete class (get/save/clear). There is exactly one
  implementation and no plan to swap it, so a Protocol would add
  indirection with no present benefit.
- **LRU/TTL infrastructure** — no eviction policy exists. Context lives
  for the life of the current in-memory process only (see §7).

---

## 2. Request flow (as implemented)

```
POST /api/execute {prompt, conversation_id?}
        │
        ▼
run_agent_pipeline (agent/orchestrator/orchestrator.py)
        │
        ├─ check_prompt_length                     (unchanged)
        │
        ├─ load stored ConversationContext (if conversation_id given)
        │
        ├─ build_compact_follow_up_context           agent/conversation/context.py
        │        (KNOWN PROPERTY / PREVIOUS BUYER QUESTION / PREVIOUS RELEVANT FOCUS)
        │
        ├─ run_triage_planner  (LLM #1, unchanged module)
        │        user_payload = {"prompt": ..., "follow_up_context": ...?}
        │
        ├─ hydrate_plan_from_context                 agent/conversation/hydration.py
        │        (fills only None PropertyDetails fields; detects listing change)
        │
        ├─ validate_plan                             (unchanged, + buyer_prompt kwarg — Phase 0)
        │
        ├─ collect_evidence_with_context              agent/conversation/cache.py
        │        (conversation_id present) OR
        │   collect_evidence                          agent/evidence_collector/collector.py
        │        (conversation_id absent — byte-identical original path)
        │
        ├─ record one "context_reuse" deterministic step (only when stateful)
        │
        ├─ build_evidence_bundle                      (unchanged)
        │
        ├─ run_report_writer  (LLM #2, unchanged module)
        │
        └─ save updated ConversationContext (only when stateful)
```

`ToolExecutor` (`LocalToolExecutor` today, a future `McpToolExecutor`)
never changes and never knows reuse exists — `collect_evidence_with_context`
still calls `tool_executor.execute(call, input_model)` for exactly the
calls it decided still need to run (see `tests/agent/conversation/test_tool_executor_boundary.py`).

---

## 3. `ConversationContext` (Phase 1)

```python
class ConversationContext(BaseModel):
    conversation_id: str
    property_details: PropertyDetails
    resolved_geo: GeoResolverOutput | None
    cached_geo: dict[str, GeoResolverOutput]        # key: geo_cache_key(...)
    cached_nearby: dict[str, CachedNearbyCategory]  # key: nearby_cache_key(...) — one per category
    cached_price: dict[str, PriceSignalOutput]      # key: price_cache_key(...)
    last_question: str | None
    last_focus: list[str]                           # e.g. ["park"], ["price"]
    turn_count: int
    updated_at: datetime
```

Deliberately **not** stored: the full conversation transcript, the report
text, prompts beyond `last_question`, vector embeddings, user profile,
provider secrets, arbitrary LLM history.

## 4. `InMemoryContextStore` + `conversation_id` (Phase 2)

A single process-local `dict[str, ConversationContext]` with `get`,
`save`, `clear`. No Redis, no persistence, no TTL, no distributed locks.

`ExecuteRequest.conversation_id: str | None = None` (max length 128,
blank/whitespace normalized to `None`). The `/api/execute` response shape
is unchanged — still exactly `{status, error, response, steps}`.

- `conversation_id` absent → exact original stateless behavior (no store
  lookup at all).
- `conversation_id` present, unknown id → treated as a brand-new
  conversation (never an error — no authentication, no ownership).
- Different `conversation_id` → never any reuse.

## 5. Compact follow-up context (Phase 3)

Built once, *before* `triage_planner` runs, only when a stored context
already has a `last_question`:

```
KNOWN PROPERTY:
address: ..., asking_price: ..., rooms: ..., sqm: ..., listing_type: ...

PREVIOUS BUYER QUESTION:
<verbatim previous prompt>

PREVIOUS RELEVANT FOCUS:
park
```

Passed as an additional `follow_up_context` key in `triage_planner`'s
`user_payload` (never replacing `prompt`, so a request without a
`conversation_id` produces the exact original `user_payload` shape). This
is the entire mechanism that lets "How far?" resolve to "the park
mentioned last turn" — no new LLM call, no transcript replay.

## 6. Hydration (Phase 4)

`hydrate_plan_from_context` fills only `None` `PropertyDetails` fields
(`raw_address`, `asking_price`, `rooms`, `sqm`, `listing_type`) from the
stored context; an explicitly-restated current value always wins. A
clearly different `raw_address` is detected as a listing change — the
plan is returned completely unhydrated and the orchestrator starts a
fresh `ConversationContext` for the rest of that turn, so a different
listing's evidence is never mixed in.

## 7. Cache keys (Phase 6) and what's cached (Phase 11)

`cache_key = tool_name + canonical_json(actual typed tool input's semantic fields)`:

- `geo_resolver`: `GeoResolverInput` (`address`, `city_hint`).
- `nearby_services`: one key **per category** — `{lat, lon, radius_m}` +
  category (Phase 8 partial reuse needs per-category granularity).
- `price_signal`: every semantic field of `PriceSignalInput` — a changed
  `asking_price`/`area_sqm`/etc. naturally produces a different key.

Only ever cached: a successfully **resolved** geo result; any successful
(`status="ok"`) nearby result, including an empty one; a price result with
outcome `compared` or `no_comparable_data`. Never cached: a technical
error, a skipped call (missing dependency), an ambiguous/not_found geo
result, or `insufficient_data` price outcome.

No TTL. **Context exists only for the life of the current in-memory
process** — an accepted, documented limitation for this university
project; a process restart clears all conversations.

## 8. Geo & nearby-partial reuse (Phase 7-9)

If the exact same `GeoResolverInput` was previously resolved, it is reused
and the dependent tools receive the reused coordinates exactly as if
`geo_resolver` had just run (via `evidence_collector.collect_evidence`'s
existing `geo_output` seed parameter).

For `nearby_services`, each requested category is looked up in the cache
independently. Categories already cached are never re-requested; the tool
is called with only the missing categories (or not called at all if every
requested category is already cached). The final `ToolEvidence` for this
turn combines the cached and freshly executed parts.

The pre-existing "unsupported nearby category" fan-out protection (the
Hapalmach 21 regression: `plan_validator` never synthesizes a
`nearby_services` call for a claim whose text resolves to zero supported
categories) is untouched by this feature and is re-verified by
`tests/agent/test_follow_up_multi_turn.py::test_scenario_5_...`.

## 9. Price reuse (Phase 10) and provenance (Phase 0)

For the exact same `PriceSignalInput`, the cached result is reused with no
new `price_signal` call. A changed price/size naturally misses the cache
and `price_signal` runs again — no separate invalidation logic.

Prerequisite fix (Phase 0, the only intentional semantic change outside
the new feature itself): `price_signal` is now allowed only when (A) the
current turn's raw buyer prompt contains explicit comparative-price intent
(a deterministic keyword scan, Hebrew+English), or (B) the plan already
carries a real, non-derived `price_comparison` claim. `buyer_goal ==
"price_validation"` alone (an LLM classification with no corroborating
evidence) is no longer sufficient — this closed a bug where a plain
listing with just an asking price and size could trigger `price_signal`
purely because the planner guessed that buyer goal.

## 10. Trace (Phase 13)

Exactly one new deterministic step, `context_reuse`, recorded only when a
`conversation_id` was supplied:

```json
{
  "conversation": "known",
  "stored_evidence_keys": 3,
  "listing_changed": false,
  "reused": ["geo_resolver", "nearby:park"],
  "executed": [],
  "tool_calls_saved": 2
}
```

The public `/api/execute` response contract is unchanged — this step is
internal (visible only via `StepTracer`, not surfaced in the four-field
response).

## 11. Multi-turn behavior (Definition of Done)

See `artifacts/follow-up-context/multi-turn-demo.json` and
`final-summary.md` for a concrete, generated 5-turn run:

| Turn | Question | Reused | Executed | Tool calls saved |
|---|---|---|---|---|
| 1 | יש פארק קרוב? | — | geo_resolver, nearby_services | 0 |
| 2 | כמה רחוק? | geo_resolver, nearby:park | — | 2 |
| 3 | ומה לגבי בתי ספר? | geo_resolver | nearby_services (school only) | 1 |
| 4 | האם המחיר הוגן? | geo_resolver | price_signal | 1 |
| 5 | ומה אם המחיר יהיה 4.4 מיליון? | geo_resolver | price_signal (new price) | 1 |

## 12. Non-goals (unchanged)

No Redis/Postgres/vector DB, no RAG, no full chat transcript storage, no
third LLM call, no new agent, no new MCP tool, no generic caching
framework, no distributed sessions, no authentication, no TTL framework,
no background cleanup workers. `Nir`'s tool implementations
(`geo_resolver`, `nearby_services`, `price_signal`) and their typed
contracts are completely unaware of, and unchanged by, this feature — see
`docs/nir_handoff.md`.
