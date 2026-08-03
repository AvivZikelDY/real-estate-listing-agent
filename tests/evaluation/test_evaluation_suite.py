"""Table-driven evaluation & regression suite for the triage agent.

Runs every `EvalScenario` in `tests.evaluation.scenarios` through the real
`agent_orchestrator`, using an offline `FakeLLMClient` (configured with each
scenario's canned `TriagePlan`/report) and mock tools — never the real
LLMod.ai API. See `docs/evaluation.md` for what this suite covers and why.
"""

import pytest
from fastapi.testclient import TestClient

from src.listing_agent.agent.orchestrator import run_agent_pipeline
from src.listing_agent.agent.schemas import AgentRunResult
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.api import routes
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
from src.listing_agent.main import app
from tests.evaluation.scenarios import SCENARIOS, EvalScenario


async def _run_scenario(scenario: EvalScenario) -> tuple[AgentRunResult, StepTracer]:
    client = FakeLLMClient()
    client.set_response(TRIAGE_PLANNER_MODULE, scenario.plan)
    client.set_response(REPORT_WRITER_MODULE, ReportOutput(report=scenario.report))
    tracer = StepTracer()
    tool_executor = scenario.executor_factory()

    result = await run_agent_pipeline(
        scenario.input_prompt, llm_client=client, tool_executor=tool_executor, tracer=tracer
    )
    return result, tracer


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_buyer_goal(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    assert result.bundle.plan.buyer_goal == scenario.expected_goal


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_expected_tools_were_attempted(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    for tool in scenario.expected_tools:
        assert tool in result.bundle.executed_tools, (
            f"{scenario.name}: expected '{tool}' to run, executed={result.bundle.executed_tools}"
        )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_forbidden_tools_never_ran(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    for tool in scenario.forbidden_tools:
        assert (
            tool not in result.bundle.executed_tools
        ), f"{scenario.name}: forbidden tool '{tool}' was attempted"
        assert tool not in {
            e.tool for e in result.bundle.tool_results
        }, f"{scenario.name}: forbidden tool '{tool}' has evidence in the bundle"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_missing_information(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    for item in scenario.expected_missing_information:
        assert item in result.bundle.missing_information


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_report_fragments(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    for fragment in scenario.expected_report_fragments:
        assert fragment in result.report, f"{scenario.name}: missing fragment '{fragment}'"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_semantic_assertion(scenario: EvalScenario) -> None:
    result, _ = await _run_scenario(scenario)
    if scenario.semantic_assertion is not None:
        assert scenario.semantic_assertion(result)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_at_most_two_normal_llm_calls_in_correct_order(
    scenario: EvalScenario,
) -> None:
    _, tracer = await _run_scenario(scenario)
    llm_steps = [step for step in tracer.get_steps() if step.type == "llm"]
    assert len(llm_steps) <= 2
    assert [step.step for step in llm_steps] == ["triage_planner", "report_writer"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_every_tool_result_appears_in_evidence_bundle(
    scenario: EvalScenario,
) -> None:
    result, _ = await _run_scenario(scenario)
    bundle_tool_names = {e.tool for e in result.bundle.tool_results}
    for tool in scenario.expected_tools:
        assert tool in bundle_tool_names, f"{scenario.name}: '{tool}' missing from tool_results"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_failed_or_skipped_tools_create_limitations(
    scenario: EvalScenario,
) -> None:
    result, _ = await _run_scenario(scenario)
    for evidence in result.bundle.tool_results:
        if evidence.status in ("error", "skipped"):
            assert any(
                evidence.tool in item for item in result.bundle.limitations
            ), f"{scenario.name}: no limitation recorded for failed/skipped '{evidence.tool}'"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario_failed_evidence_never_marked_ok(scenario: EvalScenario) -> None:
    """A tool result that failed or was skipped must never carry output data.

    This is the structural guarantee behind "the final report never labels
    failed evidence as supported": `report_writer` only ever sees each
    tool's real `status` (and no fabricated `output`), so it cannot claim a
    failed/skipped check succeeded without contradicting the Evidence
    Bundle it was given.
    """
    result, _ = await _run_scenario(scenario)
    for evidence in result.bundle.tool_results:
        if evidence.status != "ok":
            assert evidence.output is None, (
                f"{scenario.name}: '{evidence.tool}' has status={evidence.status} but still "
                "carries output data"
            )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario_execute_api_response_has_four_required_keys(scenario: EvalScenario) -> None:
    fake_client = FakeLLMClient()
    fake_client.set_response(TRIAGE_PLANNER_MODULE, scenario.plan)
    fake_client.set_response(REPORT_WRITER_MODULE, ReportOutput(report=scenario.report))

    app.dependency_overrides[routes._get_llm_client_dependency] = lambda: fake_client
    app.dependency_overrides[routes._get_tool_executor_dependency] = scenario.executor_factory
    try:
        client = TestClient(app)
        response = client.post("/api/execute", json={"prompt": scenario.input_prompt})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"status", "error", "response", "steps"}
    assert body["status"] == "ok"
