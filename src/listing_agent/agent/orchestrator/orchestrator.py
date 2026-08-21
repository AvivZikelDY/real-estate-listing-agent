"""The `agent_orchestrator` — coordinates one triage run end-to-end.

Flow: `triage_planner -> plan_validator -> evidence_collector ->
evidence_bundle_builder -> report_writer`, returning an `AgentRunResult`
(the internal Evidence Bundle plus the final buyer-facing report text).

Depends only on the `ToolExecutor` interface for tool execution — never on
`ToolRegistry` or any concrete tool implementation directly — so the
transport backing tool calls (local in-process tools today, a future MCP
server) can change without touching this module.

Optionally stateful (`conversation_id` + `context_store`): when both are
supplied, a follow-up turn's missing `PropertyDetails` fields are hydrated
from the stored `ConversationContext`, and already-collected tool results
are reused instead of re-executed (see `agent.conversation`). When either
is omitted (the default), behavior is byte-for-byte identical to the
original stateless pipeline — no context lookup, no reuse, no new
deterministic step.
"""

import logging

from src.listing_agent.agent.conversation import (
    ConversationContext,
    InMemoryContextStore,
    build_compact_follow_up_context,
    collect_evidence_with_context,
    compute_turn_focus,
    hydrate_plan_from_context,
)
from src.listing_agent.agent.evidence_bundle_builder import build_evidence_bundle
from src.listing_agent.agent.evidence_collector import collect_evidence
from src.listing_agent.agent.module_names import (
    CONTEXT_REUSE,
    EVIDENCE_BUNDLE_BUILDER,
    PLAN_VALIDATOR,
)
from src.listing_agent.agent.plan_validator import PlanValidationError, validate_plan
from src.listing_agent.agent.request_limits import check_prompt_length
from src.listing_agent.agent.schemas import AgentRunResult
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import ToolExecutor
from src.listing_agent.config import Settings, get_settings
from src.listing_agent.llm.client.base import LLMClient
from src.listing_agent.llm.report_writer import run_report_writer
from src.listing_agent.llm.triage_planner import run_triage_planner

# Developer-only visibility into reuse decisions (Step 6 of the interactive
# demo): never part of the public `/api/execute` response - `_llm_steps` in
# `api/routes.py` only ever surfaces `type == "llm"` steps. Enable with
# `logging.basicConfig(level=logging.INFO)` (already on by default under
# `uvicorn --log-level info`) to see one line per turn.
_logger = logging.getLogger(__name__)


async def run_agent_pipeline(
    prompt: str,
    *,
    llm_client: LLMClient,
    tool_executor: ToolExecutor,
    tracer: StepTracer,
    settings: Settings | None = None,
    conversation_id: str | None = None,
    context_store: InMemoryContextStore | None = None,
) -> AgentRunResult:
    """Run the full triage pipeline and return the internal run result.

    Args:
        prompt: The complete user input, exactly as submitted.
        llm_client: Injected `LLMClient` used by `triage_planner` and
            `report_writer`.
        tool_executor: Injected `ToolExecutor` used by `evidence_collector`
            to run planned tool calls, regardless of transport.
        tracer: Injected `StepTracer` recording every step of this run.
        settings: Injected `Settings` (Phase 17) supplying the request's
            deterministic limits (`max_prompt_length`, `max_claims`,
            `max_tool_calls`, `max_total_tool_execution_seconds`).
            Defaults to `get_settings()`.
        conversation_id: Optional conversation id (Phase 2). `None` (the
            default) preserves the exact original stateless behavior: no
            context lookup, no reuse, no save.
        context_store: The `InMemoryContextStore` to load/save the
            conversation's context from/to. Only consulted when
            `conversation_id` is also supplied.

    Raises:
        RequestLimitError: if `prompt` exceeds `settings.max_prompt_length`
            - checked first, before any LLM call is made.
        PlanValidationError: if `triage_planner`'s plan cannot be validated
            (e.g. it references an unknown tool). The caller (the
            `/api/execute` route) is responsible for turning this into the
            `{status: "error", ...}` response shape.
    """
    settings = settings or get_settings()

    # Deterministic, pre-flight limit check - never an LLM call - applied
    # before the expensive `triage_planner` call so an oversized prompt
    # never wastes LLM budget.
    check_prompt_length(prompt, settings)

    # Phase 3: look up any stored context *before* planning, so a compact,
    # structured follow-up summary (never the full transcript) can be
    # shown to `triage_planner` - this is what lets a pronoun-only
    # follow-up ("How far?") get resolved to the right previous topic.
    stored_context: ConversationContext | None = None
    if conversation_id and context_store is not None:
        stored_context = context_store.get(conversation_id)

    follow_up_context = (
        build_compact_follow_up_context(stored_context) if stored_context is not None else None
    )
    plan = await run_triage_planner(prompt, llm_client, tracer, follow_up_context)

    context: ConversationContext | None = None
    listing_changed = False
    stored_evidence_keys = 0
    if conversation_id and context_store is not None:
        if stored_context is not None:
            plan, listing_changed = hydrate_plan_from_context(plan, stored_context)
            if listing_changed:
                # A clearly different listing: never mix in the previous
                # listing's evidence. Start this conversation id fresh.
                context = ConversationContext(conversation_id=conversation_id)
            else:
                context = stored_context
                stored_evidence_keys = (
                    len(context.cached_geo) + len(context.cached_nearby) + len(context.cached_price)
                )
        else:
            context = ConversationContext(conversation_id=conversation_id)

    try:
        validated_plan = validate_plan(
            plan,
            max_tool_calls=settings.max_tool_calls,
            max_claims=settings.max_claims,
            buyer_prompt=prompt,
        )
    except PlanValidationError as exc:
        tracer.record_deterministic_step(
            PLAN_VALIDATOR, status="error", detail={"errors": exc.errors}
        )
        raise

    tracer.record_deterministic_step(
        PLAN_VALIDATOR,
        status="ok",
        detail={"tool_calls": [call.tool for call in validated_plan.tool_calls]},
    )

    if context is not None:
        tool_results, executed_tools, reuse_detail = await collect_evidence_with_context(
            validated_plan,
            tool_executor,
            tracer,
            context,
            max_total_tool_execution_seconds=settings.max_total_tool_execution_seconds,
        )
        tracer.record_deterministic_step(
            CONTEXT_REUSE,
            status="ok",
            detail={
                "conversation": "new" if stored_evidence_keys == 0 else "known",
                "stored_evidence_keys": stored_evidence_keys,
                "listing_changed": listing_changed,
                **reuse_detail,
            },
        )
        _logger.info(
            "conversation_id=%s reused=%s executed=%s tool_calls_saved=%s",
            conversation_id,
            reuse_detail.get("reused"),
            reuse_detail.get("executed"),
            reuse_detail.get("tool_calls_saved"),
        )
        context.property_details = validated_plan.property_details
        context.last_question = prompt
        context.last_focus = compute_turn_focus(validated_plan.tool_calls)
        context.turn_count += 1
        context_store.save(context)  # type: ignore[union-attr]
    else:
        tool_results, executed_tools = await collect_evidence(
            validated_plan,
            tool_executor,
            tracer,
            max_total_tool_execution_seconds=settings.max_total_tool_execution_seconds,
        )

    bundle = build_evidence_bundle(
        prompt=prompt,
        plan=validated_plan,
        tool_results=tool_results,
        executed_tools=executed_tools,
    )
    tracer.record_deterministic_step(
        EVIDENCE_BUNDLE_BUILDER,
        status="ok",
        detail={"request_id": bundle.request_id, "executed_tools": bundle.executed_tools},
    )

    report = await run_report_writer(bundle, llm_client, tracer)

    return AgentRunResult(bundle=bundle, report=report)

