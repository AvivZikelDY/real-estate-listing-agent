import pytest
from pydantic import BaseModel

from src.listing_agent.agent.step_tracer import StepTracer, ToolTraceRecord
from src.listing_agent.llm.client.fake import FakeLLMClient


class _PlanResult(BaseModel):
    text: str


@pytest.mark.asyncio
async def test_tracer_preserves_call_order() -> None:
    client = FakeLLMClient()
    client.queue_response("triage_planner", _PlanResult(text="first"))
    client.queue_response("report_writer", _PlanResult(text="second"))

    tracer = StepTracer()
    await tracer.call(client, "triage_planner", "sys", {}, _PlanResult)
    await tracer.call(client, "report_writer", "sys", {}, _PlanResult)

    steps = tracer.get_steps()

    assert [step.step for step in steps] == ["triage_planner", "report_writer"]
    assert [step.detail["response"]["text"] for step in steps] == ["first", "second"]


@pytest.mark.asyncio
async def test_tracer_records_module_prompt_and_response() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _PlanResult(text="ok"))

    tracer = StepTracer()
    await tracer.call(
        client,
        "triage_planner",
        "system instructions",
        {"address": "1 Rothschild"},
        _PlanResult,
    )

    step = tracer.get_steps()[0]
    assert step.step == "triage_planner"
    assert step.type == "llm"
    assert step.status == "ok"
    assert step.detail["prompt"]["system_prompt"] == "system instructions"
    assert step.detail["prompt"]["user_payload"] == {"address": "1 Rothschild"}
    assert step.detail["response"] == {"text": "ok"}


@pytest.mark.asyncio
async def test_tracer_does_not_record_secrets() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _PlanResult(text="ok"))

    tracer = StepTracer()
    await tracer.call(
        client,
        "triage_planner",
        "system instructions",
        {
            "api_key": "sk-super-secret",
            "authorization": "Bearer super-secret-token",
            "password": "hunter2",
            "nested": {"llmod_api_key": "another-secret"},
            "address": "1 Rothschild",
        },
        _PlanResult,
    )

    step = tracer.get_steps()[0]
    payload = step.detail["prompt"]["user_payload"]

    assert payload["api_key"] == "***REDACTED***"
    assert payload["authorization"] == "***REDACTED***"
    assert payload["password"] == "***REDACTED***"
    assert payload["nested"]["llmod_api_key"] == "***REDACTED***"
    assert payload["address"] == "1 Rothschild"

    # No raw secret value anywhere in the recorded step.
    dumped = str(step.model_dump())
    for secret in ("sk-super-secret", "super-secret-token", "hunter2", "another-secret"):
        assert secret not in dumped


@pytest.mark.asyncio
async def test_tracer_records_error_status_when_llm_call_fails() -> None:
    client = FakeLLMClient()  # no response configured -> LookupError

    tracer = StepTracer()
    with pytest.raises(LookupError):
        await tracer.call(client, "triage_planner", "sys", {}, _PlanResult)

    step = tracer.get_steps()[0]
    assert step.step == "triage_planner"
    assert step.status == "error"


@pytest.mark.asyncio
async def test_record_tool_trace_preserves_order_with_llm_steps() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _PlanResult(text="ok"))

    tracer = StepTracer()
    await tracer.call(client, "triage_planner", "sys", {}, _PlanResult)
    tracer.record_tool_trace(
        ToolTraceRecord(tool="geo_resolver", status="ok", duration_ms=1.5)
    )
    tracer.record_tool_trace(
        ToolTraceRecord(tool="nearby_services", status="ok", duration_ms=2.5)
    )

    steps = tracer.get_steps()
    assert [step.step for step in steps] == ["triage_planner", "geo_resolver", "nearby_services"]
    assert [step.type for step in steps] == ["llm", "tool", "tool"]

    tool_traces = tracer.get_tool_traces()
    assert [trace.tool for trace in tool_traces] == ["geo_resolver", "nearby_services"]
    assert [trace.duration_ms for trace in tool_traces] == [1.5, 2.5]


def test_record_tool_trace_reports_duration_and_status() -> None:
    tracer = StepTracer()
    tracer.record_tool_trace(
        ToolTraceRecord(
            tool="geo_resolver",
            status="ok",
            duration_ms=12.3,
            request_summary={"address": "HaYarkon 120, Tel Aviv"},
            response_summary={"outcome": "resolved"},
        )
    )

    step = tracer.get_steps()[0]
    assert step.step == "geo_resolver"
    assert step.type == "tool"
    assert step.status == "ok"
    assert step.detail["duration_ms"] == 12.3
    assert step.detail["status"] == "ok"
    assert step.detail["request_summary"] == {"address": "HaYarkon 120, Tel Aviv"}
    assert step.detail["response_summary"] == {"outcome": "resolved"}


def test_record_tool_trace_failure_is_recorded_as_error() -> None:
    tracer = StepTracer()
    tracer.record_tool_trace(
        ToolTraceRecord(
            tool="price_signal",
            status="error",
            duration_ms=5.0,
            error_code="timeout",
            response_summary={"error": "provider timed out"},
        )
    )

    step = tracer.get_steps()[0]
    assert step.status == "error"
    assert step.detail["error_code"] == "timeout"

    tool_trace = tracer.get_tool_traces()[0]
    assert tool_trace.status == "error"
    assert tool_trace.error_code == "timeout"


def test_record_tool_trace_skipped_is_recorded_as_public_ok() -> None:
    tracer = StepTracer()
    tracer.record_tool_trace(
        ToolTraceRecord(
            tool="nearby_services",
            status="skipped",
            duration_ms=0.0,
            response_summary={"reason": "geo_resolver did not resolve"},
        )
    )

    step = tracer.get_steps()[0]
    # The public StepTrace only knows "ok"/"error"; a deliberate skip is not
    # a failure, so it maps to "ok" while the richer "skipped" status is
    # still visible, unredacted, inside `detail`.
    assert step.status == "ok"
    assert step.detail["status"] == "skipped"


def test_record_tool_trace_redacts_sensitive_keys() -> None:
    tracer = StepTracer()
    tracer.record_tool_trace(
        ToolTraceRecord(
            tool="price_signal",
            status="error",
            duration_ms=3.0,
            request_summary={"api_key": "sk-secret", "address": "1 Rothschild"},
            response_summary={"authorization": "Bearer secret-token"},
        )
    )

    step = tracer.get_steps()[0]
    assert step.detail["request_summary"]["api_key"] == "***REDACTED***"
    assert step.detail["request_summary"]["address"] == "1 Rothschild"
    assert step.detail["response_summary"]["authorization"] == "***REDACTED***"

    dumped = str(step.model_dump())
    assert "sk-secret" not in dumped
    assert "secret-token" not in dumped
