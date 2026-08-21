"""Executable **Pre-Tool Triage** diagnostic (see `docs/pre_tool_triage.md`).

Turns the previously hand-authored `artifacts/claim-retention-fix/
pre-tool-triage.json` into a *generated* artifact: it runs the real
`triage_planner` -> `plan_validator` -> `tool_input_builder` pipeline up to
(but never including) tool execution, then reshapes that production output
into the exact semantic shape the Pre-Tool Triage contract requires.

```text
Raw listing
-> triage_planner            (src.listing_agent.llm.triage_planner.planner)
-> plan validation            (src.listing_agent.agent.plan_validator.validate_plan)
-> final prepared tool inputs  (src.listing_agent.agent.tool_input_builder.build_tool_input)
-> STOP before ToolExecutor.execute()
```

Every semantic field below is derived from a production function - never
re-implemented here:

* property facts               `PropertyDetails.model_dump(mode="json")`
* retained atomic claims        `validate_plan(...).claims`
* canonical category            `claim_categories.normalize_category`
* verification decision         `claim_categories.verification_decision`
* deterministic priority        `plan_validator.validator._claim_priority`
* removed-claim reasons         production `_dedupe_claims` / `_prioritize_claims`
                                / `_cap_claims`, in the identical order
                                `validate_plan` itself applies them
* tool plan (`supports_claim_ids`, dependencies)
                                `inspect_triage_plan._build_final_tool_plan`
                                (which itself calls
                                `evidence_mapper.find_relevant_claim_ids`)
* prepared tool inputs          `inspect_triage_plan._build_prepared_tool_inputs`
                                (which itself calls
                                `tool_input_builder.build_tool_input`)

It never imports/instantiates `LocalToolExecutor`, `McpToolExecutor`, any
mock/real tool, `evidence_collector`, `evidence_bundle_builder`, or
`report_writer`, and never calls MCP - it stops before tool execution.

`follow_up_topics` are a strictly deterministic, structural derivation off
retained unverified claims and missing information (there is no existing
deterministic follow-up-question generator in production; natural-language
buyer-question generation remains a `report_writer` concern). See
`docs/pre_tool_triage.md` §9's implementation note.

Usage:
    python scripts/pre_tool_triage.py --fake-llm --example 1
    python scripts/pre_tool_triage.py --fake-llm --gonen-fixture --json
    python scripts/pre_tool_triage.py --fake-llm --example 1 --output-json out.json
    python scripts/pre_tool_triage.py --real-llm --file examples/listing.txt --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow running as a plain script (`python scripts/pre_tool_triage.py`) from
# the repo root, matching the `inspect_triage_plan.py` convention.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reuse (never re-implement) the existing pre-tool-execution diagnostic's
# tool-plan and prepared-input builders.
from scripts.inspect_triage_plan import (  # noqa: E402
    _build_final_tool_plan,
    _build_prepared_tool_inputs,
)
from scripts.triage_plan_examples import EXAMPLE_LISTINGS, EXAMPLE_PLANS  # noqa: E402
from src.listing_agent.agent.claim_categories import (  # noqa: E402
    normalize_category,
    verification_decision,
)
from src.listing_agent.agent.module_names import TRIAGE_PLANNER  # noqa: E402
from src.listing_agent.agent.plan_validator import (  # noqa: E402
    PlanValidationError,
    validate_plan,
)

# Directly importing these "private" validator internals for read-only
# diagnostics is an established, accepted pattern in this repo - see
# `scripts/diagnose_claim_loss.py`, which already imports `_cap_claims`,
# `_assign_stable_claim_ids`, `MAX_CLAIMS`, and `MAX_TOOL_CALLS` the same way.
# The reason is identical here: reproduce the exact deterministic decisions
# without duplicating their algorithm.
from src.listing_agent.agent.plan_validator.validator import (  # noqa: E402
    MAX_CLAIMS,
    _cap_claims,
    _claim_priority,
    _dedupe_claims,
    _prioritize_claims,
)
from src.listing_agent.agent.schemas import Claim, TriagePlan  # noqa: E402
from src.listing_agent.agent.step_tracer import StepTracer  # noqa: E402
from src.listing_agent.agent.tool_input_builder import (  # noqa: E402
    extract_city_hint,
    resolve_nearby_categories,
)
from src.listing_agent.config import Settings, get_settings  # noqa: E402
from src.listing_agent.llm.client.base import LLMClient  # noqa: E402
from src.listing_agent.llm.client.dependency import get_llm_client  # noqa: E402
from src.listing_agent.llm.client.fake import FakeLLMClient  # noqa: E402
from src.listing_agent.llm.triage_planner.planner import run_triage_planner  # noqa: E402

# The Pre-Tool Triage verification-decision buckets, in the order
# `docs/pre_tool_triage.md` §7 prints them. `MISSING_INFORMATION` lists
# entries from `validated_plan.missing_information`, not claims.
_VERIFICATION_BUCKETS: tuple[str, ...] = (
    "LISTING_FACT",
    "CHECKABLE_WITH_TOOL",
    "REQUIRES_SELLER_OR_DOCUMENT",
    "REQUIRES_CONTRACT_REVIEW",
    "SUBJECTIVE",
    "OUT_OF_SCOPE",
    "MISSING_INFORMATION",
)

# Deterministic, mechanical follow-up templates keyed off a retained claim's
# own verification decision. Never natural-language generation, never the
# hand-authored Hebrew prose from the old artifact - just a structural
# restatement of the claim text so the drop is actionable.
_FOLLOW_UP_TEMPLATES: dict[str, str] = {
    "REQUIRES_SELLER_OR_DOCUMENT": "confirm with seller or request document: {text}",
    "REQUIRES_CONTRACT_REVIEW": "clarify in contract review: {text}",
}

# `missing_information` markers that are internal truncation diagnostics
# rather than buyer-facing missing facts - kept verbatim in the report's
# `missing_information` field (faithful to production), but excluded from
# generated follow-up topics.
_TRUNCATION_MARKER_PREFIX = "additional_claims_truncated"


def _removed_claims(raw_plan: TriagePlan) -> list[dict[str, Any]]:
    """Return the claims present in `raw_plan` but dropped by validation,
    each tagged with the deterministic reason it was removed.

    Reproduces `validate_plan`'s retention pipeline by calling the *same*
    production functions in the *same* order it does
    (`_dedupe_claims` -> `_prioritize_claims` -> `_cap_claims`), then
    attributes each removed claim by object identity to the exact stage
    that removed it - never a re-implementation of that logic.
    """
    produced = raw_plan.claims
    deduped = _dedupe_claims(produced, raw_plan.property_details)
    deduped_identity = {id(claim) for claim in deduped}

    prioritized = _prioritize_claims(deduped, raw_plan.buyer_goal)
    scratch_missing: list[str] = []
    retained = _cap_claims(
        prioritized,
        MAX_CLAIMS,
        scratch_missing,
        produced_count=len(produced),
        deduped_count=len(deduped),
    )
    retained_identity = {id(claim) for claim in retained}

    removed: list[dict[str, Any]] = []
    for claim in produced:
        if id(claim) not in deduped_identity:
            reason = "deduplicated"
        elif id(claim) not in retained_identity:
            reason = "capped_max_claims"
        else:
            continue
        removed.append(
            {
                "claim_id": claim.id or None,
                "text": claim.text,
                "canonical_category": normalize_category(claim.category),
                "reason": reason,
            }
        )
    return removed


def _retained_claim_entry(claim: Claim, buyer_goal: str) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "text": claim.text,
        "source": claim.source.value if claim.source is not None else None,
        "type": claim.type,
        "category": claim.category,
        "canonical_category": normalize_category(claim.category),
        "priority": _claim_priority(claim, buyer_goal),
        "verification_decision": verification_decision(claim),
    }


def _verification_still_required(
    validated_plan: TriagePlan,
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {bucket: [] for bucket in _VERIFICATION_BUCKETS}
    for claim in validated_plan.claims:
        grouped[verification_decision(claim)].append(claim.text)
    grouped["MISSING_INFORMATION"] = list(validated_plan.missing_information)
    return grouped


def _follow_up_topics(validated_plan: TriagePlan) -> list[dict[str, str]]:
    topics: list[dict[str, str]] = []
    for claim in validated_plan.claims:
        decision = verification_decision(claim)
        template = _FOLLOW_UP_TEMPLATES.get(decision)
        if template is None:
            continue
        topics.append(
            {
                "claim_id": claim.id,
                "verification_decision": decision,
                "topic": template.format(text=claim.text),
            }
        )
    for item in validated_plan.missing_information:
        if item.startswith(_TRUNCATION_MARKER_PREFIX):
            continue
        topics.append(
            {
                "source": "missing_information",
                "topic": f"obtain missing information: {item}",
            }
        )
    return topics


def _tool_plan(raw_plan: TriagePlan, validated_plan: TriagePlan) -> list[dict[str, Any]]:
    """Reshape `inspect_triage_plan._build_final_tool_plan` into the
    Pre-Tool Triage tool-plan shape (§5), preserving its `supports_claim_ids`
    (production `find_relevant_claim_ids`) and dependency wiring exactly."""
    entries, _call_id_by_tool = _build_final_tool_plan(raw_plan, validated_plan)
    return [
        {
            "call_id": entry["call_id"],
            "tool_name": entry["tool_name"],
            "supports_claim_ids": entry["supports_claim_ids"],
            "dependencies": entry["depends_on"],
            "planner_arguments": entry["planner_arguments"],
        }
        for entry in entries
    ]


# ------------------------------------------------------------------------
# Focused Pre-Tool Triage view.
#
# The full report above answers the *original* broad Pre-Tool Triage
# contract (every atomic claim, every verification bucket, follow-up
# topics). The focused view below is a pure, deterministic *projection* of
# that same report dict onto the project's actual external-evidence scope:
# only nearby/place claims and comparative price claims - i.e. only claims
# that can affect one of the three current tool decisions
# (`geo_resolver`, `nearby_services`, `price_signal`). It adds no new
# analysis, no LLM call, and no tool execution; it merely re-buckets what
# the planner already produced. The full detail remains available in the
# JSON report and via `--verbose`.
# ------------------------------------------------------------------------

# Canonical claim categories that are in-scope for external evidence with
# the current frozen tool set. Everything else (transaction_structure,
# condition, bare listing facts, uncategorized) is useful listing context
# but outside the current evidence scope.
_RELEVANT_CANONICAL_CATEGORIES: frozenset[str] = frozenset(
    {"nearby_amenities", "price_comparison"}
)

# Claim sources that mean "this comparative claim came from the buyer's
# own question" rather than from the seller/listing text. `DERIVED` is what
# `plan_validator` stamps on the synthetic comparative claim it creates for
# a `price_validation` buyer goal (see `_ensure_price_comparison_claim_linkage`).
_BUYER_QUESTION_SOURCES: frozenset[str] = frozenset({"derived", "user_question"})


def _nearby_support(claim_text: str) -> list[str]:
    """Return the frozen `nearby_services` categories a nearby claim's own
    text maps to (via the production `resolve_nearby_categories`), or `[]`
    if the concept is outside the frozen category set (e.g. light rail,
    shelter, hospital-only wording the aliases don't cover). Reuses the
    exact production category resolver - never a second heuristic - so a
    claim is called "supported" here iff the real pipeline could actually
    turn it into a category the tool searches for."""
    return list(resolve_nearby_categories(claim_text=claim_text))


def _price_signal_prepared_input(report: dict[str, Any]) -> dict[str, Any] | None:
    for entry in report.get("prepared_tool_inputs", []):
        if entry["tool_name"] == "price_signal":
            return entry["value"]
    return None


def build_focused_view(report: dict[str, Any]) -> dict[str, Any]:
    """Project a full Pre-Tool Triage `report` dict onto the current
    external-evidence scope. Pure/deterministic; reads only the already
    computed report, executes no tool, calls no LLM."""
    property_facts = report.get("property_facts", {})
    raw_address = property_facts.get("raw_address")
    tool_names = {entry["tool_name"] for entry in report.get("tool_plan", [])}

    nearby_tool = next(
        (e for e in report.get("tool_plan", []) if e["tool_name"] == "nearby_services"),
        None,
    )
    nearby_categories: list[str] = []
    if nearby_tool is not None:
        nearby_input = next(
            (
                e["value"]
                for e in report.get("prepared_tool_inputs", [])
                if e["tool_name"] == "nearby_services"
            ),
            {},
        )
        nearby_categories = list(nearby_input.get("categories", []))

    # --- Property context: only tool-relevant fields actually found ------
    property_context: list[dict[str, Any]] = []
    if raw_address:
        property_context.append({"field": "address", "value": raw_address})
    city_hint = extract_city_hint(raw_address)
    if city_hint:
        property_context.append({"field": "city/neighborhood", "value": city_hint})
    if property_facts.get("listing_type") not in (None, "unknown"):
        property_context.append(
            {"field": "listing_type", "value": property_facts["listing_type"]}
        )
    for field in ("asking_price", "sqm", "rooms"):
        value = property_facts.get(field)
        if value is not None:
            property_context.append({"field": field, "value": value})

    # --- Relevant claims (nearby + price comparison) ---------------------
    relevant_claims: list[dict[str, Any]] = []
    other_content_present = False
    supported_nearby_found = False
    price_comparison_claims: list[dict[str, Any]] = []

    for claim in report.get("retained_atomic_claims", []):
        canonical = claim.get("canonical_category")
        if canonical not in _RELEVANT_CANONICAL_CATEGORIES:
            other_content_present = True
            continue

        source = claim.get("source") or "listing"
        if canonical == "nearby_amenities":
            categories = _nearby_support(claim["text"])
            supported = bool(categories)
            entry: dict[str, Any] = {
                "text": claim["text"],
                "source": source,
                "type": "nearby",
                "supported": supported,
            }
            if supported:
                supported_nearby_found = True
                entry["reason"] = "maps to a frozen nearby_services category"
                entry["supported_categories"] = categories
                entry["planned_tool"] = "nearby_services" if nearby_tool else None
                entry["supports_claim_ids"] = [claim["claim_id"]]
            else:
                entry["reason"] = "unsupported nearby category"
                entry["planned_tool"] = None
            relevant_claims.append(entry)
        else:  # price_comparison
            price_comparison_claims.append(claim)
            relevant_claims.append(
                {
                    "text": claim["text"],
                    "source": source,
                    "type": "price comparison",
                    "supported": True,
                    "reason": "comparative price claim - checkable with price_signal",
                    "planned_tool": "price_signal" if "price_signal" in tool_names else None,
                    "supports_claim_ids": [claim["claim_id"]],
                }
            )

    if report.get("removed_claims"):
        other_content_present = True

    # --- Price section ---------------------------------------------------
    asking_price_found = property_facts.get("asking_price") is not None
    comparative_listing_claim = any(
        (c.get("source") or "listing") not in _BUYER_QUESTION_SOURCES
        for c in price_comparison_claims
    )
    comparative_buyer_question = any(
        (c.get("source") or "listing") in _BUYER_QUESTION_SOURCES
        for c in price_comparison_claims
    )
    price_signal_missing_inputs: list[str] = []
    if "price_signal" in tool_names:
        prepared = _price_signal_prepared_input(report) or {}
        if prepared.get("asking_price") is None:
            price_signal_missing_inputs.append("asking_price")
        if prepared.get("area_sqm") is None:
            price_signal_missing_inputs.append("area_sqm")
        price_signal_status = "MISSING INPUT" if price_signal_missing_inputs else "NEEDED"
    else:
        price_signal_status = "NOT NEEDED"

    price = {
        "asking_price_found": asking_price_found,
        "comparative_listing_claim_found": comparative_listing_claim,
        "comparative_buyer_question_found": comparative_buyer_question,
        "price_signal": price_signal_status,
        "price_signal_missing_inputs": price_signal_missing_inputs,
    }

    # --- Final tool plan (only tools that would actually execute) --------
    prepared_by_tool = {
        e["tool_name"]: e for e in report.get("prepared_tool_inputs", [])
    }
    final_tool_plan: list[dict[str, Any]] = []
    for entry in report.get("tool_plan", []):
        prepared = prepared_by_tool.get(entry["tool_name"], {})
        plan_entry: dict[str, Any] = {
            "tool_name": entry["tool_name"],
            "supports_claim_ids": entry["supports_claim_ids"],
            "depends_on": entry["dependencies"],
            "resolved": prepared.get("resolved", True),
            "input": prepared.get("value", {}),
        }
        if entry["tool_name"] == "nearby_services":
            plan_entry["categories"] = nearby_categories
        final_tool_plan.append(plan_entry)

    # --- Missing information that materially affects a relevant check ----
    missing_information: list[str] = []
    if supported_nearby_found and not raw_address:
        missing_information.append("precise address required for nearby lookup")
    if "price_signal" in tool_names:
        prepared = _price_signal_prepared_input(report) or {}
        if prepared.get("asking_price") is None:
            missing_information.append("asking price required for price comparison")
        if prepared.get("area_sqm") is None:
            missing_information.append("sqm required by price_signal")

    return {
        "property_context": property_context,
        "relevant_claims": relevant_claims,
        "price": price,
        "final_tool_plan": final_tool_plan,
        "missing_information": missing_information,
        "other_listing_content_present": other_content_present,
    }


def build_pre_tool_triage_report(
    raw_plan: TriagePlan,
    validated_plan: TriagePlan,
    *,
    buyer_question: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reshape one production planner+validation result into the exact
    `docs/pre_tool_triage.md` semantic shape. Pure/deterministic; executes
    no tool, calls no LLM, calls no MCP."""
    _entries, call_id_by_tool = _build_final_tool_plan(raw_plan, validated_plan)

    report: dict[str, Any] = {
        "title": "Pre-Tool Triage",
        "property_facts": validated_plan.property_details.model_dump(mode="json"),
        "buyer_goal": validated_plan.buyer_goal,
    }
    if buyer_question is not None:
        report["buyer_question"] = buyer_question

    report["retained_atomic_claims"] = [
        _retained_claim_entry(claim, validated_plan.buyer_goal)
        for claim in validated_plan.claims
    ]
    report["removed_claims"] = _removed_claims(raw_plan)
    report["tool_plan"] = _tool_plan(raw_plan, validated_plan)
    report["prepared_tool_inputs"] = _build_prepared_tool_inputs(
        validated_plan, call_id_by_tool
    )
    report["verification_still_required"] = _verification_still_required(validated_plan)
    report["missing_information"] = list(validated_plan.missing_information)
    report["follow_up_topics"] = _follow_up_topics(validated_plan)
    report["focused"] = build_focused_view(report)
    if diagnostics is not None:
        report["diagnostics"] = diagnostics
    return report


async def run_pre_tool_triage(
    prompt: str,
    client: LLMClient,
    tracer: StepTracer,
    settings: Settings,
    llm_mode: str,
    *,
    buyer_question: str | None = None,
) -> dict[str, Any]:
    """Run the real pipeline up to (not including) tool execution and
    return one Pre-Tool Triage report. Never calls `ToolExecutor.execute`,
    any tool, `evidence_collector`, `evidence_bundle_builder`,
    `report_writer`, or MCP."""
    start = time.perf_counter()
    raw_plan = await run_triage_planner(prompt, client, tracer)
    planner_duration_ms = (time.perf_counter() - start) * 1000

    diagnostics: dict[str, Any] = {
        "llm_mode": llm_mode,
        # Provenance metadata (diagnostic-only, never a production model
        # field): this function's own body is the proof - it calls
        # `run_triage_planner` exactly once, then `validate_plan`, then
        # (via `build_pre_tool_triage_report`) the read-only tool-plan/
        # prepared-input builders. It never imports/calls a tool
        # executor, MCP, or `report_writer`, so these are fixed facts
        # about *this diagnostic*, not something derived per-run.
        "planner_mode": llm_mode,
        "tools_executed": False,
        "mcp_called": False,
        "external_provider_called": False,
        "report_writer_called": False,
        "llm_calls": sum(1 for step in tracer.get_steps() if step.type == "llm"),
        "planner_duration_ms": round(planner_duration_ms, 2),
        "produced_claims": len(raw_plan.claims),
        "produced_tool_calls": len(raw_plan.tool_calls),
    }

    try:
        validated_plan = validate_plan(
            raw_plan,
            max_tool_calls=settings.max_tool_calls,
            max_claims=settings.max_claims,
            buyer_prompt=prompt,
        )
    except PlanValidationError as exc:
        diagnostics["validation_status"] = "rejected"
        diagnostics["validation_errors"] = list(exc.errors)
        rejected_report = {
            "title": "Pre-Tool Triage",
            "property_facts": raw_plan.property_details.model_dump(mode="json"),
            "buyer_goal": raw_plan.buyer_goal,
            **({"buyer_question": buyer_question} if buyer_question is not None else {}),
            "retained_atomic_claims": [],
            "removed_claims": [],
            "tool_plan": [],
            "prepared_tool_inputs": [],
            "verification_still_required": {bucket: [] for bucket in _VERIFICATION_BUCKETS},
            "missing_information": list(raw_plan.missing_information),
            "follow_up_topics": [],
        }
        rejected_report["focused"] = build_focused_view(rejected_report)
        rejected_report["diagnostics"] = diagnostics
        return rejected_report

    diagnostics["validation_status"] = "accepted"
    diagnostics["retained_claims"] = len(validated_plan.claims)
    diagnostics["planned_tool_calls"] = len(validated_plan.tool_calls)

    return build_pre_tool_triage_report(
        raw_plan,
        validated_plan,
        buyer_question=buyer_question,
        diagnostics=diagnostics,
    )


# ------------------------------------------------------------------------
# Text rendering (derived from the exact same in-memory report dict as the
# JSON output).
# ------------------------------------------------------------------------


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = [report["title"], "=" * len(report["title"]), ""]

    lines.append("1. PROPERTY FACTS")
    lines.append("-" * 17)
    for key, value in report["property_facts"].items():
        lines.append(f"{key}: {value}")
    lines.append("")

    lines.append("2. BUYER GOAL")
    lines.append("-" * 13)
    lines.append(f"buyer_goal: {report['buyer_goal']}")
    if "buyer_question" in report:
        lines.append(f"buyer_question: {report['buyer_question']}")
    lines.append("")

    lines.append("3. RETAINED ATOMIC CLAIMS")
    lines.append("-" * 25)
    for claim in report["retained_atomic_claims"]:
        lines.append(f"claim_id: {claim['claim_id']}")
        lines.append(f"text: {claim['text']}")
        lines.append(f"source: {claim['source']}")
        lines.append(f"canonical_category: {claim['canonical_category']}")
        lines.append(f"priority: {claim['priority']}")
        lines.append(f"verification_decision: {claim['verification_decision']}")
        lines.append("")

    lines.append("4. REMOVED CLAIMS")
    lines.append("-" * 16)
    if not report["removed_claims"]:
        lines.append("(none - every produced claim survived deduplication and the cap)")
    for claim in report["removed_claims"]:
        lines.append(f"text: {claim['text']}")
        lines.append(f"canonical_category: {claim['canonical_category']}")
        lines.append(f"reason: {claim['reason']}")
        lines.append("")

    lines.append("5. TOOL PLAN")
    lines.append("-" * 12)
    if not report["tool_plan"]:
        lines.append("(no tool calls in the final validated plan)")
    for entry in report["tool_plan"]:
        lines.append(f"tool_name: {entry['tool_name']}")
        lines.append("supports_claim_ids:")
        for claim_id in entry["supports_claim_ids"]:
            lines.append(f"  - {claim_id}")
        lines.append(f"dependencies: {entry['dependencies']}")
        lines.append(f"planner_arguments: {entry['planner_arguments']}")
        lines.append("")

    lines.append("6. PREPARED TOOL INPUTS")
    lines.append("-" * 23)
    for entry in report["prepared_tool_inputs"]:
        lines.append(f"{entry['tool_name']} ({entry['call_id']}):")
        if not entry["resolved"]:
            lines.append(f"  # unresolved dependency: {entry['unresolved_dependency_reason']}")
        lines.append(json.dumps(entry["value"], ensure_ascii=False, indent=2))
        lines.append("")

    lines.append("7. VERIFICATION STILL REQUIRED")
    lines.append("-" * 30)
    for bucket in _VERIFICATION_BUCKETS:
        items = report["verification_still_required"].get(bucket, [])
        lines.append(f"{bucket}:")
        if not items:
            lines.append("  (none)")
        for item in items:
            lines.append(f"  - {item}")
    lines.append("")

    lines.append("8. MISSING INFORMATION")
    lines.append("-" * 22)
    if not report["missing_information"]:
        lines.append("(none)")
    for item in report["missing_information"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("9. FOLLOW-UP TOPICS")
    lines.append("-" * 19)
    if not report["follow_up_topics"]:
        lines.append("(none)")
    for topic in report["follow_up_topics"]:
        lines.append(f"- {topic['topic']}")
    lines.append("")

    if "diagnostics" in report:
        lines.append("DIAGNOSTICS")
        lines.append("-" * 11)
        for key, value in report["diagnostics"].items():
            lines.append(f"{key}: {value}")

    return "\n".join(lines)


def render_focused_text(report: dict[str, Any]) -> str:
    """Render the default, scope-focused Pre-Tool Triage output.

    Consumes `report["focused"]` (a pure projection built by
    `build_focused_view`) so the console and JSON representations can never
    drift. The full, verbose sections remain available via `render_text`
    (`--verbose`) and in the JSON document."""
    focused = report.get("focused") or build_focused_view(report)
    title = "Pre-Tool Triage (focused)"
    lines: list[str] = [title, "=" * len(title), ""]

    lines.append("PROPERTY CONTEXT")
    lines.append("-" * 16)
    if not focused["property_context"]:
        lines.append("(no tool-relevant property fields extracted)")
    for item in focused["property_context"]:
        lines.append(f"- {item['field']}: {item['value']}")
    lines.append("")

    lines.append("RELEVANT CLAIMS FOUND")
    lines.append("-" * 21)
    if not focused["relevant_claims"]:
        lines.append("(no nearby or price-comparison claims were extracted)")
        lines.append("")
    for claim in focused["relevant_claims"]:
        lines.append(f"- {claim['text']}")
        lines.append(f"  source: {claim['source']}")
        lines.append(f"  type: {claim['type']}")
        lines.append(f"  supported by current tools: {'yes' if claim['supported'] else 'no'}")
        lines.append(f"  reason: {claim['reason']}")
        if claim["supported"]:
            if claim.get("supported_categories"):
                lines.append(f"  categories: {claim['supported_categories']}")
            lines.append(f"  planned tool: {claim.get('planned_tool')}")
            lines.append(f"  supports_claim_ids: {claim.get('supports_claim_ids')}")
        else:
            lines.append("  tool: none")
        lines.append("")

    price = focused["price"]
    lines.append("PRICE")
    lines.append("-" * 5)
    lines.append(f"- asking price found: {'yes' if price['asking_price_found'] else 'no'}")
    lines.append(
        "- comparative listing claim found: "
        f"{'yes' if price['comparative_listing_claim_found'] else 'no'}"
    )
    lines.append(
        "- comparative buyer question found: "
        f"{'yes' if price['comparative_buyer_question_found'] else 'no'}"
    )
    price_line = f"- price_signal: {price['price_signal']}"
    if price["price_signal_missing_inputs"]:
        price_line += f" (missing: {', '.join(price['price_signal_missing_inputs'])})"
    lines.append(price_line)
    lines.append("")

    lines.append("FINAL TOOL PLAN")
    lines.append("-" * 15)
    if not focused["final_tool_plan"]:
        lines.append("No external evidence tools needed.")
        lines.append("")
    for index, entry in enumerate(focused["final_tool_plan"], start=1):
        lines.append(f"{index}. {entry['tool_name']}")
        if entry["supports_claim_ids"]:
            lines.append(f"   supports: {entry['supports_claim_ids']}")
        if entry.get("categories"):
            lines.append(f"   categories: {entry['categories']}")
        if entry["depends_on"]:
            lines.append(f"   depends_on: {entry['depends_on']}")
        if entry["resolved"]:
            lines.append("   input:")
        else:
            lines.append("   input (unresolved until dependency runs):")
        for sub in json.dumps(entry["input"], ensure_ascii=False, indent=2).splitlines():
            lines.append(f"   {sub}")
        lines.append("")

    lines.append("MISSING INFORMATION")
    lines.append("-" * 19)
    if not focused["missing_information"]:
        lines.append("(none affecting a relevant nearby/price check)")
    for item in focused["missing_information"]:
        lines.append(f"- {item}")
    lines.append("")

    if focused["other_listing_content_present"]:
        lines.append("OTHER LISTING CONTENT")
        lines.append("-" * 21)
        lines.append(
            "Additional seller claims were present but are outside the current "
            "evidence scope and were not analyzed for external verification."
        )
        lines.append("(re-run with --verbose to see the full claim-by-claim analysis)")

    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an executable Pre-Tool Triage (docs/pre_tool_triage.md) "
            "from the real triage_planner + plan_validator pipeline, stopping "
            "before any tool is executed."
        )
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--listing", type=str, help="Raw listing text.")
    source.add_argument("--file", type=str, help="Path to a file containing the listing text.")
    source.add_argument(
        "--example",
        type=int,
        choices=sorted(EXAMPLE_LISTINGS),
        help="Use one of the built-in example listings.",
    )
    source.add_argument(
        "--gonen-fixture",
        action="store_true",
        help=(
            "Replay the captured Gonen 14 typed TriagePlan fixture "
            "(tests/agent/test_claim_retention_regression._gonen_14_plan) "
            "through FakeLLMClient. This is the previously authorized real "
            "LLMod output for that listing; no new LLM call is made."
        ),
    )
    source.add_argument(
        "--from-report",
        type=str,
        default=None,
        help=(
            "Re-render a previously saved Pre-Tool Triage report JSON "
            "(e.g. artifacts/regressions/derech-hashalom/pre-tool-triage.json) "
            "through the focused view. Reads a saved artifact only - makes no "
            "LLM call and runs no pipeline. Ignores --real-llm/--fake-llm."
        ),
    )

    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--real-llm",
        action="store_true",
        help="Call the real LLMod.ai model through the existing LLModClient.",
    )
    mode.add_argument(
        "--fake-llm",
        action="store_true",
        help=(
            "Use FakeLLMClient with a pre-built plan. Requires --example or "
            "--gonen-fixture (FakeLLMClient cannot plan arbitrary text)."
        ),
    )

    parser.add_argument(
        "--question",
        type=str,
        default=None,
        help="Optional buyer question to preserve in the report.",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON document instead.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Render the full, broad Pre-Tool Triage output (every atomic "
            "claim, every verification bucket, follow-up topics) instead of "
            "the default scope-focused view. The JSON document is unchanged "
            "and always contains both."
        ),
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help=(
            "Write the report as JSON to this path (created from the same "
            "in-memory dict used for stdout)."
        ),
    )
    return parser.parse_args(argv)


def _gonen_fixture_plan() -> TriagePlan:
    """The captured Gonen 14 typed fixture, imported (never duplicated) from
    the regression suite that owns it."""
    from tests.agent.test_claim_retention_regression import _gonen_14_plan

    return _gonen_14_plan()


def _resolve_listing_text(args: argparse.Namespace) -> str:
    if args.example is not None:
        return EXAMPLE_LISTINGS[args.example]
    if args.file:
        return Path(args.file).read_text(encoding="utf-8").strip()
    return args.listing


def _emit(report: dict[str, Any], args: argparse.Namespace) -> None:
    """Serialize/print one report per the CLI flags. The JSON document
    always contains both the full and focused views; the console defaults
    to the focused view unless `--verbose` is given."""
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")

    if args.json:
        print(serialized)
    elif args.verbose:
        print(render_text(report))
    else:
        print(render_focused_text(report))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # --from-report: re-render a saved report artifact through the focused
    # view. Reads a file only - never a pipeline run, never an LLM call.
    if args.from_report:
        report_path = Path(args.from_report)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read --from-report {report_path}: {exc}", file=sys.stderr)
            return 2
        if "focused" not in report:
            report["focused"] = build_focused_view(report)
        _emit(report, args)
        return 0

    if not (args.real_llm or args.fake_llm):
        print(
            "error: one of --real-llm / --fake-llm is required (or use "
            "--from-report to re-render a saved report artifact).",
            file=sys.stderr,
        )
        return 2
    no_source = (
        args.listing is None
        and args.file is None
        and args.example is None
        and not args.gonen_fixture
    )
    if no_source:
        print(
            "error: a listing source is required: one of --listing / --file / "
            "--example / --gonen-fixture (or --from-report).",
            file=sys.stderr,
        )
        return 2

    fixture_mode = bool(args.gonen_fixture)
    if args.fake_llm and args.example is None and not fixture_mode:
        print(
            "error: --fake-llm requires --example {1,2,3} or --gonen-fixture: "
            "FakeLLMClient only replays pre-built plans, it cannot plan "
            "arbitrary --listing/--file text without a real LLM call.",
            file=sys.stderr,
        )
        return 2
    if args.real_llm and fixture_mode:
        print(
            "error: --gonen-fixture replays a captured plan and must be used "
            "with --fake-llm (it never makes a real LLM call).",
            file=sys.stderr,
        )
        return 2

    tracer = StepTracer()
    settings = get_settings()

    if fixture_mode:
        plan = _gonen_fixture_plan()
        client: LLMClient = FakeLLMClient()
        client.set_response(TRIAGE_PLANNER, plan)
        listing_text = "GONEN 14 (captured fixture replay)"
        llm_mode = "fake"
    elif args.fake_llm:
        listing_text = _resolve_listing_text(args)
        client = FakeLLMClient()
        client.set_response(TRIAGE_PLANNER, EXAMPLE_PLANS[args.example])
        llm_mode = "fake"
    else:
        listing_text = _resolve_listing_text(args)
        real_settings = settings.model_copy(update={"llm_mode": "llmod"})
        try:
            client = get_llm_client(real_settings, step_tracer=tracer)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        llm_mode = "real"

    report = asyncio.run(
        run_pre_tool_triage(
            listing_text,
            client,
            tracer,
            settings,
            llm_mode,
            buyer_question=args.question,
        )
    )

    _emit(report, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
