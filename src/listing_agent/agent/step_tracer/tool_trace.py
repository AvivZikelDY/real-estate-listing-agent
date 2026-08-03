"""Internal typed trace record for a single tool execution (Phase 16).

Distinct from the public `StepTrace` (`api/schemas.py`) that `/api/execute`
returns unchanged: `ToolTraceRecord` is the structured, secret-safe payload
built directly from the *real* tool call (its actual input and actual
result - never reconstructed afterwards from something else) and stored
inside that step's `StepTrace.detail` — an already-compatible nested
mechanism, so the public four-field `/api/execute` contract and the
existing `StepTrace` shape (`step`, `type`, `status`, `detail`) never
change.
"""

from typing import Any, Literal

from pydantic import BaseModel


class ToolTraceRecord(BaseModel):
    """One tool call's internal trace, in the same generic shape regardless
    of which tool ran.

    - `request_summary`/`response_summary` are sanitized, structured
      summaries derived directly from the real input model and the real
      `ToolExecutionResult` - never a free-text reconstruction written
      after the fact, and never the raw provider payload (there is none:
      both are already-validated Pydantic models).
    - Never contains authorization headers, API keys, environment values,
      access tokens, or unrelated full listing content - only the fields a
      tool actually consumed/returned, and even those are redacted for any
      sensitive-looking key (see `agent.step_tracer.tracer._redact`).
    """

    trace_type: Literal["tool"] = "tool"
    tool: str
    request_summary: dict[str, Any] | None = None
    response_summary: dict[str, Any] | None = None
    status: Literal["ok", "error", "skipped"]
    duration_ms: float
    error_code: str | None = None
