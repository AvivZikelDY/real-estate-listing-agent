"""Tests for `LLModClient`: config validation, structured-output happy path,
the bounded single repair attempt, error conversion, and secret redaction.

All network interaction is faked via `httpx.MockTransport` — no real
LLMod.ai calls are made here. The one exception is the manually-skipped
smoke test at the bottom of this file, which only runs when
`RUN_LLM_SMOKE_TEST=true` is set explicitly.
"""

import json
import os

import httpx
import pytest
from pydantic import BaseModel

from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.dependency import get_llm_client
from src.listing_agent.llm.client.llmod import LLModClient, LLModError


class _Dummy(BaseModel):
    text: str


def _client(transport: httpx.MockTransport, step_tracer: StepTracer | None = None) -> LLModClient:
    return LLModClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="some-model",
        step_tracer=step_tracer,
        transport=transport,
    )


def _chat_response(content: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={"choices": [{"message": {"content": content}}]},
    )


def test_llmod_client_raises_clearly_when_config_missing() -> None:
    with pytest.raises(ValueError, match="LLMOD_API_KEY"):
        LLModClient(api_key="", base_url="", model="")


def test_llmod_client_raises_clearly_when_partially_configured() -> None:
    with pytest.raises(ValueError, match="LLMOD_MODEL"):
        LLModClient(api_key="key", base_url="https://example.invalid", model="")


def test_llmod_client_constructs_when_fully_configured() -> None:
    client = LLModClient(api_key="key", base_url="https://example.invalid", model="some-model")

    assert client is not None


async def test_llmod_client_generate_json_happy_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(json.dumps({"text": "hello"}))

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    result = await client.generate_json(
        module="triage_planner",
        system_prompt="sys",
        user_payload={"prompt": "hi"},
        response_model=_Dummy,
    )

    assert result == _Dummy(text="hello")
    assert len(requests) == 1
    sent_body = json.loads(requests[0].content)
    assert sent_body["model"] == "some-model"
    assert sent_body["messages"][0] == {"role": "system", "content": "sys"}
    assert sent_body["response_format"]["type"] == "json_schema"
    assert requests[0].headers["authorization"] == "Bearer test-key"


async def test_llmod_client_appends_chat_completions_path_to_base_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(json.dumps({"text": "hello"}))

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    await client.generate_json(
        module="triage_planner",
        system_prompt="sys",
        user_payload={},
        response_model=_Dummy,
    )

    assert str(requests[0].url) == "https://example.invalid/v1/chat/completions"


async def test_llmod_client_repairs_invalid_json_once_and_succeeds() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _chat_response("not valid json at all")
        return _chat_response(json.dumps({"text": "fixed"}))

    transport = httpx.MockTransport(handler)
    tracer = StepTracer()
    client = _client(transport, step_tracer=tracer)

    result = await client.generate_json(
        module="triage_planner",
        system_prompt="sys",
        user_payload={},
        response_model=_Dummy,
    )

    assert result == _Dummy(text="fixed")
    assert call_count == 2

    repair_steps = [s for s in tracer.get_steps() if s.step == "triage_planner_repair"]
    assert len(repair_steps) == 1
    assert repair_steps[0].status == "ok"
    assert repair_steps[0].type == "llm"


async def test_llmod_client_gives_up_after_one_failed_repair_attempt() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _chat_response("still not valid json")

    transport = httpx.MockTransport(handler)
    tracer = StepTracer()
    client = _client(transport, step_tracer=tracer)

    with pytest.raises(LLModError, match="repair attempt"):
        await client.generate_json(
            module="triage_planner",
            system_prompt="sys",
            user_payload={},
            response_model=_Dummy,
        )

    # Exactly two network calls total: the original attempt + one repair.
    # No unbounded retry loop.
    assert call_count == 2
    repair_steps = [s for s in tracer.get_steps() if s.step == "triage_planner_repair"]
    assert len(repair_steps) == 1
    assert repair_steps[0].status == "error"


async def test_llmod_client_converts_provider_error_to_human_readable_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            json={"error": {"message": "Invalid API key", "type": "auth_error"}},
        )

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    with pytest.raises(LLModError, match="Invalid API key"):
        await client.generate_json(
            module="triage_planner",
            system_prompt="sys",
            user_payload={},
            response_model=_Dummy,
        )


async def test_llmod_client_converts_timeout_to_human_readable_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    with pytest.raises(LLModError, match="timed out"):
        await client.generate_json(
            module="triage_planner",
            system_prompt="sys",
            user_payload={},
            response_model=_Dummy,
        )


async def test_llmod_client_never_records_secrets_in_repair_trace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("not valid json")

    transport = httpx.MockTransport(handler)
    tracer = StepTracer()
    client = _client(transport, step_tracer=tracer)

    with pytest.raises(LLModError):
        await client.generate_json(
            module="triage_planner",
            system_prompt="sys",
            user_payload={},
            response_model=_Dummy,
        )

    for step in tracer.get_steps():
        serialized = json.dumps(step.model_dump(mode="json"))
        assert "test-key" not in serialized
        assert "Authorization" not in serialized
        assert "Bearer" not in serialized


class _WithOpenDict(BaseModel):
    name: str
    arguments: dict


async def test_llmod_client_falls_back_to_json_object_mode_for_open_dict_fields() -> None:
    """A `dict[str, Any]`-style field can't be expressed in OpenAI strict
    JSON schema (which requires fixed, fully-required properties on every
    object). The client must detect this and fall back to plain
    `{"type": "json_object"}` mode instead of sending a schema the
    provider would reject."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(json.dumps({"name": "geo_resolver", "arguments": {"a": 1}}))

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    result = await client.generate_json(
        module="triage_planner",
        system_prompt="sys",
        user_payload={},
        response_model=_WithOpenDict,
    )

    assert result == _WithOpenDict(name="geo_resolver", arguments={"a": 1})
    sent_body = json.loads(requests[0].content)
    assert sent_body["response_format"] == {"type": "json_object"}
    # The schema must still be communicated to the model as guidance text.
    assert "arguments" in sent_body["messages"][0]["content"]


async def test_llmod_client_uses_strict_json_schema_for_fully_fixed_models() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(json.dumps({"text": "hello"}))

    transport = httpx.MockTransport(handler)
    client = _client(transport)

    await client.generate_json(
        module="triage_planner",
        system_prompt="sys",
        user_payload={},
        response_model=_Dummy,
    )

    sent_body = json.loads(requests[0].content)
    assert sent_body["response_format"]["type"] == "json_schema"
    assert sent_body["response_format"]["json_schema"]["strict"] is True
    assert sent_body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


async def test_get_llm_client_wires_step_tracer_into_llmod_client() -> None:
    settings = Settings(
        llm_mode="llmod", llmod_api_key="k", llmod_base_url="https://x", llmod_model="m"
    )
    tracer = StepTracer()

    client = get_llm_client(settings=settings, step_tracer=tracer)

    assert isinstance(client, LLModClient)
    assert client._step_tracer is tracer  # noqa: SLF001


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_SMOKE_TEST") != "true",
    reason="Real, paid LLMod.ai smoke test. Set RUN_LLM_SMOKE_TEST=true to run it.",
)
async def test_llmod_client_real_smoke_test() -> None:
    """Makes one real call to the live LLMod.ai API.

    Requires LLMOD_API_KEY, LLMOD_BASE_URL, and LLMOD_MODEL to be set in
    the environment to real, valid values. Skipped by default because it
    costs money and requires network access; never run automatically in CI.
    """
    settings = Settings()
    client = get_llm_client(settings=settings)
    assert isinstance(client, LLModClient)

    result = await client.generate_json(
        module="triage_planner",
        system_prompt=(
            "Reply with a JSON object containing a single field 'text' with any short value."
        ),
        user_payload={"prompt": "Say hello."},
        response_model=_Dummy,
    )

    assert isinstance(result, _Dummy)
    assert result.text
