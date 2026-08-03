import pytest

from src.listing_agent.agent.orchestrator import run_agent_pipeline
from src.listing_agent.agent.request_limits import RequestLimitError
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
UNKNOWN_ADDRESS = "1 Nowhere St, Atlantis"


def _client_with_plan(plan: TriagePlan, report: str = "A concise triage report.") -> FakeLLMClient:
    client = FakeLLMClient()
    client.set_response(TRIAGE_PLANNER_MODULE, plan)
    client.set_response(REPORT_WRITER_MODULE, ReportOutput(report=report))
    return client


def _executor():
    return LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))


async def test_family_request_runs_geo_and_nearby_but_not_price_signal() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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

    result = await run_agent_pipeline(
        "Is this apartment good for a family with kids?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert set(result.bundle.executed_tools) == {"geo_resolver", "nearby_services"}
    assert "price_signal" not in result.bundle.executed_tools
    assert result.report


async def test_price_request_with_all_data_runs_geo_and_price_signal() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=4_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="price_validation",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    result = await run_agent_pipeline(
        "Is the asking price fair?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert set(result.bundle.executed_tools) == {"geo_resolver", "price_signal"}
    price_evidence = next(e for e in result.bundle.tool_results if e.tool == "price_signal")
    assert price_evidence.status == "ok"


async def test_missing_sqm_prevents_price_signal() -> None:
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
    )

    result = await run_agent_pipeline(
        "Is the asking price fair?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert "price_signal" not in result.bundle.executed_tools
    assert all(e.tool != "price_signal" for e in result.bundle.tool_results)


async def test_rental_listing_prevents_price_signal() -> None:
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

    result = await run_agent_pipeline(
        "Is the rent fair?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert "price_signal" not in result.bundle.executed_tools
    assert all(e.tool != "price_signal" for e in result.bundle.tool_results)


async def test_tool_failure_still_produces_a_bundle_with_a_limitation() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=UNKNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    result = await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert result.bundle.request_id
    geo_evidence = next(e for e in result.bundle.tool_results if e.tool == "geo_resolver")
    # geo_resolver itself succeeded ("ok") with a not_found business outcome;
    # nearby_services is the one that could not run without coordinates.
    assert geo_evidence.status == "ok"
    assert geo_evidence.output["outcome"] == "not_found"
    nearby_evidence = next(e for e in result.bundle.tool_results if e.tool == "nearby_services")
    assert nearby_evidence.status == "skipped"
    assert any("no known coordinates" in item for item in result.bundle.limitations)
    assert any("nearby_services skipped" in item for item in result.bundle.limitations)
    assert result.report


async def test_duplicate_planner_calls_run_the_tool_only_once() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    result = await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert result.bundle.executed_tools.count("geo_resolver") == 1


async def test_step_tracer_records_triage_planner_then_report_writer_as_llm_steps() -> None:
    plan = TriagePlan(
        property_details=PropertyDetails(listing_type="purchase"),
        buyer_goal="general_question",
        tool_calls=[],
    )
    tracer = StepTracer()

    await run_agent_pipeline(
        "What can you tell me about this listing?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=tracer,
    )

    llm_steps = [step for step in tracer.get_steps() if step.type == "llm"]
    assert [step.step for step in llm_steps] == ["triage_planner", "report_writer"]


async def test_report_writer_receives_only_normalized_evidence_bundle() -> None:
    """End-to-end: the `report_writer` step's recorded `user_payload` must
    contain only the compact Evidence Bundle projection - no raw tool
    `output`, no executor bookkeeping (`attempts`) - and evidence for a
    price-related claim must carry a `claim_id` linking back to it."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_600_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="price_validation",
        claims=[Claim(text="below market price", type="factual", category="price")],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )
    tracer = StepTracer()

    await run_agent_pipeline(
        "Is this below market price?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=tracer,
    )

    report_writer_step = next(
        step for step in tracer.get_steps() if step.step == REPORT_WRITER_MODULE
    )
    tool_results = report_writer_step.detail["prompt"]["user_payload"]["tool_results"]
    assert tool_results  # geo_resolver + price_signal both ran
    for tool_result in tool_results:
        assert "output" not in tool_result
        assert "attempts" not in tool_result

    price_result = next(item for item in tool_results if item["tool"] == "price_signal")
    # Phase 19: `price_signal` evidence must link to the comparative buyer
    # question/claim, never merely the literal "price" listing fact - even
    # when the planner (as here) mis-categorized "below market price" as
    # plain `"price"` instead of `"price_comparison"`, `plan_validator`
    # deterministically derives and links to a comparative claim instead.
    assert price_result["claim_id"] != "claim-0"
    assert price_result["claim_id"] == "claim-derived-price-comparison"
    assert price_result["observations"] is not None


async def test_shared_evidence_links_to_multiple_relevant_claims() -> None:
    """Phase 17: a single deduplicated `nearby_services` result may support
    more than one claim - evidence is not forced onto a single, arbitrary
    "first match" claim id."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="family_suitability",
        claims=[
            Claim(text="near a good school", type="factual", category="school"),
            Claim(text="quiet, family-friendly area", type="factual", category="family"),
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    result = await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    nearby_evidence = next(e for e in result.bundle.tool_results if e.tool == "nearby_services")
    assert set(nearby_evidence.claim_ids) == {"claim-0", "claim-1"}
    # Backward-compatible single `claim_id` still points at one of them.
    assert nearby_evidence.claim_id in nearby_evidence.claim_ids


async def test_duplicate_tool_calls_with_equivalent_arguments_execute_once() -> None:
    """Phase 17: two planner-proposed `nearby_services` calls (even with
    different raw `arguments`) still execute the tool only once, because
    `evidence_collector` always derives the actual input deterministically
    from resolved coordinates rather than from planner-supplied arguments."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ],
    )

    result = await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
    )

    assert result.bundle.executed_tools.count("nearby_services") == 1


async def test_normal_path_makes_at_most_two_llm_calls() -> None:
    """Phase 17: the normal path must use exactly the two frozen LLM
    modules once each - never more."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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
    tracer = StepTracer()

    await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=tracer,
    )

    llm_steps = [step for step in tracer.get_steps() if step.type == "llm"]
    assert len(llm_steps) <= 2
    assert [step.step for step in llm_steps] == [TRIAGE_PLANNER_MODULE, REPORT_WRITER_MODULE]


async def test_oversized_prompt_never_reaches_triage_planner() -> None:
    """Phase 17: `check_prompt_length` runs before `triage_planner`, so an
    oversized prompt raises `RequestLimitError` without consuming any LLM
    call (the `FakeLLMClient` here has no configured response, so a
    LookupError would be raised instead if the check were skipped)."""
    empty_client = FakeLLMClient()  # no responses configured at all

    with pytest.raises(RequestLimitError):
        await run_agent_pipeline(
            "x" * 50,
            llm_client=empty_client,
            tool_executor=_executor(),
            tracer=StepTracer(),
            settings=Settings(max_prompt_length=10),
        )


async def test_too_many_planned_claims_are_capped_before_report_writer() -> None:
    """Phase 17: `Settings.max_claims` is honored end-to-end through the
    orchestrator."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="general_question",
        claims=[Claim(text=f"claim {i}", type="factual") for i in range(10)],
        tool_calls=[],
    )

    result = await run_agent_pipeline(
        "Tell me about this listing.",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        settings=Settings(max_claims=3),
    )

    assert len(result.bundle.plan.claims) == 3
    assert "additional_claims_truncated" in result.bundle.missing_information


async def test_too_many_planned_tool_calls_are_capped() -> None:
    """Phase 17: `Settings.max_tool_calls` is honored end-to-end through
    the orchestrator."""
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
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

    result = await run_agent_pipeline(
        "Is this good for a family?",
        llm_client=_client_with_plan(plan),
        tool_executor=_executor(),
        tracer=StepTracer(),
        settings=Settings(max_tool_calls=1),
    )

    assert len(result.bundle.plan.tool_calls) == 1
    assert result.bundle.plan.tool_calls[0].tool == "geo_resolver"
