"""Dependency function building the active `ToolExecutor`.

Selection is driven entirely by `Settings.tool_backend` (env var
`TOOL_BACKEND`):
- "mock" (default) -> `LocalToolExecutor`, running the local in-process
  `ToolRegistry` (itself governed independently by `TOOLS_MODE`).
- "mcp" -> `McpToolExecutor`, a placeholder that fails fast and safely if
  `MCP_SERVER_URL`/`MCP_REQUEST_TIMEOUT_SECONDS` are not validly configured,
  and otherwise raises `NotImplementedError` on any actual call (no real
  MCP network communication exists yet).

Any other value fails clearly and immediately.
"""

from src.listing_agent.agent.tool_executor.base import ToolExecutor
from src.listing_agent.agent.tool_executor.local import LocalToolExecutor
from src.listing_agent.agent.tool_executor.mcp import McpToolExecutor
from src.listing_agent.config import Settings, get_settings
from src.listing_agent.tools.dependency import get_tool_registry

SUPPORTED_TOOL_BACKENDS = ("mock", "mcp")


def get_tool_executor(settings: Settings | None = None) -> ToolExecutor:
    """Return the `ToolExecutor` selected by `settings.tool_backend`."""
    settings = settings or get_settings()
    backend = settings.tool_backend.strip().lower()

    if backend == "mock":
        return LocalToolExecutor(
            get_tool_registry(settings), timeout_seconds=settings.tool_timeout_seconds
        )

    if backend == "mcp":
        return McpToolExecutor(
            server_url=settings.mcp_server_url,
            timeout_seconds=settings.mcp_request_timeout_seconds,
        )

    raise ValueError(
        f"Unsupported TOOL_BACKEND: '{settings.tool_backend}'. "
        f"Supported values are: {', '.join(SUPPORTED_TOOL_BACKENDS)}."
    )
