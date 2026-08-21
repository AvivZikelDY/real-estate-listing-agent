"""Application settings, loaded from environment variables / `.env`.

Only the configuration needed so far (LLM mode + LLMod.ai connection
details, tools mode) is defined here. See `.env.example` for the full list
of supported variables.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: str = "fake"
    llmod_api_key: str = ""
    llmod_base_url: str = ""
    llmod_model: str = ""
    tools_mode: str = "mock"

    # Selects which `ToolExecutor` transport backend the agent orchestrator
    # uses: "mock" (the default) runs tools in-process via the local
    # `ToolRegistry` (itself governed by `tools_mode` above); "mcp" is a
    # not-yet-implemented placeholder for a real MCP server connection.
    tool_backend: str = "mock"
    mcp_server_url: str = ""
    mcp_request_timeout_seconds: float = 10.0

    # Conservative per-call timeout applied by `LocalToolExecutor` (Phase 14)
    # to every tool call, regardless of backend.
    tool_timeout_seconds: float = 8.0

    # --- Phase 17: request limits, deduplication, and execution deadlines ---
    # These are deterministic, pre-flight guardrails (never enforced with an
    # LLM call) that protect cost, latency, and reliability. See
    # `docs/contracts.md` §6 for the reasoning behind each default.

    # Maximum accepted length (characters) of `ExecuteRequest.prompt`.
    # Checked before any expensive work (including the `triage_planner`
    # LLM call) is attempted.
    max_prompt_length: int = 4000

    # Maximum number of claims a validated `TriagePlan` may carry forward.
    # Extra claims are dropped deterministically by `plan_validator`, not by
    # asking the LLM to re-plan.
    max_claims: int = 8

    # Maximum number of tool calls a validated `TriagePlan` may execute.
    # `plan_validator` already deduplicates by tool name (there are only
    # three whitelisted tools), so this is mostly a defensive cap.
    max_tool_calls: int = 3

    # Maximum cumulative wall-clock time (seconds) `evidence_collector` may
    # spend actually executing tool calls in one request, across all
    # planned calls. Once exceeded, any remaining planned calls are skipped
    # (not attempted) rather than the whole request failing.
    max_total_tool_execution_seconds: float = 20.0

    # Overall deadline (seconds) for one `POST /api/execute` request,
    # end-to-end (both LLM calls plus all tool execution). Configurable via
    # `LLM_REQUEST_TIMEOUT_SECONDS`. Defaults to the assignment's own
    # 300-second "no more than 5 minutes to get an answer" ceiling (see
    # `llm/client/llmod.py`'s `READ_TIMEOUT_SECONDS`, the per-HTTP-call LLM
    # client timeout this deadline must not undercut). A prior, much
    # smaller 45-second default caused real LLM calls that were still
    # comfortably within that 300-second per-call ceiling to be aborted
    # early with "Request exceeded the time limit of 45 seconds.".
    llm_request_timeout_seconds: float = 300.0


def get_settings() -> Settings:
    """Build a fresh `Settings` instance from the current environment.

    Deliberately not cached: tests toggle environment variables (e.g.
    `LLM_MODE`) between runs and must see the change immediately.
    """
    return Settings()
