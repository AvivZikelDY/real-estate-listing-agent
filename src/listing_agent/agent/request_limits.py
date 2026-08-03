"""Deterministic, pre-flight request limits (Phase 17).

These guardrails protect cost, latency, and reliability: an oversized
prompt, a runaway plan, or a slow tool must never be allowed to consume
unbounded LLM budget or wall-clock time. All checks here are deterministic
Python - never an extra LLM call - and are applied *before* the expensive
work they'd otherwise waste (e.g. prompt length is checked before
`triage_planner` is ever invoked).

See `docs/contracts.md` §6 for the rationale behind each default and
`src/listing_agent/config.py` for how each limit is configured.
"""

from src.listing_agent.config import Settings


class RequestLimitError(Exception):
    """Raised when a request violates a configured, deterministic limit.

    Always carries a single human-readable `message` describing exactly
    which limit was exceeded, safe to surface directly as
    `ExecuteResponse.error`.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def check_prompt_length(prompt: str, settings: Settings) -> None:
    """Reject an oversized prompt before any LLM call is made.

    Raises:
        RequestLimitError: if `prompt` exceeds `settings.max_prompt_length`.
    """
    if len(prompt) > settings.max_prompt_length:
        raise RequestLimitError(
            f"Prompt is too long: {len(prompt)} characters exceeds the "
            f"maximum of {settings.max_prompt_length} characters. "
            "Please shorten the listing text/question and try again."
        )
