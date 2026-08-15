"""`ToolExecutor` backed by the local, in-process `ToolRegistry`.

This is the default/current backend: it runs whichever local tool
implementations `ToolRegistry` was populated with (mock or Nir's real
implementations, per `TOOLS_MODE` — see `src/listing_agent/tools/dependency.py`).
It performs no network I/O of its own, but still applies a conservative
per-call timeout and an at-most-one-retry policy (Phase 14), since a local
tool implementation may still wrap a slow local computation or, for Nir's
real implementations later, an outbound HTTP call.
"""

import asyncio

from pydantic import BaseModel

from src.listing_agent.agent.schemas import ToolCall
from src.listing_agent.agent.tool_executor.errors import (
    RETRYABLE_TOOL_ERROR_CODES,
    ToolAmbiguousResultError,
    ToolError,
    ToolErrorCode,
    ToolExecutionError,
    ToolInsufficientDataError,
    ToolInvalidInputError,
    ToolNotFoundError,
    ToolProviderUnavailableError,
    ToolRateLimitedError,
    ToolTimeoutError,
)
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult
from src.listing_agent.tools.registry import ToolRegistry

# Nir's real tool implementations are all live now: govmap (~0.7-2.9s) and
# Overpass, whose mirror fallback can legitimately run tens of seconds when
# the canonical mirror is overloaded (measured once at 41s end to end). 120s
# gives that headroom while still bounding a genuinely hung request.
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

# Maps a specific typed `ToolExecutionError` subclass to its `ToolErrorCode`.
# A tool/registry may raise one of these to signal a precise failure reason
# instead of only returning `ToolResult(status="error", ...)`.
_TYPED_ERROR_CODES: dict[type[ToolExecutionError], ToolErrorCode] = {
    ToolTimeoutError: ToolErrorCode.TIMEOUT,
    ToolRateLimitedError: ToolErrorCode.RATE_LIMITED,
    ToolProviderUnavailableError: ToolErrorCode.PROVIDER_UNAVAILABLE,
    ToolInvalidInputError: ToolErrorCode.INVALID_INPUT,
    ToolNotFoundError: ToolErrorCode.NOT_FOUND,
    ToolAmbiguousResultError: ToolErrorCode.AMBIGUOUS_RESULT,
    ToolInsufficientDataError: ToolErrorCode.INSUFFICIENT_DATA,
}


class LocalToolExecutor:
    """Executes planned tool calls against a local `ToolRegistry`.

    Applies a per-call timeout and retries at most once, and only for the
    retryable failure codes (`timeout`, `rate_limited`,
    `provider_unavailable`) — never for input problems or business "no
    answer" outcomes.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._timeout_seconds = timeout_seconds

    async def execute(self, call: ToolCall, input_model: BaseModel) -> ToolExecutionResult:
        result = await self._attempt(call, input_model)
        if (
            result.status == "error"
            and result.error is not None
            and result.error.code in RETRYABLE_TOOL_ERROR_CODES
        ):
            result = await self._attempt(call, input_model)
            result.attempts = 2
        return result

    async def _attempt(self, call: ToolCall, input_model: BaseModel) -> ToolExecutionResult:
        try:
            tool = self._registry.get(call.tool)
        except KeyError as exc:
            return ToolExecutionResult(
                status="error",
                error=ToolError(code=ToolErrorCode.INTERNAL_ERROR, message=str(exc)),
            )

        try:
            raw = await asyncio.wait_for(tool.execute(input_model), timeout=self._timeout_seconds)
        except TimeoutError:
            return ToolExecutionResult(
                status="error",
                error=ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    message=f"'{call.tool}' timed out after {self._timeout_seconds}s.",
                ),
            )
        except ToolExecutionError as exc:
            code = _TYPED_ERROR_CODES.get(type(exc), ToolErrorCode.INTERNAL_ERROR)
            return ToolExecutionResult(status="error", error=ToolError(code=code, message=str(exc)))
        except Exception as exc:  # deliberate last-resort safety net
            return ToolExecutionResult(
                status="error",
                error=ToolError(
                    code=ToolErrorCode.INTERNAL_ERROR,
                    message=f"Unhandled error in tool '{call.tool}': {exc}",
                ),
            )

        if raw.status == "ok":
            return ToolExecutionResult(status="ok", output=raw.data)
        if raw.status == "skipped":
            return ToolExecutionResult(status="skipped", reason=raw.reason)
        # raw.status == "error": a tool's own structured technical failure
        # (or ToolRegistry's last-resort safety net for an exception a
        # tool failed to catch itself). Without a more specific typed
        # exception, this is classified as an internal error.
        return ToolExecutionResult(
            status="error",
            error=ToolError(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=raw.error or f"'{call.tool}' failed with no further detail.",
            ),
        )
