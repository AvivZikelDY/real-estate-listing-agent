"""Deterministic tracer that records completed LLM calls in order.

`StepTracer` wraps calls to an `LLMClient` so every module call is recorded
as a `StepTrace` (see `src/listing_agent/api/schemas.py`). It never records
secrets: only `module`, `system_prompt`, `user_payload`, and the resulting
response are captured, and any value under a sensitive-looking key is
redacted before being stored.
"""

from typing import Any, Literal

from pydantic import BaseModel

from src.listing_agent.agent.step_tracer.tool_trace import ToolTraceRecord
from src.listing_agent.api.schemas import StepTrace
from src.listing_agent.llm.client.base import LLMClient

REDACTED = "***REDACTED***"

# Substrings (checked case-insensitively) that mark a key as sensitive and
# therefore never eligible to be written into a trace verbatim.
_SENSITIVE_KEY_MARKERS = (
    "key",
    "secret",
    "token",
    "password",
    "authorization",
    "auth",
    "credential",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: Any) -> Any:
    """Recursively redact any dict value whose key looks sensitive."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(str(key)) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class StepTracer:
    """Records every completed LLM call, in call order."""

    def __init__(self) -> None:
        self._traces: list[StepTrace] = []
        self._tool_traces: list[ToolTraceRecord] = []

    async def call(
        self,
        client: LLMClient,
        module: str,
        system_prompt: str,
        user_payload: dict,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Invoke `client.generate_json` and record the completed call."""
        try:
            response = await client.generate_json(
                module=module,
                system_prompt=system_prompt,
                user_payload=user_payload,
                response_model=response_model,
            )
        except Exception as exc:
            self._traces.append(
                StepTrace(
                    step=module,
                    type="llm",
                    status="error",
                    detail={
                        "prompt": {
                            "system_prompt": system_prompt,
                            "user_payload": _redact(user_payload),
                        },
                        "error": str(exc),
                    },
                )
            )
            raise

        self._traces.append(
            StepTrace(
                step=module,
                type="llm",
                status="ok",
                detail={
                    "prompt": {
                        "system_prompt": system_prompt,
                        "user_payload": _redact(user_payload),
                    },
                    "response": _redact(response.model_dump(mode="json")),
                },
            )
        )
        return response

    def get_steps(self) -> list[StepTrace]:
        """Return the recorded steps, in the order the calls completed."""
        return list(self._traces)

    def record_tool_step(
        self,
        step: str,
        status: Literal["ok", "error"],
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a completed tool-execution step (e.g. one tool call).

        Superseded by `record_tool_trace` for tool executions that need a
        typed, duration-aware trace; kept for any caller that only has a
        free-form detail dict to record.
        """
        self._traces.append(
            StepTrace(step=step, type="tool", status=status, detail=_redact(detail))
        )

    def record_tool_trace(self, trace: ToolTraceRecord) -> None:
        """Record a typed `ToolTraceRecord` built from a real tool call.

        Stored both in `_tool_traces` (for direct, typed test assertions on
        ordering/duration/status) and appended to the same public `_traces`
        list as a `StepTrace` (type="tool") so `/api/execute`'s `steps`
        field keeps its exact existing shape - the richer trace is nested
        inside `detail`, never as a new top-level field. A `"skipped"` tool
        status (dependency not met, e.g. geo_resolver failed so
        nearby_services never ran) maps to the public `status="ok"`: the
        step itself did not fail, it was deliberately not attempted.
        """
        self._tool_traces.append(trace)
        public_status: Literal["ok", "error"] = "error" if trace.status == "error" else "ok"
        self._traces.append(
            StepTrace(
                step=trace.tool,
                type="tool",
                status=public_status,
                detail=_redact(trace.model_dump(mode="json")),
            )
        )

    def get_tool_traces(self) -> list[ToolTraceRecord]:
        """Return the recorded tool traces, in the order the calls completed."""
        return list(self._tool_traces)

    def record_deterministic_step(
        self,
        step: str,
        status: Literal["ok", "error"],
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a completed deterministic-module step (e.g. plan_validator)."""
        self._traces.append(
            StepTrace(step=step, type="deterministic", status=status, detail=_redact(detail))
        )

    def record_llm_repair_step(
        self,
        module: str,
        status: Literal["ok", "error"],
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a bounded structured-output repair attempt for `module`.

        Real LLM providers occasionally return malformed structured output.
        `LLModClient` permits at most one repair attempt per call; when it
        happens, it is recorded here as its own `"llm"`-typed step (named
        `"<module>_repair"`) so the trace shows exactly what happened,
        without ever including secrets, auth headers, or environment
        variables in `detail`.
        """
        self._traces.append(
            StepTrace(
                step=f"{module}_repair",
                type="llm",
                status=status,
                detail=_redact(detail),
            )
        )

