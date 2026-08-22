"""Standalone test runner for the live tool implementations - no LLM involved.

Runs the deterministic half of the pipeline (everything the app does once
the planner has finished) against a set of hand-written `TriagePlan`
fixtures, and checks what the tools actually returned:

```text
canned TriagePlan (no LLM call, ever)
-> plan_validator.validate_plan
-> tool_input_builder.build_tool_input
-> LocalToolExecutor -> ToolRegistry -> Live* / Mock* tools
-> evidence_collector.collect_evidence
-> evidence_bundle_builder.build_evidence_bundle
-> STOP (report_writer is never called)
```

No `LLMClient` of any kind is imported or constructed, so this never makes
an LLM call and never needs API credentials. The plans come from
`scripts/listing_pipeline_scenarios.py`, the same fixtures
`inspect_listing_pipeline.py` uses.

Results go to two places at once: the `logging` logger (stdout) and a
timestamped report file under
`src/listing_agent/tools/live_implementations/test_results/`.

Usage:
    python scripts/run_live_tool_tests.py                  # live tools
    python scripts/run_live_tool_tests.py --tools mock     # offline, instant
    python scripts/run_live_tool_tests.py --case 11        # one case only
    python scripts/run_live_tool_tests.py --out report.txt # custom file

WARNING: with `--tools live` every case makes real HTTP calls to govmap and
Overpass. Overpass mirrors fail over on 504/timeout, so a single case can
take over a minute and the full run several minutes. Progress is logged as
each case starts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.listing_pipeline_scenarios import SCENARIOS  # noqa: E402
from src.listing_agent.agent.evidence_bundle_builder import build_evidence_bundle  # noqa: E402
from src.listing_agent.agent.evidence_collector import collect_evidence  # noqa: E402
from src.listing_agent.agent.plan_validator import PlanValidationError, validate_plan  # noqa: E402
from src.listing_agent.agent.schemas import ToolEvidence  # noqa: E402
from src.listing_agent.agent.step_tracer import StepTracer  # noqa: E402
from src.listing_agent.agent.tool_executor.local import LocalToolExecutor  # noqa: E402
from src.listing_agent.config import get_settings  # noqa: E402
from src.listing_agent.tools.dependency import get_tool_registry  # noqa: E402

LOGGER = logging.getLogger("live_tool_tests")

# Default location for the generated report, as requested: inside the
# live_implementations package. Generated output, not source - safe to
# delete, and worth gitignoring if you don't want runs committed.
DEFAULT_RESULTS_DIR = (
    _REPO_ROOT / "src" / "listing_agent" / "tools" / "live_implementations" / "test_results"
)


@dataclass(frozen=True)
class ToolExpectation:
    """What one tool should have produced for a case.

    `outcome` is the tool's typed *business* outcome (`resolved` /
    `ambiguous` / `not_found` for geo_resolver, `compared` /
    `insufficient_data` / `no_comparable_data` for price_signal).
    `nearby_services` has no outcome field, so it stays `None` there.

    Only checked under `--tools live`: the mock implementations answer from
    a fixed dataset and resolve every address, so live-data outcomes like
    "ambiguous" are meaningless against them.
    """

    status: str
    outcome: str | None = None


@dataclass(frozen=True)
class TestCase:
    """One scenario plus the assertions to make about it."""

    scenario_id: int
    what_it_proves: str
    # Tools that must survive `plan_validator` and appear in the final plan.
    expected_planned: tuple[str, ...]
    # Tools that must NOT appear in the final plan.
    expected_dropped: tuple[str, ...] = ()
    # Per-tool expectations, checked only under `--tools live`.
    live_expectations: dict[str, ToolExpectation] = field(default_factory=dict)


# The cases. Each targets one behaviour that only matters once the tools
# are real; the plan-level checks still work offline under --tools mock.
TEST_CASES: tuple[TestCase, ...] = (
    TestCase(
        scenario_id=1,
        what_it_proves="the happy path: all three tools run and return usable data",
        expected_planned=("geo_resolver", "nearby_services", "price_signal"),
        live_expectations={
            "geo_resolver": ToolExpectation(status="ok", outcome="resolved"),
            "nearby_services": ToolExpectation(status="ok"),
            "price_signal": ToolExpectation(status="ok", outcome="compared"),
        },
    ),
    TestCase(
        scenario_id=11,
        what_it_proves=(
            "an ambiguous address is a valid 'ok' answer with no coordinates, "
            "so dependent tools are SKIPPED rather than run on a guess"
        ),
        expected_planned=("geo_resolver", "nearby_services"),
        expected_dropped=("price_signal",),
        live_expectations={
            "geo_resolver": ToolExpectation(status="ok", outcome="ambiguous"),
            "nearby_services": ToolExpectation(status="skipped"),
        },
    ),
    TestCase(
        scenario_id=12,
        what_it_proves=(
            "a non-existent address returns not_found (a business outcome), "
            "never a tool error, and still skips its dependents cleanly"
        ),
        expected_planned=("geo_resolver", "nearby_services"),
        expected_dropped=("price_signal",),
        live_expectations={
            "geo_resolver": ToolExpectation(status="ok", outcome="not_found"),
            "nearby_services": ToolExpectation(status="skipped"),
        },
    ),
    TestCase(
        scenario_id=13,
        what_it_proves=(
            "plan_validator drops price_signal for a rental before it ever runs, "
            "while nearby_services still collects evidence normally"
        ),
        expected_planned=("geo_resolver", "nearby_services"),
        expected_dropped=("price_signal",),
        live_expectations={
            "geo_resolver": ToolExpectation(status="ok", outcome="resolved"),
            "nearby_services": ToolExpectation(status="ok"),
        },
    ),
    TestCase(
        scenario_id=14,
        what_it_proves=(
            "an address outside Tel Aviv still resolves and still gets nearby "
            "evidence; a plain listing-fact price with no comparative question/"
            "claim never triggers price_signal (price-provenance guard)"
        ),
        expected_planned=("geo_resolver", "nearby_services"),
        expected_dropped=("price_signal",),
        live_expectations={
            "geo_resolver": ToolExpectation(status="ok", outcome="resolved"),
            "nearby_services": ToolExpectation(status="ok"),
        },
    ),
)

CASES_BY_ID = {case.scenario_id: case for case in TEST_CASES}


def _evidence_outcome(evidence: ToolEvidence) -> str | None:
    """The tool's typed business outcome, or `None` if it has no outcome field."""
    return (evidence.output or {}).get("outcome")


def _check_case(
    case: TestCase,
    planned_tools: list[str],
    tool_results: list[ToolEvidence],
    *,
    live: bool,
) -> list[str]:
    """Return a list of failure descriptions (empty list = the case passed)."""
    failures: list[str] = []

    for tool in case.expected_planned:
        if tool not in planned_tools:
            failures.append(f"expected '{tool}' in the validated plan, but it was not planned")
    for tool in case.expected_dropped:
        if tool in planned_tools:
            failures.append(f"'{tool}' should have been dropped by plan_validator, but was planned")

    if not live:
        return failures

    by_tool = {evidence.tool: evidence for evidence in tool_results}
    for tool, expectation in case.live_expectations.items():
        evidence = by_tool.get(tool)
        if evidence is None:
            failures.append(f"'{tool}' produced no evidence entry at all")
            continue
        if evidence.status != expectation.status:
            failures.append(
                f"'{tool}' status was '{evidence.status}', expected '{expectation.status}' "
                f"({evidence.error or evidence.reason or 'no detail'})"
            )
        if expectation.outcome is not None:
            actual = _evidence_outcome(evidence)
            if actual != expectation.outcome:
                failures.append(
                    f"'{tool}' outcome was '{actual}', expected '{expectation.outcome}'"
                )

    return failures


def _describe_evidence(evidence: ToolEvidence) -> str:
    """A one-line human summary of what a tool actually returned."""
    parts = [f"status={evidence.status}"]

    outcome = _evidence_outcome(evidence)
    if outcome:
        parts.append(f"outcome={outcome}")

    output = evidence.output or {}
    if evidence.tool == "geo_resolver" and outcome == "resolved":
        parts.append(f"address={output.get('normalized_address')}")
        parts.append(f"lat/lon={output.get('lat')},{output.get('lon')}")
        parts.append(f"quality={output.get('resolution_quality')}")
    elif evidence.tool == "nearby_services" and evidence.status == "ok":
        counts = ", ".join(
            f"{entry['category']}={entry['count']}" for entry in output.get("categories", [])
        )
        parts.append(f"places={len(output.get('places', []))}")
        parts.append(f"counts=[{counts or 'none'}]")
        parts.append(f"quality={output.get('evidence_quality')}")
    elif evidence.tool == "price_signal" and outcome == "compared":
        parts.append(f"asking_per_sqm={output.get('asking_price_per_sqm')}")
        parts.append(f"median_per_sqm={output.get('median_comparison_price_per_sqm')}")
        parts.append(f"deviation={output.get('deviation_percent')}%")
        parts.append(f"comparables={output.get('comparison_count')}")

    if evidence.reason:
        parts.append(f"reason={evidence.reason}")
    if evidence.error:
        parts.append(f"error={evidence.error}")

    return " | ".join(parts)


async def _run_case(case: TestCase, tools_mode: str) -> tuple[bool, dict[str, Any]]:
    """Run one case end to end. Returns `(passed, details)`."""
    scenario = SCENARIOS[case.scenario_id]
    settings = get_settings().model_copy(update={"tools_mode": tools_mode})
    live = tools_mode == "live"

    LOGGER.info("")
    LOGGER.info("=" * 78)
    LOGGER.info("CASE %d - %s", case.scenario_id, scenario.title)
    LOGGER.info("=" * 78)
    LOGGER.info("proves : %s", case.what_it_proves)
    LOGGER.info("listing: %s", scenario.listing.replace("\n", " / "))
    LOGGER.info("question: %s", scenario.question or "none")
    LOGGER.info("planner proposed %d tool call(s) (canned plan, no LLM call)",
                len(scenario.plan.tool_calls))

    started = datetime.now()

    try:
        plan = validate_plan(
            scenario.plan,
            max_tool_calls=settings.max_tool_calls,
            max_claims=settings.max_claims,
        )
    except PlanValidationError as exc:
        LOGGER.error("plan_validator REJECTED the plan: %s", exc)
        return False, {
            "scenario_id": case.scenario_id,
            "title": scenario.title,
            "failures": [f"plan rejected by plan_validator: {exc}"],
            "duration_s": (datetime.now() - started).total_seconds(),
            "evidence": [],
        }

    planned_tools = [call.tool for call in plan.tool_calls]
    LOGGER.info("validator kept  : %s", planned_tools or "(none)")

    executor = LocalToolExecutor(
        get_tool_registry(settings), timeout_seconds=settings.tool_timeout_seconds
    )
    tracer = StepTracer()

    if live:
        LOGGER.info("executing against LIVE data sources - this can take a while...")

    tool_results, executed_tools = await collect_evidence(
        plan,
        executor,
        tracer,
        max_total_tool_execution_seconds=settings.max_total_tool_execution_seconds,
    )
    bundle = build_evidence_bundle(
        prompt=scenario.listing,
        plan=plan,
        tool_results=tool_results,
        executed_tools=executed_tools,
    )

    duration_s = (datetime.now() - started).total_seconds()
    LOGGER.info("actually executed: %s", executed_tools or "(none)")
    LOGGER.info("--- what each tool returned ---")
    for evidence in tool_results:
        LOGGER.info("  %-16s %s", evidence.tool, _describe_evidence(evidence))

    if bundle.limitations:
        LOGGER.info("--- limitations carried into the evidence bundle ---")
        for limitation in bundle.limitations:
            LOGGER.info("  - %s", limitation)

    failures = _check_case(case, planned_tools, tool_results, live=live)
    if failures:
        LOGGER.error("RESULT: FAIL (%.1fs)", duration_s)
        for failure in failures:
            LOGGER.error("  x %s", failure)
    else:
        checked = "plan + live tool outcomes" if live else "plan only (mock mode)"
        LOGGER.info("RESULT: PASS (%.1fs) - checked %s", duration_s, checked)

    return not failures, {
        "scenario_id": case.scenario_id,
        "title": scenario.title,
        "failures": failures,
        "duration_s": duration_s,
        "evidence": [(evidence.tool, _describe_evidence(evidence)) for evidence in tool_results],
    }


def _configure_logging(report_path: Path) -> None:
    """Send every log record to stdout and to the report file at once."""
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Hebrew tool output would raise UnicodeEncodeError on a cp1252 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    formatter = logging.Formatter("%(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(report_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(stream_handler)
    LOGGER.addHandler(file_handler)
    LOGGER.propagate = False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the live tools through the real post-planner pipeline using canned "
            "TriagePlans. Never calls an LLM."
        )
    )
    parser.add_argument(
        "--tools",
        choices=("live", "mock"),
        default="live",
        help=(
            "Which implementations to exercise. 'live' (default) makes real govmap "
            "and Overpass calls; 'mock' is offline and instant, and only the "
            "plan-level assertions are checked."
        ),
    )
    parser.add_argument(
        "--case",
        type=int,
        action="append",
        choices=sorted(CASES_BY_ID),
        help="Run only this case (repeatable). Defaults to all cases.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help=(
            "Where to write the report. Defaults to a timestamped file under "
            "src/listing_agent/tools/live_implementations/test_results/."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.out:
        report_path = Path(args.out).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = DEFAULT_RESULTS_DIR / f"tool_test_run_{stamp}_{args.tools}.txt"

    _configure_logging(report_path)

    cases = [CASES_BY_ID[case_id] for case_id in sorted(set(args.case))] if args.case else list(
        TEST_CASES
    )

    LOGGER.info("Live tool test run")
    LOGGER.info("started : %s", datetime.now().isoformat(timespec="seconds"))
    LOGGER.info("tools   : %s", args.tools)
    LOGGER.info("cases   : %s", [case.scenario_id for case in cases])
    LOGGER.info("llm     : never called - every plan is a fixture")
    LOGGER.info("report  : %s", report_path)

    results: list[dict[str, Any]] = []
    for case in cases:
        _, details = asyncio.run(_run_case(case, args.tools))
        results.append(details)

    passed = [r for r in results if not r["failures"]]
    failed = [r for r in results if r["failures"]]

    LOGGER.info("")
    LOGGER.info("=" * 78)
    LOGGER.info("SUMMARY: %d passed, %d failed, %.1fs total",
                len(passed), len(failed), sum(r["duration_s"] for r in results))
    LOGGER.info("=" * 78)
    for result in results:
        mark = "PASS" if not result["failures"] else "FAIL"
        LOGGER.info("  [%s] case %-3d %-55s %6.1fs",
                    mark, result["scenario_id"], result["title"], result["duration_s"])
        for failure in result["failures"]:
            LOGGER.info("           x %s", failure)

    LOGGER.info("")
    LOGGER.info("report written to: %s", report_path)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
