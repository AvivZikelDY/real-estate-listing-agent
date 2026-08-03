"""The shared tool interface every tool implementation must satisfy."""

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from src.listing_agent.tools.schemas import ToolResult

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


@runtime_checkable
class BaseTool(Protocol[InputT, OutputT]):
    """Structural interface for a deterministic, LLM-free tool.

    Implementations must never raise an unhandled exception from
    `execute()` — failures are reported via `ToolResult(status="error", ...)`.
    """

    @property
    def name(self) -> str:
        """The exact, whitelisted tool name (e.g. "geo_resolver")."""
        ...

    async def execute(self, input_model: InputT) -> ToolResult[OutputT]:
        """Run the tool and return a structured `ToolResult`."""
        ...
