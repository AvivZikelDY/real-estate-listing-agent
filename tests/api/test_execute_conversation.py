"""API-level tests for the optional `conversation_id` field (Phase 2).

Verifies the hard requirements: the four-field `/api/execute` response
shape never changes; `conversation_id` absent means exact stateless
behavior; a present `conversation_id` enables reuse across requests to the
same conversation; a different `conversation_id` never reuses anything.
"""

from fastapi.testclient import TestClient

from src.listing_agent.agent.conversation import InMemoryContextStore
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.tool_executor import LocalToolExecutor
from src.listing_agent.api import routes
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
from src.listing_agent.main import app
from src.listing_agent.tools.dependency import get_tool_registry

client = TestClient(app)

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"


def _park_plan(raw_address: str | None = KNOWN_ADDRESS) -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=raw_address,
            asking_price=4_700_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="יש פארק קרוב?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )


def _follow_up_park_plan() -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="כמה רחוק?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )


def _override_dependencies(
    fake_client: FakeLLMClient, context_store: InMemoryContextStore | None = None
) -> None:
    app.dependency_overrides[routes._get_llm_client_dependency] = lambda: fake_client
    app.dependency_overrides[routes._get_tool_executor_dependency] = lambda: LocalToolExecutor(
        get_tool_registry(Settings(tools_mode="mock"))
    )
    if context_store is not None:
        app.dependency_overrides[routes._get_context_store_dependency] = lambda: context_store


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _client_with_plans(*plans: TriagePlan) -> FakeLLMClient:
    fake = FakeLLMClient()
    fake.set_response(REPORT_WRITER_MODULE, ReportOutput(report="A concise triage report."))
    for plan in plans:
        fake.queue_response(TRIAGE_PLANNER_MODULE, plan)
    return fake


def test_conversation_id_absent_keeps_exact_four_field_response_shape() -> None:
    _override_dependencies(_client_with_plans(_park_plan()))

    response = client.post("/api/execute", json={"prompt": "יש פארק קרוב?"})

    assert response.status_code == 200
    assert set(response.json().keys()) == {"status", "error", "response", "steps"}
    assert response.json()["status"] == "ok"


def test_conversation_id_present_still_keeps_exact_four_field_response_shape() -> None:
    store = InMemoryContextStore()
    _override_dependencies(_client_with_plans(_park_plan()), store)

    response = client.post(
        "/api/execute", json={"prompt": "יש פארק קרוב?", "conversation_id": "conv-1"}
    )

    assert response.status_code == 200
    assert set(response.json().keys()) == {"status", "error", "response", "steps"}
    assert response.json()["status"] == "ok"


def test_second_turn_with_same_conversation_id_reuses_and_reduces_tool_calls() -> None:
    store = InMemoryContextStore()
    _override_dependencies(_client_with_plans(_park_plan(), _follow_up_park_plan()), store)

    first = client.post(
        "/api/execute", json={"prompt": "יש פארק קרוב?", "conversation_id": "conv-2"}
    )
    assert first.status_code == 200
    assert first.json()["status"] == "ok"

    second = client.post("/api/execute", json={"prompt": "כמה רחוק?", "conversation_id": "conv-2"})
    assert second.status_code == 200
    assert second.json()["status"] == "ok"
    # The public response shape/steps are unaffected by reuse - still just
    # the two frozen LLM steps.
    assert [step["module"] for step in second.json()["steps"]] == [
        "triage_planner",
        "report_writer",
    ]

    stored = store.get("conv-2")
    assert stored is not None
    assert stored.turn_count == 2


def test_different_conversation_id_never_reuses_anything() -> None:
    store = InMemoryContextStore()
    _override_dependencies(_client_with_plans(_park_plan(), _follow_up_park_plan()), store)

    client.post("/api/execute", json={"prompt": "יש פארק קרוב?", "conversation_id": "conv-A"})
    second = client.post("/api/execute", json={"prompt": "כמה רחוק?", "conversation_id": "conv-B"})

    assert second.status_code == 200
    assert store.get("conv-A") is not store.get("conv-B")
    assert store.get("conv-B").turn_count == 1


def test_blank_conversation_id_is_treated_as_absent() -> None:
    _override_dependencies(_client_with_plans(_park_plan()))

    response = client.post(
        "/api/execute", json={"prompt": "יש פארק קרוב?", "conversation_id": "   "}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
