"""Unit tests for the `/api/execute` step serializer (A3).

These test the serializer directly against `StepTracer`, independent of
the HTTP layer, so they can exercise cases (a failed LLM call, a repair
step, tool/deterministic steps) without needing a real LLM provider.
"""

from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.step_tracer.tool_trace import ToolTraceRecord
from src.listing_agent.api.execute_step_serializer import build_public_steps
from src.listing_agent.api.schemas import StepTrace


def test_only_llm_steps_are_surfaced_publicly() -> None:
    tracer = StepTracer()
    tracer.record_deterministic_step("plan_validator", status="ok", detail={"note": "fine"})
    tracer.record_tool_trace(
        ToolTraceRecord(
            tool="geo_resolver",
            status="ok",
            arguments={"address": "x"},
            observations={"outcome": "resolved"},
            duration_ms=1.0,
        )
    )
    tracer._traces.append(
        StepTrace(
            step="triage_planner",
            type="llm",
            status="ok",
            detail={
                "prompt": {"system_prompt": "sp", "user_payload": {"prompt": "hi"}},
                "response": {"buyer_goal": "family_suitability"},
            },
        )
    )

    public_steps = build_public_steps(tracer.get_steps())

    assert [step.module for step in public_steps] == ["triage_planner"]
    for step in public_steps:
        assert set(step.model_dump().keys()) == {"module", "prompt", "response"}


def test_successful_llm_step_maps_detail_prompt_and_response() -> None:
    tracer = StepTracer()
    tracer._traces.append(
        StepTrace(
            step="triage_planner",
            type="llm",
            status="ok",
            detail={
                "prompt": {"system_prompt": "sp", "user_payload": {"prompt": "hi"}},
                "response": {"buyer_goal": "family_suitability"},
            },
        )
    )

    [step] = build_public_steps(tracer.get_steps())

    assert step.module == "triage_planner"
    assert step.prompt == {"system_prompt": "sp", "user_payload": {"prompt": "hi"}}
    assert step.response == {"buyer_goal": "family_suitability"}


def test_failed_llm_step_puts_the_error_inside_response() -> None:
    tracer = StepTracer()
    tracer._traces.append(
        StepTrace(
            step="triage_planner",
            type="llm",
            status="error",
            detail={
                "prompt": {"system_prompt": "sp", "user_payload": {"prompt": "hi"}},
                "error": "LLM provider timed out",
            },
        )
    )

    [step] = build_public_steps(tracer.get_steps())

    assert step.module == "triage_planner"
    assert step.response == {"error": "LLM provider timed out"}


def test_a_real_repair_step_still_appears_publicly() -> None:
    tracer = StepTracer()
    tracer.record_llm_repair_step(
        module="triage_planner",
        status="ok",
        detail={
            "system_prompt": "sp",
            "invalid_response": "{not valid json",
            "outcome": "repaired successfully",
        },
    )

    [step] = build_public_steps(tracer.get_steps())

    assert step.module == "triage_planner_repair"
    assert isinstance(step.prompt, dict)
    assert isinstance(step.response, dict)
