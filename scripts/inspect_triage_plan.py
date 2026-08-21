"""Diagnostic runner for the real `triage_planner` pipeline, stopping
*before* any tool is ever executed.

```text
Raw listing
-> triage_planner            (src.listing_agent.llm.triage_planner.planner)
-> plan validation            (src.listing_agent.agent.plan_validator)
-> dependency resolution       )  both already performed by
-> tool-call deduplication      )  plan_validator.validate_plan()
-> final prepared tool inputs  (src.listing_agent.agent.tool_input_builder)
-> STOP before ToolExecutor.execute()
```

This script never imports/instantiates `LocalToolExecutor`, `McpToolExecutor`,
any mock/real tool, `evidence_collector`, `evidence_bundle_builder`, or
`report_writer` - it reuses only the production `triage_planner` entry
point, the production `LLMClient` implementations (`FakeLLMClient` /
`LLModClient`, selected through the existing DI mechanism), the production
`plan_validator.validate_plan`, and the production
`agent.tool_input_builder.build_tool_input` (the same helper
`evidence_collector` itself uses to prepare a tool's input) - so the
prepared inputs shown here are never a re-implementation that could drift
from what the real pipeline would actually send.

Usage:
    python scripts/inspect_triage_plan.py --fake-llm --example 1
    python scripts/inspect_triage_plan.py --real-llm --listing "..."
    python scripts/inspect_triage_plan.py --real-llm --file examples/listing.txt --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow running as a plain script (`python scripts/inspect_triage_plan.py`)
# from the repo root, matching the existing `generate_architecture_image.py`
# convention.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.triage_plan_examples import EXAMPLE_LISTINGS, EXAMPLE_PLANS  # noqa: E402
from src.listing_agent.agent.claim_categories import (  # noqa: E402
    TOOLS_BY_VERIFIABLE_CATEGORY,
    classify_claim,
    normalize_category,
)
from src.listing_agent.agent.evidence_mapper import (  # noqa: E402
    find_relevant_claim_ids,
    relevant_tools_for_claim,
)
from src.listing_agent.agent.plan_validator import (  # noqa: E402
    LOCATION_DEPENDENT_TOOLS,
    PlanValidationError,
    validate_plan,
)
from src.listing_agent.agent.schemas import (  # noqa: E402
    Claim,
    PropertyDetails,
    ToolCall,
    TriagePlan,
)
from src.listing_agent.agent.step_tracer import StepTracer  # noqa: E402
from src.listing_agent.agent.tool_input_builder import (  # noqa: E402
    MissingDependencyError,
    _linked_nearby_claim_text,
    build_tool_input,
    extract_nearby_categories_argument,
    extract_nearby_query_argument,
    resolve_nearby_categories,
)
from src.listing_agent.config import Settings, get_settings  # noqa: E402
from src.listing_agent.llm.client.base import LLMClient  # noqa: E402
from src.listing_agent.llm.client.dependency import get_llm_client  # noqa: E402
from src.listing_agent.llm.client.fake import FakeLLMClient  # noqa: E402
from src.listing_agent.llm.triage_planner.planner import run_triage_planner  # noqa: E402
from src.listing_agent.tools.schemas import (  # noqa: E402
    NearbyServicesInput,
)

# ------------------------------------------------------------------------
# Claim classification (diagnostic-only presentation logic; never used by
# the production pipeline). Deterministic and derived entirely from
# existing production models/helpers - never a re-implementation of
# planner or plan_validator behavior.
# ------------------------------------------------------------------------


def _primary_verifying_tool(claim: Claim) -> str | None:
    """The one tool that actually *answers* `claim`, for a verifiable
    canonical category (e.g. `price_signal` for `"price_comparison"`,
    `nearby_services` for `"nearby_amenities"`) - `geo_resolver` is always
    a shared dependency, never the tool that itself resolves the claim."""
    canonical = normalize_category(claim.category)
    tools = TOOLS_BY_VERIFIABLE_CATEGORY.get(canonical)
    if not tools:
        return None
    return tools[-1]


def _claim_is_sent_to_tools(claim: Claim, validated_plan: TriagePlan) -> bool:
    """Whether some tool call in the *validated* plan will actually produce
    evidence relevant to `claim`.

    For a canonical *verifiable* category, this specifically requires the
    tool that answers that category (e.g. `price_signal` for
    `"price_comparison"`) to be present - `geo_resolver` alone (a shared
    dependency for several verifiable categories) is never sufficient by
    itself to say a claim "will be checked".
    """
    primary_tool = _primary_verifying_tool(claim)
    if primary_tool is not None:
        return any(
            call.tool == primary_tool
            and claim.id in find_relevant_claim_ids(call.tool, validated_plan.claims, call)
            for call in validated_plan.tool_calls
        )

    for call in validated_plan.tool_calls:
        if claim.id in find_relevant_claim_ids(call.tool, validated_plan.claims, call):
            return True
    return False


def _classify_unchecked_reason(
    claim: Claim,
    relevant_tools: list[str],
    raw_plan: TriagePlan,
    validated_plan: TriagePlan,
) -> str:
    """Why `claim` will not be checked by any tool.

    One of: "unsupported_claim_type", "missing_required_information",
    "rejected_by_plan_validation", "out_of_scope". (Subjective claims and
    listing facts are handled separately - see `_build_claims_sections` -
    and never reach this function.)
    """
    if not relevant_tools:
        return "unsupported_claim_type"

    raw_tool_names = {c.tool for c in raw_plan.tool_calls}
    proposed = [tool for tool in relevant_tools if tool in raw_tool_names]
    if not proposed:
        # No tool relevant to this claim's category was ever proposed by the
        # planner - a scope decision, not a validation rejection.
        return "out_of_scope"

    property_details = raw_plan.property_details
    if "price_signal" in proposed:
        if property_details.listing_type == "rental":
            return "rejected_by_plan_validation"
        if property_details.asking_price is None or property_details.sqm is None:
            return "missing_required_information"
    if set(proposed) & LOCATION_DEPENDENT_TOOLS and not property_details.raw_address:
        return "missing_required_information"

    # A relevant tool was proposed and had the data it needed, but still
    # isn't in the final plan (e.g. capped by `max_tool_calls`).
    return "rejected_by_plan_validation"


def _build_claims_sections(
    raw_plan: TriagePlan, validated_plan: TriagePlan
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(identified_claims, claims_not_sent_to_tools)`.

    Every claim is classified first via `classify_claim` (subjective /
    listing_fact / verifiable / unsupported) - a *listing fact* (e.g.
    rooms, size_sqm, asking price, raw address) is a seller-provided input
    consumed by a tool, never an independent claim that itself gets
    "checked" or "not checked", so it never appears in
    `claims_not_sent_to_tools` at all.
    """
    identified: list[dict[str, Any]] = []
    not_sent: list[dict[str, Any]] = []

    for claim in validated_plan.claims:
        relevant_tools = relevant_tools_for_claim(claim)
        classification = classify_claim(claim)
        sent = classification == "verifiable" and _claim_is_sent_to_tools(claim, validated_plan)

        if classification == "subjective":
            verifiability = "unverifiable_subjective"
        elif classification == "listing_fact":
            verifiability = "listing_fact"
        elif classification == "unsupported":
            verifiability = "unsupported_claim_type"
        elif sent:
            verifiability = "verifiable"
        else:
            verifiability = "verifiable_but_not_checked"

        identified.append(
            {
                "claim_id": claim.id,
                "text": claim.text,
                "type": claim.type,
                "category": claim.category,
                "verifiability": verifiability,
                "proposed_tools": relevant_tools,
            }
        )

        if classification in ("subjective", "listing_fact"):
            if classification == "subjective":
                not_sent.append(
                    {
                        "claim_id": claim.id,
                        "text": claim.text,
                        "reason": "subjective_marketing_claim",
                    }
                )
            # listing_fact: never "sent to tools" as a claim to check, and
            # never reported as "not sent" either - it was never meant to
            # be an independent claim in the first place.
            continue

        if not sent:
            reason = _classify_unchecked_reason(claim, relevant_tools, raw_plan, validated_plan)
            not_sent.append({"claim_id": claim.id, "text": claim.text, "reason": reason})

    return identified, not_sent


# ------------------------------------------------------------------------
# Final tool plan + prepared inputs.
# ------------------------------------------------------------------------

# Defaults `NearbyServicesInput` uses when `evidence_collector` does not set
# them explicitly (see `agent/tool_input_builder.py`) - read from the model
# itself so this never invents a value the real pipeline wouldn't send.
_NEARBY_SERVICES_DEFAULT_CATEGORIES = [
    category.value if hasattr(category, "value") else category
    for category in NearbyServicesInput.model_fields["categories"].default_factory()  # type: ignore[misc]
]
_NEARBY_SERVICES_DEFAULT_RADIUS_M = NearbyServicesInput.model_fields["radius_m"].default


def _build_final_tool_plan(
    raw_plan: TriagePlan, validated_plan: TriagePlan
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return `(final_tool_plan, call_id_by_tool)`."""
    raw_tool_counts: dict[str, int] = {}
    for call in raw_plan.tool_calls:
        raw_tool_counts[call.tool] = raw_tool_counts.get(call.tool, 0) + 1

    call_id_by_tool: dict[str, str] = {}
    plan_entries: list[dict[str, Any]] = []
    for index, call in enumerate(validated_plan.tool_calls):
        call_id = f"tool-call-{index + 1}"
        call_id_by_tool[call.tool] = call_id
        depends_on = (
            [call_id_by_tool["geo_resolver"]]
            if call.tool in LOCATION_DEPENDENT_TOOLS and "geo_resolver" in call_id_by_tool
            else []
        )
        plan_entries.append(
            {
                "call_id": call_id,
                "tool_name": call.tool,
                "supports_claim_ids": find_relevant_claim_ids(
                    call.tool, validated_plan.claims, call
                ),
                "depends_on": depends_on,
                "execution_stage": 1 if not depends_on else 2,
                "merged_from_duplicates": raw_tool_counts.get(call.tool, 0) > 1,
                "planner_arguments": call.arguments,
            }
        )
    return plan_entries, call_id_by_tool


def _prepared_input_for_call(
    call: ToolCall,
    property_details: PropertyDetails,
    geo_call_id: str | None,
    claims: list[Claim] | None = None,
) -> dict[str, Any]:
    """The exact typed payload `call.tool` would be executed with next, or
    an explicit unresolved-dependency placeholder if it cannot be built yet:
    either it depends on `geo_resolver`'s output, which this script never
    produces (it never runs any tool), or - for `geo_resolver` itself - the
    address names no city this agent recognises (`_NO_CITY_HINT_REASON`),
    which is refused before any tool ever runs."""
    try:
        input_model = build_tool_input(call, property_details, geo_output=None, claims=claims)
    except MissingDependencyError as exc:
        if call.tool == "nearby_services":
            requested_categories = resolve_nearby_categories(
                categories=extract_nearby_categories_argument(call.arguments),
                query=extract_nearby_query_argument(call.arguments),
                claim_text=_linked_nearby_claim_text(call, claims),
            )
            template: dict[str, Any] = {
                "lat": {"from_call": geo_call_id, "field": "lat"},
                "lon": {"from_call": geo_call_id, "field": "lon"},
                "categories": requested_categories or _NEARBY_SERVICES_DEFAULT_CATEGORIES,
                "radius_m": _NEARBY_SERVICES_DEFAULT_RADIUS_M,
            }
        elif call.tool == "geo_resolver":
            # Not a downstream dependency on another call - geo_resolver's
            # own input is unbuildable because the address itself names no
            # recognised city. There is no `from_call` to point at, so the
            # placeholder mirrors GeoResolverInput's own fields directly:
            # the address is known, the missing piece is the city hint.
            template = {
                "address": property_details.raw_address,
                "city_hint": None,
            }
        else:  # price_signal
            template = {
                "normalized_address": {"from_call": geo_call_id, "field": "normalized_address"},
                "neighborhood": {"from_call": geo_call_id, "field": "neighborhood"},
                "listing_type": property_details.listing_type,
                "asking_price": property_details.asking_price,
                "area_sqm": property_details.sqm,
            }
        return {
            "resolved": False,
            "unresolved_dependency_reason": exc.reason,
            "value": template,
        }
    return {"resolved": True, "value": input_model.model_dump(mode="json")}


def _build_prepared_tool_inputs(
    validated_plan: TriagePlan, call_id_by_tool: dict[str, str]
) -> list[dict[str, Any]]:
    geo_call_id = call_id_by_tool.get("geo_resolver")
    return [
        {
            "call_id": call_id_by_tool[call.tool],
            "tool_name": call.tool,
            **_prepared_input_for_call(
                call, validated_plan.property_details, geo_call_id, validated_plan.claims
            ),
        }
        for call in validated_plan.tool_calls
    ]


# ------------------------------------------------------------------------
# Top-level diagnostic report.
# ------------------------------------------------------------------------


async def build_diagnostic_report(
    prompt: str,
    client: LLMClient,
    tracer: StepTracer,
    settings: Settings,
    llm_mode: str,
) -> dict[str, Any]:
    """Run the real pipeline up to (not including) tool execution and
    return one structured, JSON-serializable report.

    Never calls `ToolExecutor.execute`, any mock/real tool,
    `evidence_collector`, `evidence_bundle_builder`, or `report_writer`.
    """
    start = time.perf_counter()
    raw_plan = await run_triage_planner(prompt, client, tracer)
    planner_duration_ms = (time.perf_counter() - start) * 1000

    diagnostics: dict[str, Any] = {
        "llm_mode": llm_mode,
        "llm_calls": sum(1 for step in tracer.get_steps() if step.type == "llm"),
        "planner_duration_ms": round(planner_duration_ms, 2),
        "planned_tool_calls_before_deduplication": len(raw_plan.tool_calls),
    }

    try:
        validated_plan = validate_plan(
            raw_plan, max_tool_calls=settings.max_tool_calls, max_claims=settings.max_claims
        )
    except PlanValidationError as exc:
        diagnostics["validation_status"] = "rejected"
        diagnostics["validation_errors"] = list(exc.errors)
        diagnostics["planned_tool_calls_after_deduplication"] = 0
        claims = [
            claim if claim.id else claim.model_copy(update={"id": f"claim-{i}"})
            for i, claim in enumerate(raw_plan.claims)
        ]
        identified_claims = [
            {
                "claim_id": claim.id,
                "text": claim.text,
                "type": claim.type,
                "category": claim.category,
                "verifiability": "not_evaluated_plan_rejected",
                "proposed_tools": relevant_tools_for_claim(claim),
            }
            for claim in claims
        ]
        claims_not_sent = [
            {
                "claim_id": claim.id,
                "text": claim.text,
                "reason": "rejected_by_plan_validation",
            }
            for claim in claims
        ]
        return {
            "extracted_listing_facts": raw_plan.property_details.model_dump(mode="json"),
            "identified_claims": identified_claims,
            "claims_not_sent_to_tools": claims_not_sent,
            "final_tool_plan": [],
            "prepared_tool_inputs": [],
            "diagnostics": diagnostics,
        }

    diagnostics["validation_status"] = "accepted"
    diagnostics["planned_tool_calls_after_deduplication"] = len(validated_plan.tool_calls)

    identified_claims, claims_not_sent = _build_claims_sections(raw_plan, validated_plan)
    final_tool_plan, call_id_by_tool = _build_final_tool_plan(raw_plan, validated_plan)
    prepared_tool_inputs = _build_prepared_tool_inputs(validated_plan, call_id_by_tool)

    return {
        "extracted_listing_facts": validated_plan.property_details.model_dump(mode="json"),
        "identified_claims": identified_claims,
        "claims_not_sent_to_tools": claims_not_sent,
        "final_tool_plan": final_tool_plan,
        "prepared_tool_inputs": prepared_tool_inputs,
        "diagnostics": diagnostics,
    }


# ------------------------------------------------------------------------
# Text rendering.
# ------------------------------------------------------------------------


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []

    lines.append("1. EXTRACTED LISTING FACTS")
    lines.append("-" * 26)
    for key, value in report["extracted_listing_facts"].items():
        lines.append(f"{key}: {value}")
    lines.append("")

    lines.append("2. IDENTIFIED CLAIMS")
    lines.append("-" * 20)
    for claim in report["identified_claims"]:
        lines.append(f"claim_id: {claim['claim_id']}")
        lines.append(f"text: {claim['text']}")
        lines.append(f"type: {claim['type']}")
        lines.append(f"category: {claim['category']}")
        lines.append(f"verifiability: {claim['verifiability']}")
        lines.append("proposed_tools:")
        for tool in claim["proposed_tools"]:
            lines.append(f"  - {tool}")
        lines.append("")

    lines.append("3. CLAIMS NOT SENT TO TOOLS")
    lines.append("-" * 27)
    if not report["claims_not_sent_to_tools"]:
        lines.append("(none - every claim will be checked by at least one tool)")
    for claim in report["claims_not_sent_to_tools"]:
        lines.append(f"claim_id: {claim['claim_id']}")
        lines.append(f"text: {claim['text']}")
        lines.append(f"reason: {claim['reason']}")
        lines.append("")

    lines.append("4. FINAL TOOL PLAN")
    lines.append("-" * 18)
    if not report["final_tool_plan"]:
        lines.append("(no tool calls in the final validated plan)")
    for entry in report["final_tool_plan"]:
        lines.append(f"call_id: {entry['call_id']}")
        lines.append(f"tool_name: {entry['tool_name']}")
        lines.append("supports_claim_ids:")
        for claim_id in entry["supports_claim_ids"]:
            lines.append(f"  - {claim_id}")
        lines.append(f"depends_on: {entry['depends_on']}")
        lines.append(f"execution_stage: {entry['execution_stage']}")
        lines.append(f"merged_from_duplicates: {entry['merged_from_duplicates']}")
        lines.append(f"planner_arguments: {entry['planner_arguments']}")
        lines.append("")

    lines.append("5. PREPARED TOOL INPUTS")
    lines.append("-" * 23)
    lines.append("")
    for entry in report["prepared_tool_inputs"]:
        lines.append(f"{entry['tool_name']} ({entry['call_id']}):")
        if not entry["resolved"]:
            lines.append(f"  # unresolved dependency: {entry['unresolved_dependency_reason']}")
        lines.append(json.dumps(entry["value"], ensure_ascii=False, indent=2))
        lines.append("")

    diagnostics = report["diagnostics"]
    lines.append("DIAGNOSTICS")
    lines.append("-" * 11)
    for key, value in diagnostics.items():
        lines.append(f"{key}: {value}")

    return "\n".join(lines)


# ------------------------------------------------------------------------
# CLI.
# ------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the real triage_planner pipeline (planning, validation, "
            "dependency resolution, deduplication) up to - but not including "
            "- tool execution."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--listing", type=str, help="Raw listing text.")
    source.add_argument("--file", type=str, help="Path to a file containing the listing text.")
    source.add_argument(
        "--example",
        type=int,
        choices=sorted(EXAMPLE_LISTINGS),
        help="Use one of the built-in example listings instead of --listing/--file.",
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
        help=(
            "Use FakeLLMClient with a pre-built plan. Requires --example, "
            "since FakeLLMClient cannot plan arbitrary listing text."
        ),
    )

    parser.add_argument("--json", action="store_true", help="Emit one JSON document instead.")
    return parser.parse_args(argv)


def _resolve_listing_text(args: argparse.Namespace) -> str:
    if args.example is not None:
        return EXAMPLE_LISTINGS[args.example]
    if args.file:
        return Path(args.file).read_text(encoding="utf-8").strip()
    return args.listing


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.fake_llm and args.example is None:
        print(
            "error: --fake-llm requires --example {1,2,3}: FakeLLMClient only replays "
            "pre-built plans, it cannot plan arbitrary --listing/--file text without a "
            "real LLM call.",
            file=sys.stderr,
        )
        return 2

    listing_text = _resolve_listing_text(args)
    tracer = StepTracer()
    settings = get_settings()

    if args.fake_llm:
        client: LLMClient = FakeLLMClient()
        from src.listing_agent.agent.module_names import TRIAGE_PLANNER

        client.set_response(TRIAGE_PLANNER, EXAMPLE_PLANS[args.example])
        llm_mode = "fake"
    else:
        real_settings = settings.model_copy(update={"llm_mode": "llmod"})
        try:
            client = get_llm_client(real_settings, step_tracer=tracer)
        except ValueError as exc:
            # `LLModClient`'s own constructor already raises a clear, safe
            # error naming only the missing setting(s) - never a value.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        llm_mode = "real"

    report = asyncio.run(build_diagnostic_report(listing_text, client, tracer, settings, llm_mode))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
