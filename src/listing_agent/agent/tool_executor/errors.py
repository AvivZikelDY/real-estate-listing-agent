"""Typed tool error taxonomy shared by every `ToolExecutor` backend.

`ToolErrorCode` is the single vocabulary used to classify *any* reason a
tool call did not produce a definitive successful business result — both
genuine technical failures (timeout, rate limiting, provider outage,
unexpected internal error) and input problems (invalid input). It exists
so retry decisions live in exactly one place (`RETRYABLE_TOOL_ERROR_CODES`)
instead of being re-derived ad hoc by every caller.

Note: the three business "no single answer" outcomes a tool can report
today (`resolved`/`ambiguous`/`not_found` for `geo_resolver`,
`compared`/`insufficient_data`/`no_comparable_data` for `price_signal`) are
still reported as `ToolExecutionResult(status="ok", ...)` with the
specifics in the typed output's own `outcome` field (see
`src/listing_agent/tools/schemas.py`) — that Phase 12 contract is
unchanged. `ToolErrorCode.not_found` / `.ambiguous_result` /
`.insufficient_data` exist in this taxonomy for tools/executors that need
to signal one of these as a genuine execution failure (e.g. a future
implementation without a rich typed `outcome` field) rather than a typed
business outcome — they are never retried either way.
"""

from enum import StrEnum

from pydantic import BaseModel, computed_field


class ToolErrorCode(StrEnum):
    """Why a tool call did not produce a definitive successful result."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    AMBIGUOUS_RESULT = "ambiguous_result"
    INSUFFICIENT_DATA = "insufficient_data"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL_ERROR = "internal_error"


# Only genuinely transient technical failures are ever worth retrying.
# Input problems and business "no answer" outcomes will not change on a
# second attempt.
RETRYABLE_TOOL_ERROR_CODES: frozenset[ToolErrorCode] = frozenset(
    {
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.RATE_LIMITED,
        ToolErrorCode.PROVIDER_UNAVAILABLE,
    }
)

NEVER_RETRY_TOOL_ERROR_CODES: frozenset[ToolErrorCode] = frozenset(
    {
        ToolErrorCode.INVALID_INPUT,
        ToolErrorCode.NOT_FOUND,
        ToolErrorCode.AMBIGUOUS_RESULT,
        ToolErrorCode.INSUFFICIENT_DATA,
    }
)


class ToolError(BaseModel):
    """A structured, secret-safe description of why a tool call failed.

    `message` must never include secrets, authorization headers, or raw
    environment/configuration values (e.g. a real MCP server URL) — only a
    human-readable description of what went wrong.
    """

    code: ToolErrorCode
    message: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def retryable(self) -> bool:
        """Whether a caller may retry this specific failure, at most once."""
        return self.code in RETRYABLE_TOOL_ERROR_CODES


class ToolExecutionError(RuntimeError):
    """Base class for exceptions a tool/executor can raise to signal a
    specific `ToolErrorCode` without encoding it into a free-text message.

    Not required for every tool to use (existing mocks continue to return
    `ToolResult(status=..., ...)` directly per the Phase 12 contract) but
    gives any tool/executor implementation a precise, typed way to signal
    one of these conditions when it cannot express it via a typed
    `outcome` field.
    """

    code: ToolErrorCode = ToolErrorCode.INTERNAL_ERROR


class ToolInvalidInputError(ToolExecutionError):
    code = ToolErrorCode.INVALID_INPUT


class ToolNotFoundError(ToolExecutionError):
    code = ToolErrorCode.NOT_FOUND


class ToolAmbiguousResultError(ToolExecutionError):
    code = ToolErrorCode.AMBIGUOUS_RESULT


class ToolInsufficientDataError(ToolExecutionError):
    code = ToolErrorCode.INSUFFICIENT_DATA


class ToolTimeoutError(ToolExecutionError):
    code = ToolErrorCode.TIMEOUT


class ToolRateLimitedError(ToolExecutionError):
    code = ToolErrorCode.RATE_LIMITED


class ToolProviderUnavailableError(ToolExecutionError):
    code = ToolErrorCode.PROVIDER_UNAVAILABLE


class ToolInternalError(ToolExecutionError):
    code = ToolErrorCode.INTERNAL_ERROR
