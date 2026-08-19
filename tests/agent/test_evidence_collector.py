from src.listing_agent.agent.evidence_collector import collect_evidence
from src.listing_agent.agent.schemas import PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import LocalToolExecutor
from src.listing_agent.config import Settings
from src.listing_agent.tools.dependency import get_tool_registry


def _mock_executor():
    return LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))


def _plan(tool_calls, **property_overrides) -> TriagePlan:
    property_details = PropertyDetails(
        listing_type="purchase",
        raw_address="HaYarkon 120, Tel Aviv",
        asking_price=3_000_000,
        rooms=3,
        sqm=80,
    ).model_copy(update=property_overrides)
    return TriagePlan(
        property_details=property_details, buyer_goal="general_question", tool_calls=tool_calls
    )


async def test_geo_resolver_runs_and_feeds_nearby_services() -> None:
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ]
    )
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(plan, _mock_executor(), tracer)

    assert executed == ["geo_resolver", "nearby_services"]
    assert all(evidence.status == "ok" for evidence in tool_results)


async def test_dependent_tool_skipped_without_prior_geo_resolver() -> None:
    plan = _plan([ToolCall(tool="nearby_services", arguments={})])
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(plan, _mock_executor(), tracer)

    assert executed == []
    assert tool_results[0].tool == "nearby_services"
    assert tool_results[0].status == "skipped"
    assert "geo_resolver" in tool_results[0].reason


async def test_tool_not_in_plan_is_never_executed() -> None:
    plan = _plan([ToolCall(tool="geo_resolver", arguments={})])
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(plan, _mock_executor(), tracer)

    assert executed == ["geo_resolver"]
    assert "price_signal" not in executed
    assert "nearby_services" not in executed


async def test_unresolvable_geo_resolver_does_not_abort_remaining_calls() -> None:
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
        # A real city with an imaginary street: the address genuinely
            # does not resolve, rather than being refused up front for
            # naming no city (`_NO_CITY_HINT_REASON`).
            raw_address="1 Nowhere St, Tel Aviv",
    )
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(plan, _mock_executor(), tracer)

    assert executed == ["geo_resolver"]
    geo_evidence, nearby_evidence = tool_results
    # geo_resolver still succeeded ("ok"): it produced a well-formed
    # not_found business outcome, not a technical failure.
    assert geo_evidence.status == "ok"
    assert geo_evidence.output["outcome"] == "not_found"
    # nearby_services could not run because no coordinates were resolved.
    assert nearby_evidence.status == "skipped"
    assert "geo_resolver" in nearby_evidence.reason


async def test_tracer_records_one_step_per_planned_call() -> None:
    plan = _plan([ToolCall(tool="geo_resolver", arguments={})])
    tracer = StepTracer()

    await collect_evidence(plan, _mock_executor(), tracer)

    assert len(tracer.get_steps()) == 1
    assert tracer.get_steps()[0].step == "geo_resolver"


async def test_tool_trace_records_real_duration_and_request_summary() -> None:
    plan = _plan([ToolCall(tool="geo_resolver", arguments={})])
    tracer = StepTracer()

    await collect_evidence(plan, _mock_executor(), tracer)

    tool_traces = tracer.get_tool_traces()
    assert len(tool_traces) == 1
    trace = tool_traces[0]
    assert trace.tool == "geo_resolver"
    assert trace.status == "ok"
    # A real call happened, so elapsed time must be measurable (not a
    # reconstructed/fabricated placeholder).
    assert trace.duration_ms >= 0.0
    assert trace.request_summary == {"address": "HaYarkon 120, Tel Aviv", "city_hint": "Tel Aviv"}


async def test_tool_trace_for_skipped_dependency_has_zero_duration_and_no_secrets() -> None:
    plan = _plan([ToolCall(tool="nearby_services", arguments={})])
    tracer = StepTracer()

    await collect_evidence(plan, _mock_executor(), tracer)

    tool_traces = tracer.get_tool_traces()
    assert len(tool_traces) == 1
    trace = tool_traces[0]
    assert trace.tool == "nearby_services"
    assert trace.status == "skipped"
    assert trace.duration_ms == 0.0
    assert trace.request_summary is None


async def test_tool_trace_ordering_matches_step_ordering() -> None:
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ]
    )
    tracer = StepTracer()

    await collect_evidence(plan, _mock_executor(), tracer)

    assert [trace.tool for trace in tracer.get_tool_traces()] == [
        "geo_resolver",
        "nearby_services",
    ]
    assert [step.step for step in tracer.get_steps()] == ["geo_resolver", "nearby_services"]


async def test_total_tool_execution_time_budget_skips_remaining_calls() -> None:
    """Phase 17: once the cumulative tool-execution time budget is spent,
    any remaining planned calls are skipped (never attempted) instead of
    running unbounded."""
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ]
    )
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(
        plan, _mock_executor(), tracer, max_total_tool_execution_seconds=0.0
    )

    # The very first call already exceeds a zero-second budget check
    # *before* it's attempted only if the budget was already spent; a
    # budget of 0.0 means no call is ever allowed to run.
    assert executed == []
    assert all(evidence.status == "skipped" for evidence in tool_results)
    assert all("budget" in evidence.reason for evidence in tool_results)


async def test_generous_time_budget_does_not_affect_normal_execution() -> None:
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ]
    )
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(
        plan, _mock_executor(), tracer, max_total_tool_execution_seconds=20.0
    )

    assert executed == ["geo_resolver", "nearby_services"]
    assert all(evidence.status == "ok" for evidence in tool_results)


async def test_an_address_with_no_city_is_reported_back_to_the_evidence_bundle() -> None:
    """No city named, no location work - and the bundle says exactly that.

    The tool is never executed, so this is `status="skipped"` rather than an
    error or a `not_found`: nothing was checked and nothing was found, and
    conflating the three would let the report imply the address was looked
    up and rejected. Everything downstream of coordinates skips with it.
    """
    plan = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
        raw_address="בשמואל תמיר 2",
    )
    tracer = StepTracer()

    tool_results, executed = await collect_evidence(plan, _mock_executor(), tracer)

    assert executed == [], "no tool may run without a city to search in"
    geo_evidence, nearby_evidence = tool_results
    assert geo_evidence.tool == "geo_resolver"
    assert geo_evidence.status == "skipped"
    assert geo_evidence.output is None
    assert "city" in geo_evidence.reason
    assert "בשמואל תמיר 2" in geo_evidence.reason
    assert nearby_evidence.status == "skipped"


async def test_the_no_city_skip_is_recorded_in_the_trace() -> None:
    """A skipped tool must still appear as a step, never vanish silently."""
    plan = _plan([ToolCall(tool="geo_resolver", arguments={})], raw_address="בשמואל תמיר 2")
    tracer = StepTracer()

    await collect_evidence(plan, _mock_executor(), tracer)

    steps = tracer.get_steps()
    assert len(steps) == 1
    assert steps[0].step == "geo_resolver"
