import pytest

from src.listing_agent.config import Settings
from src.listing_agent.llm.client.dependency import get_llm_client
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.client.llmod import LLModClient


def test_fake_mode_selects_fake_client() -> None:
    settings = Settings(llm_mode="fake")

    client = get_llm_client(settings)

    assert isinstance(client, FakeLLMClient)


def test_llmod_mode_selects_llmod_client_when_configured() -> None:
    settings = Settings(
        llm_mode="llmod",
        llmod_api_key="key",
        llmod_base_url="https://example.invalid",
        llmod_model="some-model",
    )

    client = get_llm_client(settings)

    assert isinstance(client, LLModClient)


def test_llmod_mode_fails_clearly_when_not_configured() -> None:
    settings = Settings(llm_mode="llmod", llmod_api_key="", llmod_base_url="", llmod_model="")

    with pytest.raises(ValueError, match="LLMOD_API_KEY"):
        get_llm_client(settings)


def test_unsupported_llm_mode_fails_clearly() -> None:
    settings = Settings(llm_mode="some-unknown-mode")

    with pytest.raises(ValueError, match="Unsupported LLM_MODE"):
        get_llm_client(settings)
