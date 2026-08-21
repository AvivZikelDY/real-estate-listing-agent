"""Phase 14: the multi-turn follow-up context & evidence-reuse scenarios.

Uses `run_agent_pipeline` end-to-end with a shared `InMemoryContextStore`
and the mock `LocalToolExecutor` (deterministic, offline mock tool data for
"HaYarkon 120, Tel Aviv" - see `tools/mock_implementations/`). The planner
is a `FakeLLMClient` queued with the exact plan each turn would produce
(no real/paid LLM calls) - what changes between turns is only the
conversation's stored context, exercising `agent.conversation` for real.
"""

from src.listing_agent.agent.conversation.context import ConversationContext, InMemoryContextStore
from src.listing_agent.agent.module_names import CONTEXT_REUSE
from src.listing_agent.agent.orchestrator import run_agent_pipeline
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import LocalToolExecutor
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
from src.listing_agent.tools.dependency import get_tool_registry

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"


def _executor() -> LocalToolExecutor:
    return LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))


def _client(plan: TriagePlan) -> FakeLLMClient:
    client = FakeLLMClient()
    client.set_response(TRIAGE_PLANNER_MODULE, plan)
    client.set_response(REPORT_WRITER_MODULE, ReportOutput(report="A concise triage report."))
    return client


def _context_reuse_detail(tracer: StepTracer) -> dict:
    step = next(s for s in tracer.get_steps() if s.step == CONTEXT_REUSE)
    return step.detail


async def test_scenario_1_first_nearby_question_runs_geo_and_park() -> None:
    """Q1: 'יש פארק קרוב?' -> geo EXECUTE, nearby park EXECUTE."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    store = InMemoryContextStore()
    tracer = StepTracer()

    result = await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(plan),
        tool_executor=_executor(),
        tracer=tracer,
        conversation_id="conv-scenario",
        context_store=store,
    )

    assert set(result.bundle.executed_tools) == {"geo_resolver", "nearby_services"}
    detail = _context_reuse_detail(tracer)
    assert detail["reused"] == []
    assert detail["tool_calls_saved"] == 0

    stored = store.get("conv-scenario")
    assert stored is not None
    assert stored.resolved_geo is not None
    assert any(key.startswith("nearby_services:park:") for key in stored.cached_nearby)


async def test_scenario_2_implicit_follow_up_reuses_geo_and_park() -> None:
    """Q2 (same conversation): 'כמה רחוק?' resolves to park via the
    compact follow-up context, and reuses both cached results - 0 new
    tool calls. This is the main acceptance test."""
    store = InMemoryContextStore()
    tracer1 = StepTracer()
    turn1_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(turn1_plan),
        tool_executor=_executor(),
        tracer=tracer1,
        conversation_id="conv-scenario",
        context_store=store,
    )

    # Turn 2: the planner (resolving "כמה רחוק?" via the compact follow-up
    # context showing PREVIOUS RELEVANT FOCUS: park) proposes the same
    # narrow nearby_services(["park"]) call again, without restating any
    # property facts.
    turn2_plan = TriagePlan(
        property_details=PropertyDetails(),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="כמה רחוק?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )
    tracer2 = StepTracer()
    result2 = await run_agent_pipeline(
        "כמה רחוק?",
        llm_client=_client(turn2_plan),
        tool_executor=_executor(),
        tracer=tracer2,
        conversation_id="conv-scenario",
        context_store=store,
    )

    assert result2.bundle.executed_tools == []
    detail = _context_reuse_detail(tracer2)
    assert set(detail["reused"]) == {"geo_resolver", "nearby:park"}
    assert detail["executed"] == []
    assert detail["tool_calls_saved"] == 2

    park_evidence = next(e for e in result2.bundle.tool_results if e.tool == "nearby_services")
    assert park_evidence.status == "ok"


async def test_scenario_3_new_nearby_category_only_executes_school() -> None:
    """Q3: 'ומה לגבי בתי ספר?' -> geo REUSE, nearby_services(["school"])
    EXECUTE only - park is not re-executed."""
    store = InMemoryContextStore()
    turn1_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(turn1_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-scenario",
        context_store=store,
    )

    turn3_plan = TriagePlan(
        property_details=PropertyDetails(),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="ומה לגבי בתי ספר?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
        ],
    )
    tracer3 = StepTracer()
    result3 = await run_agent_pipeline(
        "ומה לגבי בתי ספר?",
        llm_client=_client(turn3_plan),
        tool_executor=_executor(),
        tracer=tracer3,
        conversation_id="conv-scenario",
        context_store=store,
    )

    assert result3.bundle.executed_tools == ["nearby_services"]
    detail = _context_reuse_detail(tracer3)
    assert "geo_resolver" in detail["reused"]
    assert detail["executed"] == ["nearby_services"]

    nearby_evidence = next(e for e in result3.bundle.tool_results if e.tool == "nearby_services")
    assert nearby_evidence.status == "ok"
    # Only school was requested from the actual tool this turn - never park
    # again (verified against the tracer's recorded tool-call input).
    tool_trace = next(t for t in tracer3.get_tool_traces() if t.tool == "nearby_services")
    assert tool_trace.request_summary["categories"] == ["school"]


async def test_scenario_4_price_question_then_changed_price_re_executes() -> None:
    """Q4: 'האם המחיר הוגן?' -> geo REUSE, price_signal EXECUTE.
    Q5: 'ומה אם המחיר יהיה 4.4 מיליון?' -> new PriceSignalInput -> the
    previous price result is not reused, price_signal EXECUTEs again."""
    store = InMemoryContextStore()
    turn1_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(turn1_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-scenario",
        context_store=store,
    )

    turn4_plan = TriagePlan(
        property_details=PropertyDetails(),
        buyer_goal="price_validation",
        claims=[Claim(text="האם המחיר הוגן?", type="factual", category="price_comparison")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )
    tracer4 = StepTracer()
    result4 = await run_agent_pipeline(
        "האם המחיר הוגן?",
        llm_client=_client(turn4_plan),
        tool_executor=_executor(),
        tracer=tracer4,
        conversation_id="conv-scenario",
        context_store=store,
    )
    assert result4.bundle.executed_tools == ["price_signal"]
    detail4 = _context_reuse_detail(tracer4)
    assert "geo_resolver" in detail4["reused"]
    price_evidence = next(e for e in result4.bundle.tool_results if e.tool == "price_signal")
    assert price_evidence.status == "ok"
    assert price_evidence.output["outcome"] == "compared"

    turn5_plan = TriagePlan(
        property_details=PropertyDetails(asking_price=4_400_000),
        buyer_goal="price_validation",
        claims=[
            Claim(
                text="ומה אם המחיר יהיה 4.4 מיליון?", type="factual", category="price_comparison"
            )
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )
    tracer5 = StepTracer()
    result5 = await run_agent_pipeline(
        "ומה אם המחיר יהיה 4.4 מיליון?",
        llm_client=_client(turn5_plan),
        tool_executor=_executor(),
        tracer=tracer5,
        conversation_id="conv-scenario",
        context_store=store,
    )

    assert result5.bundle.executed_tools == ["price_signal"]
    detail5 = _context_reuse_detail(tracer5)
    assert "geo_resolver" in detail5["reused"]
    assert "price_signal" not in detail5["reused"]
    price_evidence_5 = next(e for e in result5.bundle.tool_results if e.tool == "price_signal")
    assert price_evidence_5.status == "ok"


async def test_scenario_5_unsupported_category_triggers_no_tools() -> None:
    """'ומה לגבי הממ"ד?' (safe room - unsupported category) -> 0 external
    tools, and never a broad fan-out over every supported category.

    The planner proposes no tool call at all here (mirrors the real
    Hapalmach 21 regression: `plan_validator`'s own claim-driven synthesis
    must not add `nearby_services` when the linked claim resolves to zero
    supported categories - see
    `tests/agent/test_hapsalmach_nearby_category_regression.py`)."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=4_700_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="fast_claim_triage",
        claims=[Claim(text='ומה לגבי הממ"ד?', type="factual", category="nearby_amenities")],
        tool_calls=[],
    )
    store = InMemoryContextStore()
    tracer = StepTracer()

    result = await run_agent_pipeline(
        'ומה לגבי הממ"ד?',
        llm_client=_client(plan),
        tool_executor=_executor(),
        tracer=tracer,
        conversation_id="conv-unsupported",
        context_store=store,
    )

    assert result.bundle.executed_tools == []
    assert all(e.tool != "nearby_services" for e in result.bundle.tool_results)


async def test_scenario_6_different_conversation_id_never_reuses() -> None:
    store = InMemoryContextStore()
    turn1_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(turn1_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-A",
        context_store=store,
    )

    # A second, unrelated conversation asking the same follow-up must not
    # see any reuse at all - it has no prior turn.
    turn2_plan = TriagePlan(
        property_details=PropertyDetails(raw_address=KNOWN_ADDRESS),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="כמה רחוק?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )
    tracer2 = StepTracer()
    result2 = await run_agent_pipeline(
        "כמה רחוק?",
        llm_client=_client(turn2_plan),
        tool_executor=_executor(),
        tracer=tracer2,
        conversation_id="conv-B",
        context_store=store,
    )

    assert set(result2.bundle.executed_tools) == {"geo_resolver", "nearby_services"}
    detail = _context_reuse_detail(tracer2)
    assert detail["reused"] == []


async def test_scenario_6b_different_listing_never_reuses_evidence() -> None:
    """A clearly different address within the SAME conversation must never
    mix in the previous listing's cached evidence."""
    store = InMemoryContextStore()
    turn1_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(turn1_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-listing-change",
        context_store=store,
    )

    turn2_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="Derech Hashalom 1, Tel Aviv",
            asking_price=2_000_000,
            rooms=3,
            sqm=70,
        ),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="יש פארק קרוב?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )
    tracer2 = StepTracer()
    await run_agent_pipeline(
        "מה לגבי הכתובת דרך השלום 1?",
        llm_client=_client(turn2_plan),
        tool_executor=_executor(),
        tracer=tracer2,
        conversation_id="conv-listing-change",
        context_store=store,
    )

    detail = _context_reuse_detail(tracer2)
    assert detail["reused"] == []
    assert detail["listing_changed"] is True


async def test_scenario_7_technical_failure_is_not_cached_but_empty_result_is() -> None:
    """A nearby technical timeout/error is never cached; a successful,
    empty nearby result IS cached/reusable."""
    context = ConversationContext(conversation_id="conv-fail")
    store = InMemoryContextStore()
    store.save(context)

    # Simulate a prior geo resolution succeeding but nearby technically
    # failing by directly inspecting the cache module's storage contract:
    # only "ok" evidence is ever written to `cached_nearby`/`cached_geo`.
    from src.listing_agent.agent.conversation.cache import _store_new_results
    from src.listing_agent.agent.schemas import ToolEvidence

    plan = TriagePlan(
        property_details=PropertyDetails(raw_address=KNOWN_ADDRESS, asking_price=1, sqm=1),
        buyer_goal="nearby_services_check",
        tool_calls=[ToolCall(tool="nearby_services", arguments={"categories": ["park"]})],
    )
    nearby_call = ToolCall(tool="nearby_services", arguments={"categories": ["park"]})

    from src.listing_agent.tools.schemas import GeoResolverOutput

    geo_seed = GeoResolverOutput(
        outcome="resolved",
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        lat=32.0853,
        lon=34.7738,
        resolution_quality="exact",
        source="mock_dataset",
    )

    failed_evidence = ToolEvidence(tool="nearby_services", status="error", error="boom")
    geo_seed_out = _store_new_results(
        context,
        plan,
        geo_call=None,
        executed_nearby_call=nearby_call,
        price_call=None,
        executed_by_tool={"nearby_services": failed_evidence},
        geo_seed=geo_seed,
    )
    assert geo_seed_out is geo_seed
    assert context.cached_nearby == {}

    empty_evidence = ToolEvidence(
        tool="nearby_services",
        status="ok",
        output={"places": [], "categories": [], "evidence_quality": "low", "limitations": []},
    )
    _store_new_results(
        context,
        plan,
        geo_call=None,
        executed_nearby_call=nearby_call,
        price_call=None,
        executed_by_tool={"nearby_services": empty_evidence},
        geo_seed=geo_seed,
    )
    assert len(context.cached_nearby) == 1


async def test_scenario_8_no_recognised_city_skips_geo_gracefully_and_leaves_cache_untouched() -> (
    None
):
    """Phase 2 (D.1): an address naming no city this agent recognises
    (Nir's no-city refusal, `tool_input_builder.MissingDependencyError`)
    must never crash a stateful turn. The run still completes with
    `geo_resolver` as an honest `status="skipped"` evidence entry (never
    an uncaught exception, never `status="error"`), and nothing is
    written to the conversation's geo/nearby caches for this turn."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="גונן 14",  # names no recognised city
            asking_price=4_350_000,
            rooms=5,
            sqm=123,
        ),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="יש פארק קרוב?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )
    store = InMemoryContextStore()
    tracer = StepTracer()

    result = await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(plan),
        tool_executor=_executor(),
        tracer=tracer,
        conversation_id="conv-no-city",
        context_store=store,
    )

    assert result.bundle.executed_tools == []
    geo_evidence = next(t for t in result.bundle.tool_results if t.tool == "geo_resolver")
    assert geo_evidence.status == "skipped"
    assert geo_evidence.reason is not None

    stored = store.get("conv-no-city")
    assert stored is not None
    assert stored.resolved_geo is None
    assert stored.cached_geo == {}
    assert stored.cached_nearby == {}


async def test_scenario_9_valid_address_turn_after_a_no_city_turn_still_caches_normally() -> None:
    """A later turn in the same conversation with a resolvable address
    must still resolve and cache geo/nearby normally - the no-city turn
    leaves no residue that could block a subsequent successful turn."""
    store = InMemoryContextStore()

    no_city_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address="גונן 14",
            asking_price=4_350_000,
            rooms=5,
            sqm=123,
        ),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="יש פארק קרוב?", type="factual", category="nearby_amenities")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )
    first = await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(no_city_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-recovers",
        context_store=store,
    )
    assert first.bundle.executed_tools == []

    valid_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    second = await run_agent_pipeline(
        "יש פארק קרוב?",
        llm_client=_client(valid_plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        conversation_id="conv-recovers",
        context_store=store,
    )

    assert set(second.bundle.executed_tools) == {"geo_resolver", "nearby_services"}
    stored = store.get("conv-recovers")
    assert stored is not None
    assert stored.resolved_geo is not None
    assert any(key.startswith("nearby_services:park:") for key in stored.cached_nearby)
