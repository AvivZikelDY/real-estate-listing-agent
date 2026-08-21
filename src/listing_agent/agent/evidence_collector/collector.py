"""The deterministic `evidence_collector` module.

Executes a validated `TriagePlan`'s tool calls, in order, through the
injected `ToolExecutor` interface. Never calls an LLM, never calls a tool
absent from the validated plan, and always continues past a failed tool
instead of aborting the run (fail-soft: one tool's failure never prevents
independent evidence from other tools). Deliberately unaware of *how* a
tool call is actually executed (in-process local tools, a future MCP
server, etc., including that layer's own timeout/retry policy) — see
`src/listing_agent/agent/tool_executor/`.

Every executed tool's `ToolExecutionResult` is converted into a
`ToolEvidence` entry through `agent.evidence_mapper.map_tool_evidence` (see
Phase 15) — that mapping layer owns turning typed tool outputs into
grounded observations, per-item limitations, and claim linkage; this
module owns only *which* tool call to make (dependency ordering,
input construction) and always producing exactly one evidence entry per
planned call.
"""

import time

from pydantic import ValidationError

from src.listing_agent.agent.evidence_mapper import (
    find_relevant_claim_id,
    find_relevant_claim_ids,
    map_tool_evidence,
)
from src.listing_agent.agent.schemas import ToolCall, ToolEvidence, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer, ToolTraceRecord
from src.listing_agent.agent.tool_executor import ToolExecutor
from src.listing_agent.agent.tool_input_builder import MissingDependencyError, build_tool_input
from src.listing_agent.tools.schemas import GeoResolverOutput

# Conservative default cap (seconds) on cumulative tool-execution wall-clock
# time across all planned calls in one request. Normally overridden by
# `Settings.max_total_tool_execution_seconds` (Phase 17); kept here as a
# safe fallback for direct callers/tests that don't pass one explicitly.
DEFAULT_MAX_TOTAL_TOOL_EXECUTION_SECONDS = 20.0


async def collect_evidence(
    plan: TriagePlan,
    tool_executor: ToolExecutor,
    tracer: StepTracer,
    *,
    max_total_tool_execution_seconds: float = DEFAULT_MAX_TOTAL_TOOL_EXECUTION_SECONDS,
    geo_output: GeoResolverOutput | None = None,
) -> tuple[list[ToolEvidence], list[str]]:
    """Execute `plan.tool_calls`, in order, and collect their evidence.

    Args:
        max_total_tool_execution_seconds: Cumulative wall-clock budget
            (Phase 17) for all *actual* tool calls in this request. Once
            exceeded, any remaining planned calls are recorded as
            `status="skipped"` (never attempted) instead of running - one
            slow tool call can never silently blow through an unbounded
            total request time.
        geo_output: A pre-resolved `GeoResolverOutput` to seed dependent
            tools with (conversation follow-up reuse). Used only when
            `plan.tool_calls` does not itself include a `geo_resolver` call
            this turn (e.g. it was reused from a stored conversation
            context) - a `geo_resolver` call actually present in the plan
            always produces its own fresh `geo_output`, overriding this
            seed. `None` (the default) preserves today's exact behavior.

    Returns:
        A `(tool_results, executed_tools)` pair. `tool_results` has one
        `ToolEvidence` entry per planned tool call (executed or not).
        `executed_tools` lists only the tool names actually invoked through
        `tool_executor` (excludes calls skipped because a dependency, e.g.
        resolved coordinates, was never produced, or because the total
        tool-execution time budget was already spent).
    """
    tool_results: list[ToolEvidence] = []
    executed_tools: list[str] = []
    # Only a *resolved* geo_resolver outcome provides usable coordinates
    # for dependent tools. "ambiguous" and "not_found" are valid, typed
    # business outcomes (see `tools/schemas.py`) but neither gives a
    # single answer a dependent tool could act on. If geo_resolver itself
    # technically failed (status="error"), `geo_output` is also `None`
    # here, so dependent tools are skipped exactly the same way - a failed
    # prerequisite never silently blocks the rest of the request, it only
    # prevents the specific tools that need its result.
    total_tool_execution_seconds = 0.0

    for call in plan.tool_calls:
        if total_tool_execution_seconds >= max_total_tool_execution_seconds:
            reason = (
                f"the total tool-execution time budget of "
                f"{max_total_tool_execution_seconds:.1f}s was already spent"
            )
            tool_results.append(_dependency_skipped_evidence(call, reason, plan))
            tracer.record_tool_trace(
                ToolTraceRecord(
                    tool=call.tool,
                    status="skipped",
                    duration_ms=0.0,
                    response_summary={"reason": tool_results[-1].reason},
                )
            )
            continue

        try:
            input_model = build_tool_input(call, plan.property_details, geo_output, plan.claims)
        except MissingDependencyError as exc:
            tool_results.append(_dependency_skipped_evidence(call, exc.reason, plan))
            tracer.record_tool_trace(
                ToolTraceRecord(
                    tool=call.tool,
                    status="skipped",
                    duration_ms=0.0,
                    response_summary={"reason": tool_results[-1].reason},
                )
            )
            continue
        except ValidationError as exc:
            # A malformed plan (e.g. a negative asking_price/sqm the planner
            # somehow produced, or an unsupported nearby_services category)
            # must never crash the pipeline — it is reported as a
            # structured, never-retryable tool failure instead.
            tool_results.append(
                ToolEvidence(
                    tool=call.tool,
                    status="error",
                    error=f"Invalid input for tool '{call.tool}': {exc}",
                    error_code="invalid_input",
                    claim_id=find_relevant_claim_id(call.tool, plan.claims, call),
                    claim_ids=find_relevant_claim_ids(call.tool, plan.claims, call),
                    limitations=[
                        f"{call.tool} could not run: the request was invalid for this listing/tool."
                    ],
                )
            )
            tracer.record_tool_trace(
                ToolTraceRecord(
                    tool=call.tool,
                    status="error",
                    duration_ms=0.0,
                    error_code="invalid_input",
                    response_summary={"error": tool_results[-1].error},
                )
            )
            continue
        except ValueError:  # pragma: no cover - plan_validator already rejects unknown tools
            tool_results.append(
                ToolEvidence(
                    tool=call.tool,
                    status="error",
                    error=f"Tool '{call.tool}' is not supported by evidence_collector.",
                    error_code="internal_error",
                )
            )
            continue

        start = time.perf_counter()
        result = await tool_executor.execute(call, input_model)
        elapsed_seconds = time.perf_counter() - start
        total_tool_execution_seconds += elapsed_seconds
        duration_ms = elapsed_seconds * 1000
        executed_tools.append(call.tool)

        if (
            call.tool == "geo_resolver"
            and result.status == "ok"
            and result.output is not None
            and result.output.outcome == "resolved"
        ):
            geo_output = result.output

        evidence = map_tool_evidence(call, result, plan.claims)
        tool_results.append(evidence)
        tracer.record_tool_trace(
            ToolTraceRecord(
                tool=call.tool,
                status=result.status,
                duration_ms=duration_ms,
                error_code=evidence.error_code,
                request_summary=input_model.model_dump(mode="json"),
                response_summary={
                    "reason": evidence.reason,
                    "error": evidence.error,
                    "observations": evidence.observations,
                },
            )
        )

    return tool_results, executed_tools


def _dependency_skipped_evidence(call: ToolCall, reason: str, plan: TriagePlan) -> ToolEvidence:
    """Build a structured evidence entry for a tool that never ran because a
    prerequisite (e.g. resolved coordinates, or the total tool-execution
    time budget) was not available.

    Recorded as `status="skipped"`, not `"error"`: the tool itself never
    failed - it was never attempted because a dependency wasn't met. This
    keeps "this specific tool broke" (status="error") distinct from "this
    tool was never going to be useful without X" (status="skipped"), so
    `report_writer` can describe each precisely instead of conflating them.
    """
    tool = call.tool
    return ToolEvidence(
        tool=tool,
        status="skipped",
        output=None,
        reason=f"Skipped because {reason}.",
        claim_id=find_relevant_claim_id(tool, plan.claims, call),
        claim_ids=find_relevant_claim_ids(tool, plan.claims, call),
        limitations=[f"{tool} skipped: {reason}."],
    )
