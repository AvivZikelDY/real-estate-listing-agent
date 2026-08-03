"""`ToolExecutionResult`: the unified, typed result every `ToolExecutor`
backend returns (finalized in Phase 14).

Distinct from `src.listing_agent.tools.schemas.ToolResult` (the envelope a
raw tool implementation returns): `ToolExecutionResult` is what the
*executor* returns after applying timeout/retry policy and classifying any
failure into a typed `ToolError` — this is the shape `evidence_collector`
actually consumes.
"""

from typing import Literal

from pydantic import BaseModel, model_validator

from src.listing_agent.agent.tool_executor.errors import ToolError


class ToolExecutionResult[DataT: BaseModel](BaseModel):
    """The result of one `ToolExecutor.execute()` call, after timeout/retry.

    Invariants (enforced below):
    - `status == "ok"` requires `output` to be set and `error` to be unset.
    - `status == "error"` requires `error` to be set and `output` to be
      unset — an error result must never also carry a successful output.
    - `status == "skipped"` requires neither `output` nor `error` (the
      `reason` field explains why, e.g. `price_signal` on a rental).
    """

    status: Literal["ok", "error", "skipped"]
    output: DataT | None = None
    error: ToolError | None = None
    reason: str | None = None
    # How many attempts were made to obtain this result (1 = succeeded or
    # failed on the first try with no retry attempted; 2 = one retry was
    # attempted per the "at most one retry" policy).
    attempts: int = 1

    @model_validator(mode="after")
    def _status_matches_payload(self) -> "ToolExecutionResult":
        if self.status == "ok":
            if self.output is None:
                raise ValueError("status='ok' requires 'output' to be set.")
            if self.error is not None:
                raise ValueError("status='ok' must not carry an 'error'.")
        elif self.status == "error":
            if self.error is None:
                raise ValueError("status='error' requires 'error' to be set.")
            if self.output is not None:
                raise ValueError("status='error' must not carry a successful 'output'.")
        elif self.status == "skipped" and (self.output is not None or self.error is not None):
            raise ValueError("status='skipped' must not carry 'output' or 'error'.")
        return self

    @property
    def ok(self) -> bool:
        """Convenience accessor: `True` only for `status == "ok"`."""
        return self.status == "ok"
