"""The `agent_orchestrator` — coordinates one triage run end-to-end.

Flow: `triage_planner -> plan_validator -> evidence_collector ->
evidence_bundle_builder -> report_writer`, returning an `AgentRunResult`
(the internal Evidence Bundle plus the final buyer-facing report text).

Depends only on the `ToolExecutor` interface for tool execution — never on
`ToolRegistry` or any concrete tool implementation directly — so the
transport backing tool calls (local in-process tools today, a future MCP
server) can change without touching this module.
"""

from src.listing_agent.agent.evidence_bundle_builder import build_evidence_bundle
from src.listing_agent.agent.evidence_collector import collect_evidence
from src.listing_agent.agent.module_names import EVIDENCE_BUNDLE_BUILDER, PLAN_VALIDATOR
from src.listing_agent.agent.plan_validator import PlanValidationError, validate_plan
from src.listing_agent.agent.request_limits import check_prompt_length
from src.listing_agent.agent.schemas import AgentRunResult
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import ToolExecutor
from src.listing_agent.config import Settings, get_settings
from src.listing_agent.llm.client.base import LLMClient
from src.listing_agent.llm.report_writer import run_report_writer
from src.listing_agent.llm.triage_planner import run_triage_planner


async def run_agent_pipeline(
    prompt: str,
    *,
    llm_client: LLMClient,
    tool_executor: ToolExecutor,
    tracer: StepTracer,
    settings: Settings | None = None,
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

    plan = await run_triage_planner(prompt, llm_client, tracer)

    try:
        validated_plan = validate_plan(
            plan,
            max_tool_calls=settings.max_tool_calls,
            max_claims=settings.max_claims,
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
