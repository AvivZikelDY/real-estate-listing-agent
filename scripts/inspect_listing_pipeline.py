"""Diagnostic and evaluation runner for the listing-triage pipeline,
covering `triage_planner` through `evidence_bundle_builder` (optionally
executing the existing deterministic local tools), and stopping *before*
`report_writer`.

```text
Raw listing + optional user question
-> triage_planner                      (src.listing_agent.llm.triage_planner)
-> plan validation / dependency        (src.listing_agent.agent.plan_validator)
   handling / deduplication
-> claim classification                (src.listing_agent.agent.claim_categories)
-> [--stage planner] STOP before any tool call is ever made
-> [--stage evidence] LocalToolExecutor (mock tools, forced regardless of
   TOOLS_MODE, unless --live-tools opts in to whatever TOOLS_MODE selects -
   never an MCP server or an external provider either way)
-> evidence_collector
-> evidence_bundle_builder
-> STOP before report_writer
```

This script never imports/instantiates `McpToolExecutor` or `report_writer`,
and only reaches Nir's real tool implementations when `--live-tools` is
passed explicitly - it reuses only the production entry points:
`run_triage_planner`, `plan_validator.validate_plan`,
`agent.claim_categories`, `evidence_mapper`, `agent.tool_input_builder`,
`LocalToolExecutor`, `evidence_collector.collect_evidence`, and
`evidence_bundle_builder.build_evidence_bundle` - and reuses the claim
classification/report-building helpers already written for
`scripts/inspect_triage_plan.py` rather than re-implementing them.

Usage:
    python scripts/inspect_listing_pipeline.py --stage planner --scenario 1 --fake-llm
    python scripts/inspect_listing_pipeline.py --stage evidence --scenario all --fake-llm
    python scripts/inspect_listing_pipeline.py --stage planner --scenario 2 --real-llm
    python scripts/inspect_listing_pipeline.py --stage evidence \
        --listing "..." --question "..." --fake-llm --json
    python scripts/inspect_listing_pipeline.py --stage evidence --scenario 1 \
        --fake-llm --live-tools        # real govmap/Overpass calls, no LLM cost
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.inspect_triage_plan as _planner_diag  # noqa: E402
from scripts.listing_pipeline_scenarios import SCENARIOS, Scenario  # noqa: E402
from src.listing_agent.agent.evidence_bundle_builder import build_evidence_bundle  # noqa: E402
from src.listing_agent.agent.evidence_collector import collect_evidence  # noqa: E402
from src.listing_agent.agent.module_names import TRIAGE_PLANNER  # noqa: E402
from src.listing_agent.agent.plan_validator import (  # noqa: E402
    ALLOWED_TOOLS,
    PlanValidationError,
    validate_plan,
)
from src.listing_agent.agent.schemas import Claim, TriagePlan  # noqa: E402
from src.listing_agent.agent.step_tracer import StepTracer  # noqa: E402
from src.listing_agent.agent.tool_executor.local import LocalToolExecutor  # noqa: E402
from src.listing_agent.agent.tool_input_builder import (  # noqa: E402
    extract_nearby_categories_argument,
    extract_nearby_query_argument,
)
from src.listing_agent.config import Settings, get_settings  # noqa: E402
from src.listing_agent.llm.client.base import LLMClient  # noqa: E402
from src.listing_agent.llm.client.dependency import get_llm_client  # noqa: E402
from src.listing_agent.llm.client.fake import FakeLLMClient  # noqa: E402
from src.listing_agent.llm.triage_planner.planner import run_triage_planner  # noqa: E402
from src.listing_agent.tools.dependency import get_tool_registry  # noqa: E402

STAGES = ("planner", "evidence")


class _CliError(Exception):
    """Raised by CLI helpers to signal a clean, user-facing exit code.

    Caught only in `main()` and converted to a return code - never lets a
    plain `SystemExit` propagate out of `asyncio.run(...)` (which would
    otherwise surface as an uncaught exception to callers of `main()`,
    e.g. the test suite, instead of the intended process exit code).
    """

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"CLI exited with code {code}")


# ------------------------------------------------------------------------
# Combining listing + question into one production `prompt` string.
#
# `run_triage_planner(prompt, ...)` only ever accepts a single string (its
# schema is frozen - see `docs/contracts.md`); there is no separate
# "question" argument to the real production entry point. A user question
# is therefore appended to the raw listing behind a clear, fixed label the
# planner prompt already instructs it to treat as the buyer's goal, so a
# real LLM call still only exercises the existing, single `run_triage_planner`
# call - never a second LLM module or a redesigned contract.
# ------------------------------------------------------------------------
_QUESTION_LABEL = "שאלת המשתמש"


def _combine_prompt(listing: str, question: str | None) -> str:
    if not question:
        return listing
    return f"{listing}\n\n{_QUESTION_LABEL}: {question}"


# ------------------------------------------------------------------------
# Question analysis (diagnostic-only; deterministic; never an LLM call).
# ------------------------------------------------------------------------

_WALKING_TIME_PATTERN = re.compile(r"דקות הליכה|walking\s*time", re.IGNORECASE)
_QUALITY_PATTERN = re.compile(r"טובים|איכות|quality|good schools?", re.IGNORECASE)
_BUILDING_ATTRIBUTE_PATTERN = re.compile(r"מעלית|חניה|elevator|parking", re.IGNORECASE)

# Canonical categories relevant to each `BuyerGoal` - used only to explain
# *why* a listing claim was or wasn't inspected for the current question,
# never to change what `plan_validator`/`triage_planner` actually decided.
# `None` means "every listing claim may be relevant" (no narrowing).
_GOAL_RELEVANT_CATEGORIES: dict[str, frozenset[str] | None] = {
    "family_suitability": frozenset({"nearby_amenities", "address", "rooms", "size_sqm"}),
    "nearby_services_check": frozenset({"nearby_amenities", "address"}),
    "price_validation": frozenset({"price_comparison", "price", "size_sqm", "address"}),
    "general_question": None,
    "fast_claim_triage": None,
}


def _claim_source(claim: Claim, question: str | None) -> str:
    """`"user_question"` if `claim.text` was (near-)verbatim drawn from the
    question, `"listing"` otherwise. Purely a substring comparison against
    the original, unmodified input strings - never a re-derivation of
    planner behavior."""
    if question and claim.text.strip() and claim.text.strip() in question:
        return "user_question"
    return "listing"


def _question_answerability(
    question: str | None, buyer_goal: str, validated_plan: TriagePlan
) -> str:
    """Classify how answerable `question` is, given the validated plan.

    One of: `"not_applicable"` (no question), `"unsupported"`,
    `"missing_information"`, `"partially_verifiable"`, `"verifiable"`.
    """
    if not question:
        return "not_applicable"

    if _BUILDING_ATTRIBUTE_PATTERN.search(question) and not validated_plan.tool_calls:
        return "unsupported"
    if buyer_goal == "family_suitability":
        return "partially_verifiable"
    if _WALKING_TIME_PATTERN.search(question):
        return "partially_verifiable"
    if _QUALITY_PATTERN.search(question):
        return "partially_verifiable"
    if buyer_goal == "price_validation" and (
        validated_plan.property_details.asking_price is None
        or validated_plan.property_details.sqm is None
    ):
        return "missing_information"
    return "verifiable"


def _build_question_analysis(
    question: str | None,
    buyer_goal: str,
    validated_plan: TriagePlan,
) -> dict[str, Any]:
    if not question:
        return {"question": "none", "goal": "general_listing_triage"}

    claims = validated_plan.claims
    question_derived = [c for c in claims if _claim_source(c, question) == "user_question"]
    relevant_categories = _GOAL_RELEVANT_CATEGORIES.get(buyer_goal)

    def _is_relevant(claim: Claim) -> bool:
        if relevant_categories is None:
            return True
        return claim.category in relevant_categories

    listing_claims = [c for c in claims if _claim_source(c, question) == "listing"]
    relevant_listing_claims = [c for c in listing_claims if _is_relevant(c)]
    ignored_claims = [c for c in listing_claims if not _is_relevant(c)]

    return {
        "question": question,
        "goal": buyer_goal,
        "answerability": _question_answerability(question, buyer_goal, validated_plan),
        "question_derived_claims": [c.text for c in question_derived],
        "relevant_listing_claims": [c.text for c in relevant_listing_claims],
        "claims_ignored_not_relevant_to_question": [c.text for c in ignored_claims],
    }


# ------------------------------------------------------------------------
# Claim/tool-plan sections - reuse `scripts/inspect_triage_plan.py`'s
# existing helpers (never a second implementation of the same
# classification logic).
# ------------------------------------------------------------------------


def _build_identified_claims_and_not_sent(
    raw_plan: TriagePlan, validated_plan: TriagePlan, question: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    identified, not_sent = _planner_diag._build_claims_sections(raw_plan, validated_plan)

    claims_by_id = {c.id: c for c in validated_plan.claims}
    for entry in identified:
        claim = claims_by_id.get(entry["claim_id"])
        source = _claim_source(claim, question) if claim else "listing"
        entry["source"] = source
        entry["relevant_to_question"] = bool(question) and source == "user_question"

    for entry in not_sent:
        # A claim skipped purely because no relevant tool was ever
        # *proposed* by the planner ("out_of_scope") reads more precisely
        # as "irrelevant to the current question" once a question exists -
        # the underlying classification is unchanged.
        if question and entry["reason"] == "out_of_scope":
            entry["reason"] = "irrelevant_to_current_question"

    return identified, not_sent


# ------------------------------------------------------------------------
# Evidence-stage execution (mock local tools only).
# ------------------------------------------------------------------------


def _tool_executor(settings: Settings, *, live_tools: bool) -> tuple[LocalToolExecutor, str]:
    """Build the Evidence-stage executor and report which tools it runs.

    Defaults (`live_tools=False`) to a `LocalToolExecutor` backed by the
    deterministic mock tools regardless of the process's own `TOOLS_MODE`,
    so the diagnostic stays offline and reproducible by default.

    With `--live-tools` (`live_tools=True`) it instead honours the
    process's own `TOOLS_MODE`, which is the only way this script can
    observe Nir's real implementations end to end. That reaches live
    third-party HTTP APIs (govmap, Overpass) and is therefore always an
    explicit opt-in, never the default. `TOOL_BACKEND` is still ignored:
    this script only ever uses the in-process `LocalToolExecutor`, never
    `McpToolExecutor` or any external provider.
    """
    tools_mode = settings.tools_mode.strip().lower() if live_tools else "mock"
    executor_settings = settings.model_copy(update={"tools_mode": tools_mode})
    executor = LocalToolExecutor(
        get_tool_registry(executor_settings), timeout_seconds=settings.tool_timeout_seconds
    )
    return executor, tools_mode


def _sanitize_tool_results(tool_results: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for evidence in tool_results:
        entries.append(
            {
                "tool": evidence.tool,
                "status": evidence.status,
                "output": evidence.output,
                "error": evidence.error,
                "error_code": evidence.error_code,
                "reason": evidence.reason,
                "claim_ids": evidence.claim_ids,
                "observations": evidence.observations,
                "limitations": evidence.limitations,
            }
        )
    return entries


def _evidence_bundle_report(bundle: Any) -> dict[str, Any]:
    return {
        "request_id": bundle.request_id,
        "executed_tools": bundle.executed_tools,
        "missing_information": bundle.missing_information,
        "limitations": bundle.limitations,
        "tool_results": _sanitize_tool_results(bundle.tool_results),
    }


# ------------------------------------------------------------------------
# Raw-plan visibility + normalization/validation decisions (Phase 19).
#
# Both are purely presentational: they explain already-computed data by
# diffing the raw plan against the validated plan / prepared inputs -
# never a second implementation of `plan_validator` or
# `tool_input_builder` behavior. Never exposes hidden chain of thought,
# secrets, or raw HTTP details - only tool names, arguments, and claim ids
# already present in the (sanitized) plan objects.
# ------------------------------------------------------------------------


def _build_raw_planner_tool_calls(raw_plan: TriagePlan) -> list[dict[str, Any]]:
    """The planner's proposed tool calls exactly as returned, before any
    `plan_validator` normalization/synthesis/dropping is applied."""
    return [
        {
            "tool": call.tool,
            "arguments": call.arguments,
            "supports_claim_ids": call.supports_claim_ids,
        }
        for call in raw_plan.tool_calls
    ]


def _build_normalization_decisions(
    raw_plan: TriagePlan,
    validated_plan: TriagePlan,
    prepared_tool_inputs: list[dict[str, Any]],
) -> list[str]:
    """Deterministic, human-readable decisions explaining what changed
    between the raw planner output and the final validated/prepared plan
    (tool retained/dropped/synthesized, nearby category normalization)."""
    decisions: list[str] = []

    raw_tools = {call.tool for call in raw_plan.tool_calls}
    validated_tools = {call.tool for call in validated_plan.tool_calls}

    for tool in sorted(ALLOWED_TOOLS):
        in_raw = tool in raw_tools
        in_validated = tool in validated_tools

        if in_validated and not in_raw:
            call = next(c for c in validated_plan.tool_calls if c.tool == tool)
            claim_id = (call.supports_claim_ids or [None])[0]
            decisions.append(
                f"{tool} synthesized: a known verifiable claim ({claim_id}) had no "
                "proposed tool call in the raw plan."
            )
        elif in_raw and not in_validated:
            decisions.append(f"{tool} rejected: {_rejection_reason(tool, raw_plan)}")

    nearby_call = next((c for c in validated_plan.tool_calls if c.tool == "nearby_services"), None)
    nearby_prepared = next(
        (p for p in prepared_tool_inputs if p["tool_name"] == "nearby_services"), None
    )
    if nearby_call is not None and nearby_prepared is not None:
        resolved_categories = nearby_prepared["value"].get("categories")
        raw_categories = extract_nearby_categories_argument(nearby_call.arguments)
        raw_query = extract_nearby_query_argument(nearby_call.arguments)
        if raw_categories:
            decisions.append(f"nearby_services retained: explicit categories={raw_categories!r}.")
        elif raw_query:
            decisions.append(
                f"nearby_services retained: query={raw_query!r} normalized to "
                f"categories={resolved_categories!r}."
            )
        elif (
            resolved_categories
            and resolved_categories != _planner_diag._NEARBY_SERVICES_DEFAULT_CATEGORIES
        ):
            decisions.append(
                "nearby_services retained: categories derived from the linked claim's "
                f"text -> categories={resolved_categories!r}."
            )

    if not decisions:
        decisions.append("No normalization or validation changes were needed.")

    return decisions


def _rejection_reason(tool: str, raw_plan: TriagePlan) -> str:
    """A concise, deterministic (never fabricated) reason a raw-proposed
    tool call did not survive to the final validated plan."""
    property_details = raw_plan.property_details
    if tool == "price_signal":
        if property_details.listing_type == "rental":
            return "price_signal is forbidden for rental listings."
        if property_details.asking_price is None or property_details.sqm is None:
            return "missing asking_price or sqm after argument normalization."
        return "dropped by plan validation (e.g. capped at max_tool_calls)."
    if tool in ("nearby_services", "geo_resolver"):
        if not property_details.raw_address:
            return "no usable address to resolve."
        return "dropped by plan validation (e.g. capped at max_tool_calls)."
    return "dropped by plan validation."


# ------------------------------------------------------------------------
# Top-level per-scenario diagnostic report.
# ------------------------------------------------------------------------


async def build_pipeline_report(
    listing: str,
    question: str | None,
    client: LLMClient,
    tracer: StepTracer,
    settings: Settings,
    llm_mode: str,
    stage: str,
    live_tools: bool = False,
) -> dict[str, Any]:
    """Run the real pipeline for one (listing, question) pair, stopping
    before tool execution (`stage="planner"`) or before `report_writer`
    (`stage="evidence"`). Never calls `report_writer`."""
    prompt = _combine_prompt(listing, question)

    start = time.perf_counter()
    raw_plan = await run_triage_planner(prompt, client, tracer)
    planner_duration_ms = (time.perf_counter() - start) * 1000

    diagnostics: dict[str, Any] = {
        "llm_mode": llm_mode,
        "llm_calls": sum(1 for step in tracer.get_steps() if step.type == "llm"),
        "planner_duration_ms": round(planner_duration_ms, 2),
        "planned_tool_calls_before_deduplication": len(raw_plan.tool_calls),
        # Which implementations the Evidence stage would use. Forced to
        # "mock" unless --live-tools was passed; see `_tool_executor`. In
        # `--stage planner` no tool ever runs, so `tools_executed` stays
        # empty regardless of what this reports.
        "tools_mode": settings.tools_mode.strip().lower() if live_tools else "mock",
        "mcp_called": False,
        "external_provider_called": False,
        "report_writer_called": False,
    }

    try:
        validated_plan = validate_plan(
            raw_plan, max_tool_calls=settings.max_tool_calls, max_claims=settings.max_claims
        )
    except PlanValidationError as exc:
        diagnostics["validation_status"] = "rejected"
        diagnostics["validation_errors"] = list(exc.errors)
        diagnostics["planned_tool_calls_after_deduplication"] = 0
        diagnostics["tools_executed"] = []
        diagnostics["evidence_bundle_item_count"] = 0
        diagnostics["total_pipeline_duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        return {
            "input": {
                "listing": listing,
                "question": question,
                "llm_mode": llm_mode,
                "stage": stage,
            },
            "question_analysis": {"question": question or "none", "goal": "rejected"},
            "listing_facts": raw_plan.property_details.model_dump(mode="json"),
            "identified_claims": [],
            "claims_not_sent_to_tools": [],
            "raw_planner_tool_calls": _build_raw_planner_tool_calls(raw_plan),
            "normalization_decisions": [],
            "final_tool_plan": [],
            "prepared_tool_inputs": [],
            "tool_results": [],
            "evidence_bundle": None,
            "diagnostics": diagnostics,
        }

    diagnostics["validation_status"] = "accepted"
    diagnostics["planned_tool_calls_after_deduplication"] = len(validated_plan.tool_calls)

    question_analysis = _build_question_analysis(
        question, validated_plan.buyer_goal, validated_plan
    )
    identified_claims, claims_not_sent = _build_identified_claims_and_not_sent(
        raw_plan, validated_plan, question
    )
    final_tool_plan, call_id_by_tool = _planner_diag._build_final_tool_plan(
        raw_plan, validated_plan
    )
    prepared_tool_inputs = _planner_diag._build_prepared_tool_inputs(
        validated_plan, call_id_by_tool
    )
    raw_planner_tool_calls = _build_raw_planner_tool_calls(raw_plan)
    normalization_decisions = _build_normalization_decisions(
        raw_plan, validated_plan, prepared_tool_inputs
    )

    tool_results: list[dict[str, Any]] = []
    evidence_bundle: dict[str, Any] | None = None
    diagnostics["tools_executed"] = []
    diagnostics["evidence_bundle_item_count"] = 0

    if stage == "evidence":
        executor, diagnostics["tools_mode"] = _tool_executor(settings, live_tools=live_tools)
        raw_tool_results, executed_tools = await collect_evidence(
            validated_plan,
            executor,
            tracer,
            max_total_tool_execution_seconds=settings.max_total_tool_execution_seconds,
        )
        bundle = build_evidence_bundle(
            prompt=prompt,
            plan=validated_plan,
            tool_results=raw_tool_results,
            executed_tools=executed_tools,
        )
        tool_results = _sanitize_tool_results(raw_tool_results)
        evidence_bundle = _evidence_bundle_report(bundle)
        diagnostics["tools_executed"] = executed_tools
        diagnostics["evidence_bundle_item_count"] = len(bundle.tool_results)

    diagnostics["total_pipeline_duration_ms"] = round((time.perf_counter() - start) * 1000, 2)

    return {
        "input": {
            "listing": listing,
            "question": question,
            "llm_mode": llm_mode,
            "stage": stage,
        },
        "question_analysis": question_analysis,
        "listing_facts": validated_plan.property_details.model_dump(mode="json"),
        "identified_claims": identified_claims,
        "claims_not_sent_to_tools": claims_not_sent,
        "raw_planner_tool_calls": raw_planner_tool_calls,
        "normalization_decisions": normalization_decisions,
        "final_tool_plan": final_tool_plan,
        "prepared_tool_inputs": prepared_tool_inputs,
        "tool_results": tool_results,
        "evidence_bundle": evidence_bundle,
        "diagnostics": diagnostics,
    }


# ------------------------------------------------------------------------
# Scenario-level invariant checks (used by `--scenario all`'s summary and
# by the offline test suite).
# ------------------------------------------------------------------------


def _check_invariants(scenario: Scenario, report: dict[str, Any]) -> list[str]:
    """Return a list of failed invariant descriptions (empty = all passed)."""
    failures: list[str] = []

    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    for tool in scenario.expected_tools:
        if tool not in planned_tools:
            failures.append(f"expected tool '{tool}' missing from final plan")
    for tool in scenario.forbidden_tools:
        if tool in planned_tools:
            failures.append(f"forbidden tool '{tool}' present in final plan")
        if tool in report["diagnostics"].get("tools_executed", []):
            failures.append(f"forbidden tool '{tool}' was actually executed")

    if report["diagnostics"].get("report_writer_called") is not False:
        failures.append("report_writer_called must always be False")

    return failures


# ------------------------------------------------------------------------
# Text rendering.
# ------------------------------------------------------------------------


def render_text(scenario_id: str, report: dict[str, Any]) -> str:
    lines: list[str] = []

    lines.append(f"=== SCENARIO {scenario_id} ===")
    lines.append("")
    lines.append("1. INPUT")
    lines.append("-" * 7)
    lines.append(f"listing: {report['input']['listing']}")
    lines.append(f"question: {report['input']['question'] or 'none'}")
    lines.append(f"llm_mode: {report['input']['llm_mode']}")
    lines.append(f"stage: {report['input']['stage']}")
    lines.append("")

    lines.append("2. USER QUESTION ANALYSIS")
    lines.append("-" * 25)
    qa = report["question_analysis"]
    for key, value in qa.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("")

    lines.append("3. EXTRACTED LISTING FACTS")
    lines.append("-" * 26)
    for key, value in report["listing_facts"].items():
        lines.append(f"{key}: {value}")
    lines.append("")

    lines.append("4. IDENTIFIED CLAIMS")
    lines.append("-" * 20)
    for claim in report["identified_claims"]:
        lines.append(f"claim_id: {claim['claim_id']}")
        lines.append(f"text: {claim['text']}")
        lines.append(f"type: {claim['type']}")
        lines.append(f"category: {claim['category']}")
        lines.append(f"source: {claim['source']}")
        lines.append(f"verifiability: {claim['verifiability']}")
        lines.append(f"relevant_to_question: {claim['relevant_to_question']}")
        lines.append("proposed_tools:")
        for tool in claim["proposed_tools"]:
            lines.append(f"  - {tool}")
        lines.append("")

    lines.append("5. CLAIMS NOT SENT TO TOOLS")
    lines.append("-" * 27)
    if not report["claims_not_sent_to_tools"]:
        lines.append("(none)")
    for claim in report["claims_not_sent_to_tools"]:
        lines.append(f"claim_id: {claim['claim_id']}")
        lines.append(f"text: {claim['text']}")
        lines.append(f"reason: {claim['reason']}")
        lines.append("")

    lines.append("6. RAW PLANNER TOOL CALLS")
    lines.append("-" * 25)
    if not report["raw_planner_tool_calls"]:
        lines.append("(the planner proposed no tool calls)")
    for call in report["raw_planner_tool_calls"]:
        lines.append(f"tool: {call['tool']}")
        lines.append(f"arguments: {call['arguments']}")
        lines.append(f"supports_claim_ids: {call['supports_claim_ids']}")
        lines.append("")

    lines.append("7. NORMALIZATION AND VALIDATION DECISIONS")
    lines.append("-" * 41)
    for decision in report["normalization_decisions"]:
        lines.append(f"- {decision}")
    lines.append("")

    lines.append("8. FINAL VALIDATED TOOL PLAN")
    lines.append("-" * 28)
    if not report["final_tool_plan"]:
        lines.append("(no tool calls in the final validated plan)")
    for entry in report["final_tool_plan"]:
        lines.append(f"call_id: {entry['call_id']}")
        lines.append(f"tool_name: {entry['tool_name']}")
        lines.append(f"supports_claim_ids: {entry['supports_claim_ids']}")
        lines.append(f"depends_on: {entry['depends_on']}")
        lines.append(f"execution_stage: {entry['execution_stage']}")
        lines.append(f"merged_from_duplicates: {entry['merged_from_duplicates']}")
        lines.append(f"planner_arguments: {entry['planner_arguments']}")
        lines.append("")

    lines.append("9. PREPARED TOOL INPUTS")
    lines.append("-" * 23)
    for entry in report["prepared_tool_inputs"]:
        lines.append(f"{entry['tool_name']} ({entry['call_id']}):")
        if not entry["resolved"]:
            lines.append(f"  # unresolved dependency: {entry['unresolved_dependency_reason']}")
        lines.append(json.dumps(entry["value"], ensure_ascii=False, indent=2))
        lines.append("")

    if report["input"]["stage"] == "evidence":
        # Labelled with the mode actually used, so a --live-tools run is
        # never mistaken for a mock one when reading a saved report.
        heading = f"10. TOOL EXECUTION RESULTS ({report['diagnostics']['tools_mode']} tools)"
        lines.append(heading)
        lines.append("-" * len(heading))
        if not report["tool_results"]:
            lines.append("(no tool calls executed)")
        for result in report["tool_results"]:
            lines.append(json.dumps(result, ensure_ascii=False, indent=2))
        lines.append("")

        lines.append("11. EVIDENCE BUNDLE")
        lines.append("-" * 19)
        if report["evidence_bundle"] is not None:
            lines.append(json.dumps(report["evidence_bundle"], ensure_ascii=False, indent=2))
        lines.append("")

    lines.append("12. DIAGNOSTICS")
    lines.append("-" * 14)
    for key, value in report["diagnostics"].items():
        lines.append(f"{key}: {value}")

    return "\n".join(lines)


def render_summary_table(rows: list[dict[str, Any]]) -> str:
    lines = ["SCENARIO SUMMARY", "-" * 16]
    lines.append("ID | Goal | Claims | Planned Tools | Executed Tools | Answerability | Result")
    for row in rows:
        lines.append(
            f"{row['id']} | {row['goal']} | {row['claims']} | "
            f"{','.join(row['planned_tools'])} | {','.join(row['executed_tools'])} | "
            f"{row['answerability']} | {row['result']}"
        )
    failed = [row for row in rows if row["result"] != "PASS"]
    if failed:
        lines.append("")
        lines.append("FAILED INVARIANTS")
        lines.append("-" * 17)
        for row in failed:
            for failure in row["failures"]:
                lines.append(f"scenario {row['id']}: {failure}")
    return "\n".join(lines)


# ------------------------------------------------------------------------
# CLI.
# ------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostic and evaluation runner for the listing-triage pipeline, "
            "covering triage_planner through evidence_bundle_builder (optionally "
            "executing the existing deterministic local tools), stopping before "
            "report_writer."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--listing", type=str, help="Raw listing text.")
    source.add_argument("--file", type=str, help="Path to a file containing the listing text.")
    source.add_argument(
        "--scenario",
        type=str,
        help="Scenario id (1-10) or 'all' to use a built-in scenario instead of --listing/--file.",
    )
    parser.add_argument("--question", type=str, default=None, help="Optional user question.")

    parser.add_argument(
        "--stage", choices=STAGES, required=True, help="Which pipeline stage to run."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--real-llm",
        action="store_true",
        help="Call the real LLMod.ai model through the existing LLModClient.",
    )
    mode.add_argument(
        "--fake-llm",
        action="store_true",
        help="Use FakeLLMClient with a pre-built plan. Requires --scenario.",
    )

    parser.add_argument(
        "--live-tools",
        action="store_true",
        help=(
            "Run --stage evidence against the tools selected by TOOLS_MODE instead of "
            "always forcing the mocks. With TOOLS_MODE=nir this makes real HTTP calls "
            "to govmap and Overpass, so a run can take tens of seconds."
        ),
    )

    parser.add_argument("--json", action="store_true", help="Emit one JSON document instead.")
    parser.add_argument(
        "--allow-paid-batch",
        action="store_true",
        help="Required to run --scenario all with --real-llm (multiple paid LLM calls).",
    )
    return parser.parse_args(argv)


def _resolve_scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.scenario is None:
        return []
    if args.scenario == "all":
        return [SCENARIOS[key] for key in sorted(SCENARIOS)]
    try:
        scenario_id = int(args.scenario)
    except ValueError:
        print(f"error: invalid --scenario value '{args.scenario}'", file=sys.stderr)
        raise _CliError(2) from None
    if scenario_id not in SCENARIOS:
        print(
            f"error: unknown scenario {scenario_id}. Valid ids: {sorted(SCENARIOS)}",
            file=sys.stderr,
        )
        raise _CliError(2)
    return [SCENARIOS[scenario_id]]


def _build_client_for_scenario(
    scenario: Scenario | None, args: argparse.Namespace, settings: Settings, tracer: StepTracer
) -> tuple[LLMClient, str]:
    if args.fake_llm:
        if scenario is None:
            print(
                "error: --fake-llm requires --scenario {1..10,all}: FakeLLMClient only "
                "replays pre-built plans, it cannot plan arbitrary --listing/--file/--question "
                "text without a real LLM call.",
                file=sys.stderr,
            )
            raise _CliError(2)
        client: LLMClient = FakeLLMClient()
        client.set_response(TRIAGE_PLANNER, scenario.plan)
        return client, "fake"

    real_settings = settings.model_copy(update={"llm_mode": "llmod"})
    try:
        client = get_llm_client(real_settings, step_tracer=tracer)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise _CliError(2) from None
    return client, "real"


async def _run_one(
    scenario: Scenario | None,
    listing: str,
    question: str | None,
    args: argparse.Namespace,
    settings: Settings,
) -> dict[str, Any]:
    tracer = StepTracer()
    client, llm_mode = _build_client_for_scenario(scenario, args, settings, tracer)
    return await build_pipeline_report(
        listing,
        question,
        client,
        tracer,
        settings,
        llm_mode,
        args.stage,
        live_tools=args.live_tools,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    try:
        scenarios = _resolve_scenarios(args)

        if args.real_llm and args.scenario == "all" and not args.allow_paid_batch:
            print(
                "error: --scenario all with --real-llm requires --allow-paid-batch "
                "(multiple paid LLM calls). Run one scenario at a time otherwise.",
                file=sys.stderr,
            )
            return 2

        reports: list[tuple[str, dict[str, Any]]] = []

        if scenarios:
            for scenario in scenarios:
                report = asyncio.run(
                    _run_one(scenario, scenario.listing, scenario.question, args, settings)
                )
                reports.append((str(scenario.id), report))
        else:
            if args.fake_llm:
                print(
                    "error: --fake-llm requires --scenario {1..10,all}.",
                    file=sys.stderr,
                )
                return 2
            listing = (
                Path(args.file).read_text(encoding="utf-8").strip() if args.file else args.listing
            )
            report = asyncio.run(_run_one(None, listing, args.question, args, settings))
            reports.append(("custom", report))
    except _CliError as exc:
        return exc.code

    if args.json:
        document = {
            "run_configuration": {
                "stage": args.stage,
                "llm_mode": reports[0][1]["input"]["llm_mode"],
            },
            "scenarios": [
                {"scenario_id": scenario_id, **report} for scenario_id, report in reports
            ],
        }
        if len(reports) > 1:
            document["summary"] = _build_summary_rows(scenarios, reports)
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0

    for scenario_id, report in reports:
        print(render_text(scenario_id, report))
        print()

    if len(reports) > 1:
        summary_rows = _build_summary_rows(scenarios, reports)
        print(render_summary_table(summary_rows))

    return 0


def _build_summary_rows(
    scenarios: list[Scenario], reports: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scenario, (_, report) in zip(scenarios, reports, strict=True):
        failures = _check_invariants(scenario, report)
        rows.append(
            {
                "id": scenario.id,
                "goal": report["question_analysis"].get("goal", "?"),
                "claims": len(report["identified_claims"]),
                "planned_tools": [entry["tool_name"] for entry in report["final_tool_plan"]],
                "executed_tools": report["diagnostics"].get("tools_executed", []),
                "answerability": report["question_analysis"].get("answerability", "n/a"),
                "result": "PASS" if not failures else "FAIL",
                "failures": failures,
            }
        )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
