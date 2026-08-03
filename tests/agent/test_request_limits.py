import pytest

from src.listing_agent.agent.request_limits import RequestLimitError, check_prompt_length
from src.listing_agent.config import Settings


def test_prompt_within_limit_passes() -> None:
    settings = Settings(max_prompt_length=100)
    check_prompt_length("a short prompt", settings)  # must not raise


def test_oversized_prompt_raises_clear_error() -> None:
    settings = Settings(max_prompt_length=10)

    with pytest.raises(RequestLimitError) as exc_info:
        check_prompt_length("this prompt is definitely too long", settings)

    assert "too long" in exc_info.value.message
    assert "10" in exc_info.value.message


def test_prompt_exactly_at_limit_passes() -> None:
    settings = Settings(max_prompt_length=5)
    check_prompt_length("12345", settings)  # must not raise
