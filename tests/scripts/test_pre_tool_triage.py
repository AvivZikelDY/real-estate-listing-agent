"""Tests for `scripts/pre_tool_triage.py` (the executable Pre-Tool Triage).

Proves the diagnostic is a faithful projection of the production
`triage_planner` -> `plan_validator` -> `tool_input_builder` pipeline: it
reuses (never re-implements) `validate_plan`, `find_relevant_claim_ids`,
`build_tool_input`, `verification_decision`, and `_claim_priority`, never
executes a tool, never calls `report_writer`, and never touches MCP.

Every test runs fully offline against the captured Gonen 14 typed fixture
(`tests/agent/test_claim_retention_regression._gonen_14_plan`) or the
built-in example plans, through `FakeLLMClient`. No real LLM call is made.
"""

from __future__ import annotations

import json

import pytest

import scripts.pre_tool_triage as script
from src.listing_agent.agent.evidence_mapper import find_relevant_claim_ids
from src.listing_agent.agent.plan_validator import validate_plan
from src.listing_agent.agent.tool_input_builder import build_tool_input
from tests.agent.test_claim_retention_regression import _gonen_14_plan


def _gonen_report() -> dict:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    return script.build_pre_tool_triage_report(raw, validated)


# --- 1 & 2: retained claims are exactly the validated production plan ------


def test_retained_claims_use_the_validated_production_plan() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    report = script.build_pre_tool_triage_report(raw, validated)

    reported_ids = [c["claim_id"] for c in report["retained_atomic_claims"]]
    reported_texts = [c["text"] for c in report["retained_atomic_claims"]]

    assert reported_ids == [c.id for c in validated.claims]
    assert reported_texts == [c.text for c in validated.claims]


def test_retained_claims_match_a_fresh_validate_plan_call_exactly() -> None:
    """The report never caches or mutates a plan: two independent
    `validate_plan` calls on the same input yield the same retained set."""
    report = _gonen_report()
    independent = validate_plan(_gonen_14_plan())

    assert [c["claim_id"] for c in report["retained_atomic_claims"]] == [
        c.id for c in independent.claims
    ]


# --- 3: removed claims match deterministic selection decisions -------------


def test_removed_claims_match_raw_minus_validated_with_reasons() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    report = script.build_pre_tool_triage_report(raw, validated)

    retained_ids = {c.id for c in validated.claims}
    raw_ids = {c.id for c in raw.claims}
    expected_removed_ids = raw_ids - retained_ids

    reported_removed_ids = {c["claim_id"] for c in report["removed_claims"]}
    assert reported_removed_ids == expected_removed_ids

    reasons = {c["claim_id"]: c["reason"] for c in report["removed_claims"]}
    # c3 is the duplicate address claim (removed by dedup); the rest are
    # dropped by the priority-ordered cap.
    assert reasons["c3"] == "deduplicated"
    assert reasons["c6"] == "capped_max_claims"
    assert reasons["c8"] == "capped_max_claims"
    assert reasons["c12"] == "capped_max_claims"


# --- 4: supports_claim_ids match production linkage ------------------------


def test_supports_claim_ids_match_find_relevant_claim_ids() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    report = script.build_pre_tool_triage_report(raw, validated)

    call_by_tool = {call.tool: call for call in validated.tool_calls}
    for entry in report["tool_plan"]:
        call = call_by_tool[entry["tool_name"]]
        expected = find_relevant_claim_ids(call.tool, validated.claims, call)
        assert entry["supports_claim_ids"] == expected


# --- 5: prepared tool input uses production build_tool_input() -------------


def test_prepared_geo_input_equals_direct_build_tool_input() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    report = script.build_pre_tool_triage_report(raw, validated)

    geo_call = next(c for c in validated.tool_calls if c.tool == "geo_resolver")
    expected = build_tool_input(
        geo_call, validated.property_details, geo_output=None, claims=validated.claims
    ).model_dump(mode="json")

    prepared = next(e for e in report["prepared_tool_inputs"] if e["tool_name"] == "geo_resolver")
    assert prepared["resolved"] is True
    assert prepared["value"] == expected


# --- 6: daycare normalization is preserved --------------------------------


def test_gonen_nearby_categories_normalize_to_daycare() -> None:
    report = _gonen_report()
    nearby = next(e for e in report["tool_plan"] if e["tool_name"] == "nearby_services")
    assert nearby["planner_arguments"].get("categories") == ["daycare"]


# --- 7: transaction-structure claim retained but has no tool --------------


def test_transaction_structure_claim_retained_without_a_tool() -> None:
    report = _gonen_report()
    # The rent-then-purchase commitment (c11) survives as a retained claim.
    transaction_claim = next(
        c for c in report["retained_atomic_claims"] if "השכרה" in c["text"] and "רכישה" in c["text"]
    )
    linked_ids = {
        claim_id
        for entry in report["tool_plan"]
        for claim_id in entry["supports_claim_ids"]
    }
    assert transaction_claim["claim_id"] not in linked_ids


# --- 8: price_signal never appears without comparative intent --------------


def test_price_signal_absent_without_comparative_intent() -> None:
    report = _gonen_report()
    assert "price_signal" not in {e["tool_name"] for e in report["tool_plan"]}


# --- 9: diagnostic never executes tools -----------------------------------


def test_report_has_no_tool_execution_results() -> None:
    report = _gonen_report()
    for forbidden in ("tool_results", "execution_status", "evidence", "results"):
        assert forbidden not in report


def _import_lines() -> str:
    with open(script.__file__, encoding="utf-8") as f:
        return "".join(
            line for line in f if line.lstrip().startswith(("import ", "from "))
        )


def test_module_never_imports_a_tool_executor() -> None:
    imports = _import_lines()
    for forbidden in ("LocalToolExecutor", "McpToolExecutor", "ToolExecutor", "ToolRegistry"):
        assert forbidden not in imports


@pytest.mark.asyncio
async def test_run_pre_tool_triage_never_calls_a_tool_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, patch

    client = script.FakeLLMClient()
    client.set_response("triage_planner", _gonen_14_plan())
    tracer = script.StepTracer()
    settings = script.get_settings()

    with patch(
        "src.listing_agent.agent.tool_executor.LocalToolExecutor.execute",
        new=AsyncMock(side_effect=AssertionError("tool executor must never be called")),
    ):
        report = await script.run_pre_tool_triage(
            "listing", client, tracer, settings, "fake"
        )
    assert report["retained_atomic_claims"]


# --- 10 & 11: never calls report_writer, never calls MCP ------------------


def test_module_never_references_report_writer_or_mcp() -> None:
    imports = _import_lines()
    assert "report_writer" not in imports
    assert "Mcp" not in imports
    assert "MCP" not in imports
    for forbidden in (
        "evidence_collector",
        "evidence_bundle_builder",
    ):
        assert forbidden not in imports


# --- 12: diagnostic never alters the production plan ----------------------


def test_diagnostic_does_not_mutate_the_production_plan() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    before_claims = [(c.id, c.text, c.category) for c in validated.claims]
    before_tools = [(c.tool, dict(c.arguments)) for c in validated.tool_calls]

    script.build_pre_tool_triage_report(raw, validated)

    after_claims = [(c.id, c.text, c.category) for c in validated.claims]
    after_tools = [(c.tool, dict(c.arguments)) for c in validated.tool_calls]
    assert before_claims == after_claims
    assert before_tools == after_tools

    # And a fresh validation of the same input is bit-identical.
    revalidated = validate_plan(_gonen_14_plan())
    assert [c.model_dump(mode="json") for c in revalidated.claims] == [
        c.model_dump(mode="json") for c in validated.claims
    ]
    assert [c.model_dump(mode="json") for c in revalidated.tool_calls] == [
        c.model_dump(mode="json") for c in validated.tool_calls
    ]


# --- 13: JSON and console representations derive from the same dict --------


def test_json_output_file_matches_in_memory_dict(tmp_path) -> None:
    out = tmp_path / "gonen.json"
    exit_code = script.main(
        ["--fake-llm", "--gonen-fixture", "--json", "--output-json", str(out)]
    )
    assert exit_code == 0

    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    from src.listing_agent.config import get_settings

    settings = get_settings()
    expected = script.build_pre_tool_triage_report(raw, validated)
    # The CLI attaches diagnostics; compare everything except the timing-only
    # diagnostics block, which is derived from the same dict either way.
    written = json.loads(out.read_text(encoding="utf-8"))
    written.pop("diagnostics", None)
    assert written == expected

    # The text renderer consumes the same structure round-tripped through JSON.
    rendered = script.render_text(json.loads(json.dumps(expected)))
    assert "Pre-Tool Triage" in rendered
    _ = settings  # settings exercised to prove get_settings path is import-safe


# --- 14: secrets are never written ----------------------------------------


def test_no_secret_looking_values_in_output() -> None:
    serialized = json.dumps(_gonen_report(), ensure_ascii=False).lower()
    for marker in ("api_key", "secret", "token", "password", "authorization", "credential"):
        assert marker not in serialized


# ==========================================================================
# Focused Pre-Tool Triage view (scope-refocused default output).
# ==========================================================================

from pathlib import Path  # noqa: E402

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "regressions"
    / "derech-hashalom"
    / "pre-tool-triage.json"
)


def _claim(claim_id: str, text: str, canonical: str | None, source=None) -> dict:
    return {
        "claim_id": claim_id,
        "text": text,
        "source": source,
        "type": "factual",
        "category": canonical,
        "canonical_category": canonical,
        "priority": 2,
        "verification_decision": "CHECKABLE_WITH_TOOL",
    }


def _report(
    *,
    property_facts: dict | None = None,
    retained: list[dict] | None = None,
    removed: list[dict] | None = None,
    tool_plan: list[dict] | None = None,
    prepared: list[dict] | None = None,
) -> dict:
    return {
        "property_facts": property_facts or {"listing_type": "purchase"},
        "retained_atomic_claims": retained or [],
        "removed_claims": removed or [],
        "tool_plan": tool_plan or [],
        "prepared_tool_inputs": prepared or [],
    }


def _geo_nearby_plan(categories: list[str]) -> tuple[list[dict], list[dict]]:
    tool_plan = [
        {
            "call_id": "tool-call-1",
            "tool_name": "geo_resolver",
            "supports_claim_ids": ["claim-0"],
            "dependencies": [],
            "planner_arguments": {},
        },
        {
            "call_id": "tool-call-2",
            "tool_name": "nearby_services",
            "supports_claim_ids": ["claim-0"],
            "dependencies": ["tool-call-1"],
            "planner_arguments": {},
        },
    ]
    prepared = [
        {"call_id": "tool-call-1", "tool_name": "geo_resolver", "resolved": True, "value": {}},
        {
            "call_id": "tool-call-2",
            "tool_name": "nearby_services",
            "resolved": False,
            "unresolved_dependency_reason": "geo_resolver did not produce resolved coordinates",
            "value": {"categories": categories, "radius_m": 500},
        },
    ]
    return tool_plan, prepared


# 1. Unrelated seller claims do not clutter the focused output.
def test_unrelated_claims_excluded_from_focused_relevant_claims() -> None:
    report = _report(
        retained=[
            _claim("claim-0", "פארק וולפסון", "nearby_amenities"),
            _claim("claim-1", "התוכנית הופקדה", "transaction_structure"),
            _claim("claim-2", "ממ\"ד", None),
            _claim("claim-3", "דירה מהממת", "condition"),
        ],
    )
    focused = script.build_focused_view(report)
    texts = [c["text"] for c in focused["relevant_claims"]]
    assert texts == ["פארק וולפסון"]
    assert focused["other_listing_content_present"] is True


# 2. A supported nearby claim appears (maps to a frozen category).
def test_supported_nearby_claim_is_marked_supported() -> None:
    tool_plan, prepared = _geo_nearby_plan(["park"])
    report = _report(
        property_facts={"listing_type": "purchase", "raw_address": "דרך השלום, תל אביב"},
        retained=[_claim("claim-0", "פארק וולפסון", "nearby_amenities")],
        tool_plan=tool_plan,
        prepared=prepared,
    )
    focused = script.build_focused_view(report)
    claim = focused["relevant_claims"][0]
    assert claim["supported"] is True
    assert claim["type"] == "nearby"
    assert claim["supported_categories"] == ["park"]
    assert claim["planned_tool"] == "nearby_services"


# 3. An unsupported nearby category appears but has no tool.
def test_unsupported_nearby_category_has_no_tool() -> None:
    report = _report(
        retained=[_claim("claim-0", "רכבת קלה", "nearby_amenities")],
    )
    focused = script.build_focused_view(report)
    claim = focused["relevant_claims"][0]
    assert claim["supported"] is False
    assert claim["type"] == "nearby"
    assert claim["reason"] == "unsupported nearby category"
    assert claim["planned_tool"] is None


# 4. Price exists but no comparative claim -> no price_signal.
def test_price_present_without_comparison_yields_no_price_signal() -> None:
    report = _report(
        property_facts={"listing_type": "purchase", "asking_price": 2_890_000.0, "sqm": 60.0},
        retained=[_claim("claim-0", "פארק וולפסון", "nearby_amenities")],
    )
    focused = script.build_focused_view(report)
    assert focused["price"]["asking_price_found"] is True
    assert focused["price"]["comparative_listing_claim_found"] is False
    assert focused["price"]["price_signal"] == "NOT NEEDED"


# 5. A buyer comparative question -> price_signal NEEDED when inputs exist.
def test_buyer_price_question_triggers_price_signal_when_inputs_exist() -> None:
    report = _report(
        property_facts={"listing_type": "purchase", "asking_price": 4_700_000.0, "sqm": 100.0},
        retained=[
            _claim(
                "claim-derived-price-comparison",
                "Buyer question: is the asking price below or at market price?",
                "price_comparison",
                source="derived",
            )
        ],
        tool_plan=[
            {
                "call_id": "tool-call-2",
                "tool_name": "price_signal",
                "supports_claim_ids": ["claim-derived-price-comparison"],
                "dependencies": ["tool-call-1"],
                "planner_arguments": {},
            }
        ],
        prepared=[
            {
                "call_id": "tool-call-2",
                "tool_name": "price_signal",
                "resolved": False,
                "unresolved_dependency_reason": "geo",
                "value": {"asking_price": 4_700_000.0, "area_sqm": 100.0},
            }
        ],
    )
    focused = script.build_focused_view(report)
    assert focused["price"]["comparative_buyer_question_found"] is True
    assert focused["price"]["price_signal"] == "NEEDED"


# 6. A comparative claim but missing required price input -> MISSING INPUT.
def test_price_signal_missing_input_is_reported_and_tool_skipped() -> None:
    report = _report(
        property_facts={"listing_type": "purchase"},
        retained=[_claim("claim-0", "מחיר מציאה", "price_comparison")],
        tool_plan=[
            {
                "call_id": "tool-call-2",
                "tool_name": "price_signal",
                "supports_claim_ids": ["claim-0"],
                "dependencies": ["tool-call-1"],
                "planner_arguments": {},
            }
        ],
        prepared=[
            {
                "call_id": "tool-call-2",
                "tool_name": "price_signal",
                "resolved": False,
                "unresolved_dependency_reason": "geo",
                "value": {"asking_price": None, "area_sqm": None},
            }
        ],
    )
    focused = script.build_focused_view(report)
    assert focused["price"]["price_signal"] == "MISSING INPUT"
    assert focused["price"]["price_signal_missing_inputs"] == ["asking_price", "area_sqm"]
    assert "asking price required for price comparison" in focused["missing_information"]
    assert "sqm required by price_signal" in focused["missing_information"]


# 7. A nearby claim works without any price information.
def test_nearby_claim_without_price_produces_geo_and_nearby_only() -> None:
    tool_plan, prepared = _geo_nearby_plan(["park"])
    report = _report(
        property_facts={"listing_type": "purchase", "raw_address": "דרך השלום, תל אביב"},
        retained=[_claim("claim-0", "פארק וולפסון", "nearby_amenities")],
        tool_plan=tool_plan,
        prepared=prepared,
    )
    focused = script.build_focused_view(report)
    tool_names = [e["tool_name"] for e in focused["final_tool_plan"]]
    assert tool_names == ["geo_resolver", "nearby_services"]
    assert focused["price"]["price_signal"] == "NOT NEEDED"


# 8. No relevant claims -> no tool plan.
def test_no_relevant_claims_produces_no_tool_plan() -> None:
    report = _report(
        retained=[_claim("claim-0", "דירה מהממת", "condition")],
    )
    focused = script.build_focused_view(report)
    assert focused["relevant_claims"] == []
    assert focused["final_tool_plan"] == []


# 9. Only actual planner-extracted claims are printed (never invented).
def test_focused_claims_are_a_subset_of_retained_claims() -> None:
    report = _report(
        retained=[
            _claim("claim-0", "פארק וולפסון", "nearby_amenities"),
            _claim("claim-1", "מחסן", None),
        ],
    )
    focused = script.build_focused_view(report)
    retained_texts = {c["text"] for c in report["retained_atomic_claims"]}
    for claim in focused["relevant_claims"]:
        assert claim["text"] in retained_texts


# 10. Saved real planner replay does not manufacture missing claims.
def test_derech_hashalom_replay_does_not_manufacture_claims() -> None:
    report = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    focused = script.build_focused_view(report)

    nearby = {c["text"]: c for c in focused["relevant_claims"] if c["type"] == "nearby"}
    # Wolfson Park was extracted and maps to a frozen category.
    assert nearby["פארק וולפסון,"]["supported"] is True
    assert nearby["פארק וולפסון,"]["supported_categories"] == ["park"]
    # Light rail was extracted but is outside the frozen categories.
    assert nearby["רכבת קלה,"]["supported"] is False
    # The shelter ("מקלט") was never extracted as a claim -> never invented.
    assert all("מקלט" not in c["text"] for c in focused["relevant_claims"])
    # No comparative price claim/question -> no price_signal.
    assert focused["price"]["price_signal"] == "NOT NEEDED"
    assert focused["price"]["comparative_buyer_question_found"] is False


# 11. The full, verbose diagnostic data remains available.
def test_full_verbose_sections_still_present_in_report() -> None:
    report = _gonen_report()
    for key in (
        "retained_atomic_claims",
        "removed_claims",
        "verification_still_required",
        "follow_up_topics",
        "focused",
    ):
        assert key in report
    rendered = script.render_text(report)
    assert "3. RETAINED ATOMIC CLAIMS" in rendered
    focused_rendered = script.render_focused_text(report)
    assert "RELEVANT CLAIMS FOUND" in focused_rendered


# 12. The focused projection never mutates the production plan/report.
def test_focused_view_is_a_pure_projection() -> None:
    raw = _gonen_14_plan()
    validated = validate_plan(raw)
    before = [(c.id, c.text, c.category) for c in validated.claims]
    report = script.build_pre_tool_triage_report(raw, validated)
    script.build_focused_view(report)
    after = [(c.id, c.text, c.category) for c in validated.claims]
    assert before == after
    # A second independent validation is bit-identical (production semantics
    # unchanged by the focused refactor).
    revalidated = validate_plan(_gonen_14_plan())
    assert [c.model_dump(mode="json") for c in revalidated.claims] == [
        c.model_dump(mode="json") for c in validated.claims
    ]


# --- CLI: --from-report re-renders a saved artifact (no LLM call) ----------


def test_from_report_renders_focused_view(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--from-report", str(_ARTIFACT)]) == 0
    out = capsys.readouterr().out
    assert "Pre-Tool Triage (focused)" in out
    assert "FINAL TOOL PLAN" in out


# --- CLI guards ------------------------------------------------------------


def test_fake_mode_requires_example_or_fixture() -> None:
    assert script.main(["--fake-llm", "--listing", "x"]) == 2


def test_gonen_fixture_rejects_real_llm() -> None:
    assert script.main(["--real-llm", "--gonen-fixture"]) == 2


def test_fake_example_runs_and_prints_sections(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--fake-llm", "--example", "1", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "1. PROPERTY FACTS" in out
    assert "3. RETAINED ATOMIC CLAIMS" in out


def test_buyer_question_is_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(
        ["--fake-llm", "--example", "1", "--question", "מה המחיר?", "--verbose"]
    ) == 0
    out = capsys.readouterr().out
    assert "buyer_question: מה המחיר?" in out
