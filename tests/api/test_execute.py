import asyncio

from fastapi.testclient import TestClient

from src.listing_agent.agent.schemas import PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.tool_executor import LocalToolExecutor
from src.listing_agent.api import routes
from src.listing_agent.config import Settings, get_settings
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
from src.listing_agent.main import app
from src.listing_agent.tools.dependency import get_tool_registry

client = TestClient(app)

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"
# See the note in tests/agent/test_orchestrator.py: imaginary street,
# real city, so the address reaches geo_resolver and fails there.
UNKNOWN_ADDRESS = "1 Nowhere St, Tel Aviv"


def _override_dependencies(plan: TriagePlan, report: str = "A concise triage report.") -> None:
    fake_client = FakeLLMClient()
    fake_client.set_response(TRIAGE_PLANNER_MODULE, plan)
    fake_client.set_response(REPORT_WRITER_MODULE, ReportOutput(report=report))

    app.dependency_overrides[routes._get_llm_client_dependency] = lambda: fake_client
    app.dependency_overrides[routes._get_tool_executor_dependency] = lambda: LocalToolExecutor(
        get_tool_registry(Settings(tools_mode="mock"))
    )


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _family_plan(raw_address: str = KNOWN_ADDRESS) -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=raw_address,
            asking_price=3_000_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )


def test_execute_response_has_exactly_four_top_level_fields() -> None:
    _override_dependencies(_family_plan())

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    assert response.status_code == 200
    assert set(response.json().keys()) == {"status", "error", "response", "steps"}


def test_execute_normal_run_has_exactly_two_ordered_steps() -> None:
    _override_dependencies(_family_plan())

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    body = response.json()
    assert body["status"] == "ok"
    assert len(body["steps"]) == 2


def test_execute_steps_use_the_exact_frozen_module_names() -> None:
    _override_dependencies(_family_plan())

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    steps = response.json()["steps"]
    assert [step["module"] for step in steps] == ["triage_planner", "report_writer"]


def test_execute_public_steps_have_exactly_module_prompt_response() -> None:
    _override_dependencies(_family_plan())

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    steps = response.json()["steps"]
    assert len(steps) > 0
    for step in steps:
        assert set(step.keys()) == {"module", "prompt", "response"}
        assert isinstance(step["prompt"], dict)
        assert isinstance(step["response"], dict)


def test_execute_final_report_matches_the_report_writer_output_verbatim() -> None:
    _override_dependencies(
        _family_plan(), report="Grounded report using only evidence bundle facts."
    )

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    body = response.json()
    assert body["response"] == "Grounded report using only evidence bundle facts."
    # The report_writer step's recorded prompt shows exactly what evidence it
    # was grounded in - nothing beyond the Evidence Bundle was available to it.
    report_step = next(step for step in body["steps"] if step["module"] == "report_writer")
    payload = report_step["prompt"]["user_payload"]
    assert set(payload) == {
        "prompt",
        "property_details",
        "buyer_goal",
        "claims",
        "executed_tools",
        "tool_results",
        "missing_information",
        "limitations",
    }


def test_execute_tool_failure_is_surfaced_as_a_limitation() -> None:
    _override_dependencies(_family_plan(raw_address=UNKNOWN_ADDRESS))

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    body = response.json()
    assert body["status"] == "ok"
    report_step = next(step for step in body["steps"] if step["module"] == "report_writer")
    limitations = report_step["prompt"]["user_payload"]["limitations"]
    assert any("nearby_services skipped" in item for item in limitations)


def test_execute_rejects_empty_prompt_with_status_error() -> None:
    response = client.post("/api/execute", json={"prompt": ""})

    assert response.status_code == 422
    assert response.json()["status"] == "error"


def test_execute_rejects_blank_prompt_with_status_error() -> None:
    response = client.post("/api/execute", json={"prompt": "   "})

    assert response.status_code == 422
    assert response.json()["status"] == "error"


def test_execute_rejects_missing_prompt_with_status_error() -> None:
    response = client.post("/api/execute", json={})

    assert response.status_code == 422
    assert response.json()["status"] == "error"


def test_execute_rental_request_never_runs_price_signal() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="rental",
            raw_address=KNOWN_ADDRESS,
            asking_price=6_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="price_validation",
        tool_calls=[ToolCall(tool="price_signal", arguments={})],
    )
    _override_dependencies(plan)

    response = client.post("/api/execute", json={"prompt": "Is the rent fair?"})

    body = response.json()
    assert body["status"] == "ok"
    report_step = next(step for step in body["steps"] if step["module"] == "report_writer")
    payload = report_step["prompt"]["user_payload"]
    assert "price_signal" not in payload["executed_tools"]
    assert all(result["tool"] != "price_signal" for result in payload["tool_results"])


def test_execute_missing_sqm_surfaces_a_clarification_signal() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=4_000_000,
            rooms=3,
            sqm=None,
        ),
        buyer_goal="price_validation",
        tool_calls=[ToolCall(tool="price_signal", arguments={})],
        missing_information=["sqm"],
    )
    _override_dependencies(plan)

    response = client.post("/api/execute", json={"prompt": "Is the asking price fair?"})

    body = response.json()
    assert body["status"] == "ok"
    report_step = next(step for step in body["steps"] if step["module"] == "report_writer")
    payload = report_step["prompt"]["user_payload"]
    assert "sqm" in payload["missing_information"]
    assert "price_signal" not in payload["executed_tools"]


def test_execute_rejects_oversized_prompt_before_calling_the_llm() -> None:
    """Phase 17: an oversized prompt is rejected deterministically, before
    `triage_planner` is ever invoked (no LLM call is queued/consumed)."""
    _override_dependencies(_family_plan())
    app.dependency_overrides[routes._get_settings_dependency] = lambda: Settings(
        max_prompt_length=10
    )

    response = client.post(
        "/api/execute", json={"prompt": "This prompt is definitely longer than ten characters."}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "error"
    assert "too long" in body["error"]
    assert body["response"] is None
    # No LLM call was ever made: no llm-typed steps are reported.
    assert body["steps"] == []


def test_execute_accepts_prompt_within_the_configured_limit() -> None:
    _override_dependencies(_family_plan())
    app.dependency_overrides[routes._get_settings_dependency] = lambda: Settings(
        max_prompt_length=1000
    )

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    assert response.json()["status"] == "ok"


class _SlowFakeLLMClient(FakeLLMClient):
    """A `FakeLLMClient` that sleeps before returning, to exercise the
    overall request deadline (Phase 17) without any real network call."""

    def __init__(self, delay_seconds: float, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._delay_seconds = delay_seconds

    async def generate_json(self, *args, **kwargs):
        await asyncio.sleep(self._delay_seconds)
        return await super().generate_json(*args, **kwargs)


def test_execute_enforces_the_overall_request_deadline() -> None:
    """Phase 17: a run that exceeds `llm_request_timeout_seconds` returns a
    clear `status="error"`, never an unbounded hang or an opaque 500."""
    plan = _family_plan()
    slow_client = _SlowFakeLLMClient(delay_seconds=0.5)
    slow_client.set_response(TRIAGE_PLANNER_MODULE, plan)
    slow_client.set_response(REPORT_WRITER_MODULE, ReportOutput(report="report"))

    app.dependency_overrides[routes._get_llm_client_dependency] = lambda: slow_client
    app.dependency_overrides[routes._get_tool_executor_dependency] = lambda: LocalToolExecutor(
        get_tool_registry(Settings(tools_mode="mock"))
    )
    app.dependency_overrides[routes._get_settings_dependency] = lambda: Settings(
        llm_request_timeout_seconds=0.05
    )

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "error"
    assert "time limit" in body["error"]
    assert body["response"] is None


def test_llm_request_timeout_seconds_is_configurable_via_env_var(
    monkeypatch,
) -> None:
    """`LLM_REQUEST_TIMEOUT_SECONDS` must reach the actual deadline enforced
    around the agent pipeline (not just be an unused `Settings` field)."""
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "0.05")
    env_settings = get_settings()
    assert env_settings.llm_request_timeout_seconds == 0.05

    plan = _family_plan()
    slow_client = _SlowFakeLLMClient(delay_seconds=0.5)
    slow_client.set_response(TRIAGE_PLANNER_MODULE, plan)
    slow_client.set_response(REPORT_WRITER_MODULE, ReportOutput(report="report"))

    app.dependency_overrides[routes._get_llm_client_dependency] = lambda: slow_client
    app.dependency_overrides[routes._get_tool_executor_dependency] = lambda: LocalToolExecutor(
        get_tool_registry(Settings(tools_mode="mock"))
    )
    app.dependency_overrides[routes._get_settings_dependency] = lambda: env_settings

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    body = response.json()
    assert body["status"] == "error"
    assert "time limit of 0 seconds" in body["error"]


def test_execute_normal_path_is_well_within_the_default_deadline() -> None:
    """The default deadline must not interfere with a normal, fast run."""
    _override_dependencies(_family_plan())

    response = client.post("/api/execute", json={"prompt": "Is this good for a family?"})

    assert response.json()["status"] == "ok"
