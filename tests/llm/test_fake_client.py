import pytest
from pydantic import BaseModel

from src.listing_agent.llm.client.fake import FakeLLMClient


class _Greeting(BaseModel):
    text: str


class _Other(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_fake_client_returns_configured_response() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _Greeting(text="hello"))

    result = await client.generate_json(
        module="triage_planner",
        system_prompt="system",
        user_payload={"foo": "bar"},
        response_model=_Greeting,
    )

    assert isinstance(result, _Greeting)
    assert result.text == "hello"


@pytest.mark.asyncio
async def test_fake_client_constructor_accepts_responses() -> None:
    client = FakeLLMClient(responses={"report_writer": _Greeting(text="hi")})

    result = await client.generate_json(
        module="report_writer",
        system_prompt="system",
        user_payload={},
        response_model=_Greeting,
    )

    assert result == _Greeting(text="hi")


@pytest.mark.asyncio
async def test_fake_client_is_deterministic_across_repeated_calls() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _Greeting(text="stable"))

    first = await client.generate_json("triage_planner", "s", {}, _Greeting)
    second = await client.generate_json("triage_planner", "s", {}, _Greeting)

    assert first == second == _Greeting(text="stable")


@pytest.mark.asyncio
async def test_fake_client_supports_a_queue_of_distinct_responses() -> None:
    client = FakeLLMClient()
    client.queue_response("triage_planner", _Greeting(text="first"))
    client.queue_response("triage_planner", _Greeting(text="second"))

    first = await client.generate_json("triage_planner", "s", {}, _Greeting)
    second = await client.generate_json("triage_planner", "s", {}, _Greeting)

    assert first.text == "first"
    assert second.text == "second"


@pytest.mark.asyncio
async def test_fake_client_raises_when_module_not_configured() -> None:
    client = FakeLLMClient()

    with pytest.raises(LookupError):
        await client.generate_json("triage_planner", "s", {}, _Greeting)


@pytest.mark.asyncio
async def test_fake_client_raises_on_response_model_mismatch() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", _Other(value=1))

    with pytest.raises(TypeError):
        await client.generate_json("triage_planner", "s", {}, _Greeting)
