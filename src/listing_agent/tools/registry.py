"""Registry that looks up and executes tools by their exact whitelisted name."""

from pydantic import BaseModel

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.schemas import ToolResult


class ToolRegistry:
    """Registers tools by exact name and executes them through `BaseTool`."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register `tool` under its exact `name`.

        Raises:
            ValueError: if a tool is already registered under that name.
        """
        if tool.name in self._tools:
            raise ValueError(f"A tool named '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        """Return the tool registered under `name`.

        Raises:
            KeyError: if no tool is registered under that name.
        """
        try:
            return self._tools[name]
        except KeyError:
            registered = ", ".join(sorted(self._tools)) or "(none)"
            raise KeyError(
                f"Unknown tool: '{name}'. Registered tools: {registered}."
            ) from None

    async def execute(self, name: str, input_model: BaseModel) -> ToolResult:
        """Look up `name` and execute it with `input_model`.

        Unknown tool names are rejected immediately (a caller/plan bug).
        Any exception a registered tool fails to catch internally is caught
        here as a last resort and converted into a structured
        `ToolResult(status="error", ...)` — `execute()` never raises on
        behalf of a misbehaving tool.
        """
        tool = self.get(name)
        try:
            return await tool.execute(input_model)
        except Exception as exc:  # deliberate last-resort safety net
            return ToolResult(
                status="error",
                error=f"Unhandled error in tool '{name}': {exc}",
            )
