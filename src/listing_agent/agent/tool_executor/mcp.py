"""Placeholder `ToolExecutor` for a future real MCP-backed tool transport.

This phase only establishes the `ToolExecutor` seam and configuration
validation. It never connects to a real MCP server. Selecting
`TOOL_BACKEND=mcp` without valid configuration fails immediately and
safely — never with a raw exception that leaks configured values, and
never falling back to local tools.
"""

from pydantic import BaseModel

from src.listing_agent.agent.schemas import ToolCall
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult


class ToolExecutorConfigurationError(RuntimeError):
    """Raised when a `ToolExecutor` backend is selected without valid
    configuration. Never includes the configured value itself — only the
    name of the missing setting — so this can be surfaced in logs, API
    error messages, or traces without risk of leaking a secret or an
    internal URL."""


class McpToolExecutor:
    """`ToolExecutor` placeholder for a real MCP server connection.

    TODO: implement real MCP network communication in a future phase
    (request/response wire format, auth, retries). Until then, every call
    to `execute()` raises `NotImplementedError`.
    """

    def __init__(self, server_url: str, timeout_seconds: float) -> None:
        if not server_url or not server_url.strip():
            raise ToolExecutorConfigurationError(
                "TOOL_BACKEND=mcp requires MCP_SERVER_URL to be set. "
                "See .env.example for the required configuration."
            )
        if timeout_seconds <= 0:
            raise ToolExecutorConfigurationError(
                "TOOL_BACKEND=mcp requires a positive MCP_REQUEST_TIMEOUT_SECONDS."
            )
        # Stored only for a future real implementation; never logged or
        # included in any exception/trace message.
        self._server_url = server_url
        self._timeout_seconds = timeout_seconds

    async def execute(self, call: ToolCall, input_model: BaseModel) -> ToolExecutionResult:
        raise NotImplementedError(
            "McpToolExecutor does not implement real MCP network communication yet. "
            "This phase only establishes the ToolExecutor interface and configuration "
            "validation; real MCP wiring is a future phase."
        )
