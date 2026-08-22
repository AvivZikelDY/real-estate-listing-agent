"""The `ToolExecutor` seam between `agent_orchestrator`/`evidence_collector`
and however a planned tool call actually gets executed.

`evidence_collector` depends only on this interface — never on
`ToolRegistry`, the mock implementations, or the live implementations
directly. This is what lets the transport (in-process local tools today,
a real MCP server in a future phase) change without touching any
deterministic agent module.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from src.listing_agent.agent.schemas import ToolCall
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult


@runtime_checkable
class ToolExecutor(Protocol):
    """Executes one already-validated, planned tool call.

    Implementations must never raise an unhandled exception for a normal
    "the tool ran and didn't have a good answer" case — every outcome,
    including timeouts and provider failures, comes back as a
    `ToolExecutionResult` (see `result.py` and `errors.py`). A genuine
    configuration problem with the executor itself (e.g. a backend
    selected without valid connection details) is raised eagerly at
    construction time instead, so a caller never silently talks to a
    misconfigured backend.
    """

    async def execute(self, call: ToolCall, input_model: BaseModel) -> ToolExecutionResult:
        """Execute `call.tool` with the already-built, typed `input_model`.

        `call` is the planned tool call this input was built for (used to
        identify which tool to run); `input_model` is the concrete,
        Phase-12-frozen Pydantic input model (e.g. `GeoResolverInput`)
        that the caller has already constructed, including any
        cross-tool dependency data (e.g. resolved coordinates).

        Implementations apply their own timeout and at-most-one-retry
        policy (retrying only `timeout`/`rate_limited`/`provider_unavailable`
        failures) and return the final, typed `ToolExecutionResult`.
        """
        ...
