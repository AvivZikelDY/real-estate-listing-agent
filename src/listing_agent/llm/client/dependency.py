"""Dependency function selecting the active LLM client implementation.

Selection is driven entirely by `Settings.llm_mode` (env var `LLM_MODE`):
- "fake"  -> FakeLLMClient (deterministic, offline)
- "llmod" -> LLModClient (real LLMod.ai client; network calls not implemented yet)

Any other value fails clearly and immediately.
"""

from typing import TYPE_CHECKING

from src.listing_agent.config import Settings, get_settings
from src.listing_agent.llm.client.base import LLMClient
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.client.llmod import LLModClient

if TYPE_CHECKING:
    from src.listing_agent.agent.step_tracer import StepTracer

SUPPORTED_LLM_MODES = ("fake", "llmod")


def get_llm_client(
    settings: Settings | None = None, step_tracer: "StepTracer | None" = None
) -> LLMClient:
    """Return the `LLMClient` implementation selected by `settings.llm_mode`.

    `step_tracer`, when given, is passed through to `LLModClient` so it can
    record a bounded structured-output repair attempt as its own step. It
    is never used to receive secrets, auth headers, or environment
    variables.
    """
    settings = settings or get_settings()
    mode = settings.llm_mode.strip().lower()

    if mode == "fake":
        return FakeLLMClient()

    if mode == "llmod":
        return LLModClient(
            api_key=settings.llmod_api_key,
            base_url=settings.llmod_base_url,
            model=settings.llmod_model,
            step_tracer=step_tracer,
        )

    raise ValueError(
        f"Unsupported LLM_MODE: '{settings.llm_mode}'. "
        f"Supported values are: {', '.join(SUPPORTED_LLM_MODES)}."
    )
