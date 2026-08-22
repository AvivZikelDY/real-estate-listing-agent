"""Tests for `scripts/inspect_listing_pipeline.py`.

Covers: running all 10 scenarios in both Planner and Evidence stage,
question-derived claim representation, claim source survival into the
Evidence Bundle, narrowed/general questions changing the tool plan,
family-suitability partial answerability, walking-time/school-quality
never being fabricated, missing price info blocking `price_signal`,
price evidence linking to `price_comparison` (not `price`), park-only
questions producing a park-only `nearby_services` input, one shared
`geo_resolver` call, `report_writer` never being invoked, JSON validity,
required text sections, and secret redaction.

All tests run fully offline (`--fake-llm`); no real-LLM test is part of
this suite (see the completion report for the separate, manually-run
real-LLM verification).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

import scripts.inspect_listing_pipeline as script
from scripts.listing_pipeline_scenarios import SCENARIOS
from src.listing_agent.agent.schemas import TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor.local import LocalToolExecutor
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.fake import FakeLLMClient


def _fake_settings(**overrides: object) -> Settings:
    base = {
        "llm_mode": "fake",
        "llmod_api_key": "",
        "llmod_base_url": "",
        "llmod_model": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _run(scenario_id: int, stage: str) -> dict:
    scenario = SCENARIOS[scenario_id]
    client = FakeLLMClient()
    from src.listing_agent.agent.module_names import TRIAGE_PLANNER

    client.set_response(TRIAGE_PLANNER, scenario.plan)
    tracer = StepTracer()
    settings = _fake_settings()
    return await script.build_pipeline_report(
        scenario.listing, scenario.question, client, tracer, settings, "fake", stage
    )


# --- 1: all scenarios run in Planner mode ---


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
async def test_all_scenarios_run_in_planner_mode(scenario_id: int) -> None:
    report = await _run(scenario_id, "planner")
    assert report["diagnostics"]["report_writer_called"] is False
    assert report["tool_results"] == []
    assert report["evidence_bundle"] is None


# --- 2: all scenarios run in Evidence mode where tools are expected ---


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
async def test_all_scenarios_run_in_evidence_mode(scenario_id: int) -> None:
    report = await _run(scenario_id, "evidence")
    scenario = SCENARIOS[scenario_id]
    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    assert set(scenario.expected_tools) <= planned_tools
    for forbidden in scenario.forbidden_tools:
        assert forbidden not in planned_tools
        assert forbidden not in report["diagnostics"]["tools_executed"]
    assert report["diagnostics"]["report_writer_called"] is False


# --- 3: unsupported scenarios run without calling any tool ---


@pytest.mark.asyncio
async def test_unsupported_scenario_calls_no_tool() -> None:
    report = await _run(7, "evidence")
    assert report["final_tool_plan"] == []
    assert report["diagnostics"]["tools_executed"] == []
    assert report["question_analysis"]["answerability"] == "unsupported"


@pytest.mark.asyncio
async def test_missing_information_scenario_calls_no_tool() -> None:
    report = await _run(8, "evidence")
    assert report["final_tool_plan"] == []
    assert report["diagnostics"]["tools_executed"] == []
    assert report["question_analysis"]["answerability"] == "missing_information"


# --- 4: question-derived claims are represented ---


@pytest.mark.asyncio
async def test_question_derived_claims_represented() -> None:
    report = await _run(4, "planner")
    qa = report["question_analysis"]
    assert qa["question_derived_claims"] == ["האם יש פארק קרוב לדירה?"]

    derived = [c for c in report["identified_claims"] if c["source"] == "user_question"]
    assert len(derived) == 1
    assert derived[0]["text"] == "האם יש פארק קרוב לדירה?"


# --- 5: question-source metadata survives into Evidence Bundle ---


@pytest.mark.asyncio
async def test_question_source_survives_into_evidence_bundle() -> None:
    report = await _run(4, "evidence")
    nearby_evidence = next(r for r in report["tool_results"] if r["tool"] == "nearby_services")
    # The nearby_services evidence must remain linked to the question-derived
    # claim id (evidence_mapper's own claim linkage), proving the source
    # distinction is not lost between claim identification and evidence.
    question_claim_ids = {
        c["claim_id"] for c in report["identified_claims"] if c["source"] == "user_question"
    }
    assert set(nearby_evidence["claim_ids"]) & question_claim_ids


# --- 6: narrow questions reduce the tool plan ---


@pytest.mark.asyncio
async def test_narrow_question_excludes_price_signal() -> None:
    report = await _run(9, "planner")
    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    assert "price_signal" not in planned_tools
    assert planned_tools == {"geo_resolver", "nearby_services"}
    nearby_call = next(e for e in report["final_tool_plan"] if e["tool_name"] == "nearby_services")
    assert nearby_call["planner_arguments"]["categories"] == ["park"]


# --- 7: general questions may inspect multiple objective claims ---


@pytest.mark.asyncio
async def test_general_question_inspects_multiple_objective_claims() -> None:
    report = await _run(10, "planner")
    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    # Phase 0: price_signal is never kept without comparative-price
    # provenance, even for a "general question" scenario with a plain price
    # listing fact.
    assert planned_tools == {"geo_resolver", "nearby_services"}


# --- 8: family suitability remains partially answerable ---


@pytest.mark.asyncio
async def test_family_suitability_is_partially_verifiable() -> None:
    report = await _run(2, "planner")
    assert report["question_analysis"]["answerability"] == "partially_verifiable"
    assert report["question_analysis"]["goal"] == "family_suitability"
    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    assert "price_signal" not in planned_tools


# --- 9: walking time is not fabricated ---


@pytest.mark.asyncio
async def test_walking_time_not_fabricated() -> None:
    report = await _run(5, "evidence")
    assert report["question_analysis"]["answerability"] == "partially_verifiable"
    for result in report["tool_results"]:
        observations = result.get("observations") or {}
        assert "walking_time" not in json.dumps(observations)
        assert "walking_minutes" not in json.dumps(observations)


# --- 10: school quality is not inferred ---


@pytest.mark.asyncio
async def test_school_quality_not_inferred() -> None:
    report = await _run(6, "evidence")
    assert report["question_analysis"]["answerability"] == "partially_verifiable"
    for result in report["tool_results"]:
        observations = result.get("observations") or {}
        dumped = json.dumps(observations)
        assert "quality" not in dumped
        assert "rating" not in dumped


# --- 11: missing price information blocks price_signal ---


@pytest.mark.asyncio
async def test_missing_price_information_blocks_price_signal() -> None:
    report = await _run(8, "planner")
    planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    assert "price_signal" not in planned_tools


# --- 12: price evidence is linked to price_comparison, not merely price ---


@pytest.mark.asyncio
async def test_price_evidence_linked_to_price_comparison_claim() -> None:
    report = await _run(3, "evidence")
    price_evidence = next(r for r in report["tool_results"] if r["tool"] == "price_signal")
    price_comparison_claim = next(
        c for c in report["identified_claims"] if c["category"] == "price_comparison"
    )
    price_claim = next(
        c
        for c in report["identified_claims"]
        if c["category"] == "price" and c["source"] == "listing"
    )
    assert price_comparison_claim["claim_id"] in price_evidence["claim_ids"]
    assert price_claim["claim_id"] not in price_evidence["claim_ids"]


# --- 13: park-only questions create park-only nearby input ---


@pytest.mark.asyncio
async def test_park_only_question_creates_park_only_input() -> None:
    report = await _run(4, "planner")
    nearby_input = next(
        e for e in report["prepared_tool_inputs"] if e["tool_name"] == "nearby_services"
    )
    assert nearby_input["resolved"] is False  # geo dependency never fabricated
    assert nearby_input["value"]["categories"] == ["park"]


# --- 14: one shared geo_resolver call serves dependent tools ---


@pytest.mark.asyncio
async def test_shared_geo_resolver_call_serves_dependents() -> None:
    report = await _run(1, "planner")
    geo_entries = [e for e in report["final_tool_plan"] if e["tool_name"] == "geo_resolver"]
    assert len(geo_entries) == 1
    supports = set(geo_entries[0]["supports_claim_ids"])
    nearby_claim = next(
        c for c in report["identified_claims"] if c["category"] == "nearby_amenities"
    )
    price_claim = next(
        c for c in report["identified_claims"] if c["category"] == "price_comparison"
    )
    assert nearby_claim["claim_id"] in supports
    assert price_claim["claim_id"] in supports


# --- 15: no report_writer call occurs ---


def test_module_never_imports_report_writer_or_execution_internals() -> None:
    with open(script.__file__, encoding="utf-8") as f:
        import_lines = [line for line in f if line.lstrip().startswith(("import ", "from "))]
    import_text = "".join(import_lines)
    assert "report_writer" not in import_text
    assert "McpToolExecutor" not in import_text
    assert "live_implementations" not in import_text


@pytest.mark.asyncio
async def test_local_tool_executor_never_called_in_planner_mode() -> None:
    with patch.object(LocalToolExecutor, "execute", new=AsyncMock()) as mock_execute:
        await _run(1, "planner")
        mock_execute.assert_not_called()


@pytest.mark.asyncio
async def test_local_tool_executor_called_only_for_planned_tools_in_evidence_mode() -> None:
    with patch.object(LocalToolExecutor, "execute", new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = __import__(
            "src.listing_agent.agent.tool_executor.result", fromlist=["ToolExecutionResult"]
        ).ToolExecutionResult(status="skipped", reason="test stub")
        report = await _run(1, "evidence")
        called_tools = {call.args[0].tool for call in mock_execute.await_args_list}
        planned_tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
        assert called_tools <= planned_tools


# --- 16: JSON mode is serializable ---


def test_json_mode_serializable_for_all_scenarios() -> None:
    exit_code = script.main(
        ["--stage", "planner", "--scenario", "all", "--fake-llm", "--json", "--allow-paid-batch"]
    )
    assert exit_code == 0


# --- 17: text mode contains all required sections ---


@pytest.mark.asyncio
async def test_text_mode_contains_all_required_sections() -> None:
    report = await _run(1, "evidence")
    text = script.render_text("1", report)
    for section in (
        "1. INPUT",
        "2. USER QUESTION ANALYSIS",
        "3. EXTRACTED LISTING FACTS",
        "4. IDENTIFIED CLAIMS",
        "5. CLAIMS NOT SENT TO TOOLS",
        "6. RAW PLANNER TOOL CALLS",
        "7. NORMALIZATION AND VALIDATION DECISIONS",
        "8. FINAL VALIDATED TOOL PLAN",
        "9. PREPARED TOOL INPUTS",
        # Heading names the tool mode actually used; without --live-tools
        # that is always "mock".
        "10. TOOL EXECUTION RESULTS (mock tools)",
        "11. EVIDENCE BUNDLE",
        "12. DIAGNOSTICS",
    ):
        assert section in text


# --- 18: secrets are redacted ---


def test_secrets_never_printed(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = script.main(["--stage", "planner", "--scenario", "1", "--fake-llm", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "LLMOD_API_KEY" not in captured.out
    assert "api_key" not in captured.out.lower() or "***REDACTED***" in captured.out


# --- 19: existing production tests remain unchanged (smoke check) ---


def test_scenarios_use_only_whitelisted_tools() -> None:
    for scenario in SCENARIOS.values():
        for call in scenario.plan.tool_calls:
            assert call.tool in ("geo_resolver", "nearby_services", "price_signal")


# --- 20: real-LLM tests are not part of normal CI (documented convention) ---


def test_real_llm_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "get_settings", lambda: _fake_settings())
    exit_code = script.main(["--stage", "planner", "--scenario", "1", "--real-llm"])
    # Real credentials are not configured (forced empty here regardless of
    # the ambient environment), so this must fail safely (never silently
    # fall back to FakeLLMClient, and never risk a real paid LLM call).
    assert exit_code == 2


# --- Additional: scenario-all summary table renders ---


def test_scenario_all_summary_renders(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = script.main(["--stage", "planner", "--scenario", "all", "--fake-llm"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SCENARIO SUMMARY" in captured.out
    assert "PASS" in captured.out or "FAIL" in captured.out


# --- 21: Phase 19 deviation regressions, using a deliberately deviant raw
# plan (mimicking a real-planner mistake) rather than the well-behaved
# scenario fixtures, so the diagnostic pipeline's own recovery/synthesis
# behavior is exercised end-to-end. ---


async def _run_with_plan(
    listing: str, question: str | None, plan: TriagePlan, stage: str
) -> dict:
    client = FakeLLMClient()
    from src.listing_agent.agent.module_names import TRIAGE_PLANNER

    client.set_response(TRIAGE_PLANNER, plan)
    tracer = StepTracer()
    settings = _fake_settings()
    return await script.build_pipeline_report(
        listing, question, client, tracer, settings, "fake", stage
    )


@pytest.mark.asyncio
async def test_diagnostic_recovers_omitted_nearby_services_call() -> None:
    from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan

    listing = (
        'דירת 4 חדרים מהממת ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח.\n'
        "קרובה לבתי ספר, גנים ופארקים.\nמחיר מציאה מתחת למחיר השוק."
    )
    deviant_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="דיזנגוף 100, תל אביב",
            asking_price=4_700_000,
            sqm=100,
            rooms=4,
        ),
        buyer_goal="price_validation",
        claims=[
            Claim(text="דירת 4 חדרים", type="factual", category="rooms"),
            Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
            Claim(
                text="קרובה לבתי ספר, גנים ופארקים",
                type="factual",
                category="nearby_amenities",
            ),
        ],
        # The planner forgot the nearby_services call entirely.
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )
    report = await _run_with_plan(listing, None, deviant_plan, "evidence")
    tools = {entry["tool_name"] for entry in report["final_tool_plan"]}
    assert tools == {"geo_resolver", "nearby_services", "price_signal"}
    nearby_entry = next(
        e for e in report["final_tool_plan"] if e["tool_name"] == "nearby_services"
    )
    assert set(nearby_entry["planner_arguments"].get("categories", [])) == {
        "school",
        "daycare",
        "park",
    }
    assert any("synthesized" in d.lower() for d in report["normalization_decisions"])


@pytest.mark.asyncio
async def test_diagnostic_links_price_signal_to_derived_comparison_not_price_fact() -> None:
    from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan

    listing = (
        'דירת 4 חדרים ברחוב דיזנגוף 100 בתל אביב.\n100 מ"ר, מחיר 4,700,000 ש"ח.'
    )
    question = "האם המחיר נמוך ממחיר השוק?"
    deviant_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="דיזנגוף 100, תל אביב",
            asking_price=4_700_000,
            sqm=100,
            rooms=4,
        ),
        buyer_goal="price_validation",
        claims=[
            Claim(text='מחיר 4,700,000 ש"ח', type="factual", category="price"),
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": "דיזנגוף 100, תל אביב"}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )
    report = await _run_with_plan(listing, question, deviant_plan, "evidence")
    price_entry = next(e for e in report["final_tool_plan"] if e["tool_name"] == "price_signal")
    supported_claim_ids = price_entry["supports_claim_ids"]
    assert supported_claim_ids
    linked_claim = next(
        c for c in report["identified_claims"] if c["claim_id"] == supported_claim_ids[0]
    )
    assert linked_claim["category"] == "price_comparison"
    assert linked_claim["category"] != "price" or "price" not in supported_claim_ids


@pytest.mark.asyncio
async def test_diagnostic_normalizes_query_argument_to_park_only() -> None:
    from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan

    listing = "דירת 3 חדרים ברחוב אבן גבירול 80 בתל אביב."
    question = "האם יש פארק קרוב לדירה?"
    deviant_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="אבן גבירול 80, תל אביב",
            rooms=3,
        ),
        buyer_goal="nearby_services_check",
        claims=[
            Claim(
                text="האם יש פארק קרוב לדירה?",
                type="factual",
                category="nearby_amenities",
            ),
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": "אבן גבירול 80, תל אביב"}),
            # The planner used a free-text `query` instead of `categories`.
            ToolCall(tool="nearby_services", arguments={"query": "פארק"}),
        ],
    )
    report = await _run_with_plan(listing, question, deviant_plan, "evidence")
    nearby_entry = next(
        e for e in report["final_tool_plan"] if e["tool_name"] == "nearby_services"
    )
    prepared = next(
        e for e in report["prepared_tool_inputs"] if e["call_id"] == nearby_entry["call_id"]
    )
    assert prepared["value"]["categories"] == ["park"]
    assert any("park" in d.lower() for d in report["normalization_decisions"])
